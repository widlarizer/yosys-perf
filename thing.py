from enum import StrEnum
from pathlib import Path
import argparse
import pkgutil
import importlib
import subprocess
import functools
import tempfile
import json
import re
import sys
import os

try:
    import scripts
    HAS_SCRIPTS = True
except ImportError:
    HAS_SCRIPTS = False

r = functools.partial(subprocess.check_output, text=True)


class OutputMode(StrEnum):
    HUMAN = "human"
    CSV = "csv"


class RunMode(StrEnum):
    ARTIFACT = "artifact"
    VERILOG = "verilog"
    SYNTH = "synth"
    ANALYZE = "analyze"
    FF = "ff"


class SynthMode(StrEnum):
    SYNTH = "synth"
    SYNTH_FLATTEN = "synth -flatten"

    @staticmethod
    def from_str(s):
        match s:
            case "":        return SynthMode.SYNTH
            case "flatten": return SynthMode.SYNTH_FLATTEN
            case _:
                print(f"Invalid synthesis flow: {s}", file=sys.stderr)
                sys.exit(1)


def common_parent(paths):
    if not paths:
        return Path(".")
    common = []
    for parts in zip(*(p.parts for p in paths)):
        if len(set(parts)) > 1:
            break
        common.append(parts[0])
    return Path(*common) if common else Path(".")


def fmt_params(params):
    return "__".join(f"{k}_{v}" for k, v in sorted(params.items())) if params else ""


def design_map():
    if not HAS_SCRIPTS:
        return {}
    d = {}
    for _, modname, ispkg in pkgutil.iter_modules(scripts.__path__):
        if ispkg:
            continue
        mod = importlib.import_module(f"scripts.{modname}")
        for c in getattr(mod, "__all__", []):
            d[c.__name__.lower()] = c
    return d


def discover_designs(artifacts_dir):
    p = Path(artifacts_dir)
    return sorted(f.stem for f in p.glob("*.il")) if p.exists() else []


def params_from_str(pairs):
    ret = {}
    for pair in pairs:
        kv = pair.split("=")
        assert len(kv) == 2, f"Can't parse {pair} as parameter"
        ret[kv[0]] = kv[1]
    return ret


def tag_for(design, params):
    fp = fmt_params(params)
    return f"{design}_{fp}" if fp else design


def artifact_path_for(tag):
    return Path("artifacts") / f"{tag}.il"


SEQ_GROUPS = {"reg", "reg_ff", "reg_latch"}
MEM_GROUPS = {"mem"}


def load_cell_groups(json_path):
    with open(json_path) as f:
        groups = json.load(f).get("groups", {})
    cats = {}
    for gname, types in groups.items():
        cat = "seq" if gname in SEQ_GROUPS else "mem" if gname in MEM_GROUPS else "comb"
        for t in types:
            cats[t] = cat
    return cats


def dump_cell_groups(yosys_bin):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            [str(yosys_bin), "-p", f"help -dump-cells-json {tmp_path}"],
            capture_output=True, text=True, check=True,
        )
        return load_cell_groups(tmp_path)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: cell dump failed ({e}).", file=sys.stderr)
        return {}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def classify_cells(cells_breakdown, cell_cats):
    totals = {"seq": 0, "mem": 0, "comb": 0, "other": 0}
    by_type = {"seq": {}, "mem": {}, "comb": {}, "other": {}}
    for cell_type, count in cells_breakdown.items():
        cat = cell_cats.get(cell_type, "other")
        totals[cat] += count
        by_type[cat][cell_type] = count
    return totals, by_type


def run_yosys(yosys_bin, script, detailed_timing=False):
    cmd = [str(yosys_bin)]
    if detailed_timing:
        cmd.append("-d")
    cmd.extend(["-p", script])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout + result.stderr


_STAT_FIELDS = [
    "wires", "wire_bits", "public_wires", "public_wire_bits",
    "ports", "port_bits", "memories", "memory_bits", "processes",
]


def parse_stat(output):
    """parse yosys `stat` output across format versions"""
    stats = {}

    # new short format
    cells_matches = list(re.finditer(
        r'^\s+(\d+)\s+cells\s*$', output, re.MULTILINE
    ))

    if cells_matches:
        last = cells_matches[-1]
        stats["cells"] = int(last.group(1))

        # stop at "submodules"
        cells = {}
        for line in output[last.end():].split('\n'):
            m = re.match(r'^\s+(\d+)\s{3,}(\S+)\s*$', line)
            if m:
                cells[m.group(2)] = int(m.group(1))
            elif line.strip() == '':
                continue
            else:
                break
        stats["cells_breakdown"] = cells

        # other stat fields
        block_start = output.rfind('+---', 0, last.start())
        if block_start < 0:
            block_start = max(0, last.start() - 2000)
        block = output[block_start:last.start()]
        for field in _STAT_FIELDS:
            pat = rf'^\s+(\d+|-)\s+{re.escape(field)}\s*$'
            m = re.search(pat, block, re.MULTILINE)
            if m and m.group(1) != '-':
                stats[field.replace(' ', '_')] = int(m.group(1))

    else:
        # old format
        match = re.search(
            r'(?:Printing statistics|Count including submodules).*?\n(.*?)(?=End of script|\Z)',
            output, re.DOTALL | re.IGNORECASE
        )
        if match:
            summary = match.group(1)
            for field in _STAT_FIELDS + ["cells"]:
                pat = rf'Number of {field}:\s+(\d+)'
                m = re.search(pat, summary, re.IGNORECASE)
                if m:
                    stats[field] = int(m.group(1))

            cells = {}
            cells_section = re.search(
                r'Number of cells:\s+\d+\s*\n((?:\s+\S+\s+\d+\s*\n)*)',
                summary, re.IGNORECASE
            )
            if cells_section:
                for m in re.finditer(r'^\s+(\S+)\s+(\d+)\s*$', cells_section.group(1), re.MULTILINE):
                    cells[m.group(1)] = int(m.group(2))
            stats["cells_breakdown"] = cells
        else:
            stats["cells_breakdown"] = {}

    m = re.search(
        r'CPU:\s*user\s+([\d.]+)s\s+system\s+([\d.]+)s.*?MEM:\s*([\d.]+)\s*MB',
        output
    )
    if m:
        stats["user_time"] = float(m.group(1))
        stats["sys_time"] = float(m.group(2))
        stats["time"] = stats["user_time"] + stats["sys_time"]
        stats["mem_mb"] = float(m.group(3))

    m = re.search(r'Time spent:\s*(.+?)(?:\n|$)', output)
    if m:
        stats["top_times"] = [
            (pm.group(3), int(pm.group(1)), int(pm.group(2)), int(pm.group(4)))
            for pm in re.finditer(r'(\d+)%\s+(\d+)x\s+(\w+)\s*\((\d+)\s*sec\)', m.group(1))
        ]

    for m in re.finditer(r'ABC:.*?nd\s*=\s*(\d+).*?lev\s*=\s*(\d+)', output):
        stats["abc_nd"] = stats.get("abc_nd", 0) + int(m.group(1))
        lev = int(m.group(2))
        if lev > stats.get("abc_lev", 0):
            stats["abc_lev"] = lev

    return stats


def parse_detailed_timing(output):
    return sorted(
        [(pm.group(4), float(pm.group(3)), int(pm.group(2)))
         for pm in re.finditer(r'^\s*(\d+)%\s+(\d+)\s+calls?\s+([\d.]+)\s+sec\s+(\S+)', output, re.MULTILINE)],
        key=lambda x: -x[1]
    )


def format_pass_timing(pass_times, top_n=10):
    if not pass_times:
        return ""
    total = sum(t for _, t, _ in pass_times)
    show = pass_times if top_n is None else pass_times[:top_n]
    lines = []
    for name, secs, count in show:
        pct = (secs / total * 100) if total > 0 else 0
        count_str = f" ({count}x)" if count > 1 else ""
        lines.append(f"    {pct:5.1f}%  {secs:6.3f}s  {name}{count_str}")
    if top_n is not None and len(pass_times) > top_n:
        shown = sum(t for _, t, _ in pass_times[:top_n])
        other_pct = ((total - shown) / total * 100) if total > 0 else 0
        lines.append(f"    {other_pct:5.1f}%  ... {len(pass_times) - top_n} more passes")
    return '\n'.join(lines)


def parse_abc_area(output):
    return sum(int(m.group(1)) for m in re.finditer(r'\bnd\s*=\s*(\d+)', output))


class HumanOut:
    def __init__(self, verbose=False):
        self._verbose = verbose

    def add(self, yosys_bin, tag, result):
        print(f"{yosys_bin}: {tag}")
        print(result)

    def add_stats(self, stats, yosys_bins):
        design = stats.get("design", "unknown")
        yosys = Path(stats.get("yosys", "yosys")).name
        prefix = f"{yosys}: {design}" if len(yosys_bins) > 1 else design
        user = stats.get("user_time")

        if user is not None:
            print(f"{prefix}: user {user:.2f}s system {stats['sys_time']:.2f}s, MEM: {stats['mem_mb']:.2f} MB")
        else:
            print(f"{prefix}:")

        abc_nd = stats.get("abc_nd")
        if abc_nd:
            print(f"  ABC: {abc_nd} nodes, {stats.get('abc_lev', 0)} levels")

        pass_timing = stats.get("pass_timing", [])
        if pass_timing:
            print(f"  Pass timing:")
            print(format_pass_timing(pass_timing, top_n=None if self._verbose else 10))
        else:
            top_times = stats.get("top_times", [])
            if top_times:
                parts = [f"{pct}% {name} ({count}x)" for name, pct, count, _ in top_times]
                print(f"  Top time: {', '.join(parts)}")
        print()

    def add_ff(self, result):
        total = result["total"]
        seq, comb, other = result["seq"], result["comb"], result["other"]

        print(f"{result['design']}: {total} cells")
        if total > 0:
            print(f"  seq:   {seq:6d}  ({seq/total*100:5.1f}%)")
            print(f"  comb:  {comb:6d}  ({comb/total*100:5.1f}%)")
            if other > 0:
                print(f"  other: {other:6d}  ({other/total*100:5.1f}%)")

        abc_area, ff_area = result.get("abc_area", 0), result.get("ff_area", 0)
        total_area = result.get("total_area", 0)

        if total_area > 0:
            print(f"  area: {abc_area} logic + {ff_area} FF = "
                  f"{total_area} ({result['ff_area_pct']:.1f}% FF)")

        if self._verbose:
            for cat in ("seq", "comb", "other"):
                by_type = result.get(f"{cat}_types", {})
                if by_type:
                    print(f"  {cat} details:")
                    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
                        print(f"    {c:6d}  {t}")
        print()

    def out(self):
        pass


class CsvOut:
    def __init__(self):
        self._time = {}
        self._memory = {}
        self._cells = {}

    def add(self, yosys_bin, tag, result):
        match = re.search(r"user ([\d.]+)s system ([\d.]+)s, MEM: ([\d.]+) MB", result)
        if not match:
            print(f"Unexpected formatting of \"{result}\"", file=sys.stderr)
            return
        user, system, mem = match.groups()
        self._time[yosys_bin, tag] = float(user) + float(system)
        self._memory[yosys_bin, tag] = float(mem)

    def add_stats(self, stats, yosys_bins):
        design = stats.get("design", "unknown")
        yosys = stats.get("yosys", "yosys")
        t = stats.get("time")
        if t is not None:
            self._time[Path(yosys), design] = t
        mem = stats.get("mem_mb")
        if mem is not None:
            self._memory[Path(yosys), design] = mem
        cells = stats.get("cells")
        if cells is not None:
            self._cells[Path(yosys), design] = cells

    def add_ff(self, result):
        pass

    def out(self):
        if not self._time and not self._memory:
            return
        yosyes = sorted({ys for ys, _ in self._time.keys()})
        designs = sorted({d for _, d in self._time.keys()})
        if not yosyes:
            return

        ys_root = common_parent([Path(str(y)) for y in yosyes])

        def header():
            print("design", end=";")
            for ys in yosyes:
                print(Path(str(ys)).relative_to(ys_root), end=";")
            print()

        for label, data, fmt in [
            ("time", self._time, lambda v: f"{v:.2f}"),
            ("memory", self._memory, lambda v: f"{v:.1f}"),
            ("cells", self._cells, lambda v: f"{v}"),
        ]:
            if not data:
                continue
            print(label)
            header()
            for design in designs:
                print(design, end=";")
                for ys in yosyes:
                    v = data.get((ys, design))
                    print(fmt(v) if v is not None else "", end=";")
                print()
            print()


yosys_log_end = re.compile("End of script.*")


def run_mode_basic(out, mode, design, synth_mode, yosys, params):
    design_name, design_class = design
    tag = tag_for(design_name, params)
    ap = artifact_path_for(tag)
    read_sv_ys = design_class.sv(params)
    synth_ys = synth_mode.value

    match mode:
        case RunMode.ARTIFACT: script = f"{read_sv_ys}\nwrite_rtlil {ap}"
        case RunMode.VERILOG:  script = f"{read_sv_ys}\n{synth_ys}"
        case RunMode.SYNTH:    script = f"read_rtlil {ap}\n{synth_ys}"

    log = r([str(yosys), "-p", script])
    m = yosys_log_end.search(log)
    out.add(yosys, f"{tag}-{synth_mode.name}", m.group(0) if m else "(no end-of-script marker)")


def run_mode_analyze(yosys_bin, tag, flow, detailed_timing=False):
    ap = artifact_path_for(tag)
    assert ap.exists(), f"Artifact not found: {ap}"

    synth_cmd = "synth -flatten -noabc" if flow == "flatten" else "synth -noabc"
    script = (
        f"read_rtlil {ap}; {synth_cmd}; "
        f"abc -g AND,NAND,OR,NOR,XOR,XNOR,ANDNOT,ORNOT,MUX "
        f"-script +print_stats; stat"
    )

    output = run_yosys(yosys_bin, script, detailed_timing=detailed_timing)
    stats = parse_stat(output)
    stats["design"] = tag
    stats["yosys"] = str(yosys_bin)
    if detailed_timing:
        stats["pass_timing"] = parse_detailed_timing(output)
    return stats


def run_mode_ff(yosys_bin, tag, cell_cats, ff_size=6):
    ap = artifact_path_for(tag)
    assert ap.exists(), f"Artifact not found: {ap}"

    script = (
        f"read_rtlil {ap}; "
        f"synth -flatten -noabc; "
        f"stat; "
        f"abc -g AND,NAND,OR,NOR,XOR,XNOR,ANDNOT,ORNOT,MUX -script +print_stats"
    )

    output = run_yosys(yosys_bin, script)
    stats = parse_stat(output)
    cells_breakdown = stats.get("cells_breakdown", {})

    totals, by_type = classify_cells(cells_breakdown, cell_cats)
    total_cells = sum(totals.values())
    seq_count = totals["seq"]

    abc_area = parse_abc_area(output)
    ff_area = seq_count * ff_size
    total_area = abc_area + ff_area

    return {
        "design": tag,
        "seq": seq_count, "mem": totals["mem"],
        "comb": totals["comb"], "other": totals["other"],
        "total": total_cells,
        "ratio": seq_count / total_cells if total_cells > 0 else 0.0,
        "abc_area": abc_area, "ff_area": ff_area,
        "total_area": total_area,
        "ff_area_pct": (ff_area / total_area * 100) if total_area > 0 else 0.0,
        "seq_types": by_type["seq"], "mem_types": by_type["mem"],
        "other_types": by_type["other"], "comb_types": by_type["comb"],
    }


def resolve_designs(args, mode):
    params = params_from_str(args.param)
    if mode in (RunMode.ARTIFACT, RunMode.VERILOG, RunMode.SYNTH):
        if args.auto:
            return [("jpeg", {}), ("ibex", {}), ("fft64", {"width": "64"})]
        assert args.design, "Missing --design without --auto"
        return [(args.design, params)]
    if args.design:
        return [(args.design, params)]
    discovered = discover_designs("artifacts")
    assert discovered, "No designs found in artifacts/"
    return [(d, {}) for d in discovered]


def single_run(out, mode, args, design_name, params):
    designs = design_map()
    assert design_name in designs, f"Design {design_name} not found in scripts module"
    assert mode == RunMode.SYNTH or args.flow == "", "--flow specified outside of synth mode"
    for yosys in args.yosys:
        run_mode_basic(out, mode, (design_name, designs[design_name]()), SynthMode.from_str(args.flow), yosys, params)


def main():
    parser = argparse.ArgumentParser(description="Yosys design profiling and analysis")
    parser.add_argument("mode", choices=[m.value for m in RunMode],
        help="artifact: generate .il; verilog: SV+synth; synth: .il+synth; analyze: profiling; ff: FF/area analysis")
    parser.add_argument("--yosys", default=[Path("yosys")], type=Path, nargs="+", help="Yosys binary path(s)")
    parser.add_argument("--design", type=str, help="Design name (default: all in artifacts/ for analyze/ff)")
    parser.add_argument("--flow", default="", type=str, help="Synthesis flow: '' or 'flatten'")
    parser.add_argument("--param", nargs="*", default=[], help="Design parameters (e.g. width=64)")
    parser.add_argument("--auto", action="store_true", help="Run predefined designs (artifact/verilog/synth)")
    parser.add_argument("--output", choices=list(map(str, OutputMode)), default=OutputMode.HUMAN, help="Output format")
    parser.add_argument("-d", "--detailed-timing", action="store_true", help="Per-pass timing (analyze mode)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all passes / cell details")
    parser.add_argument("--ff-size", type=int, default=6, help="AIG-equivalent size per FF (default: 6)")

    args = parser.parse_args()
    mode = RunMode(args.mode)
    out = CsvOut() if OutputMode(args.output) == OutputMode.CSV else HumanOut(verbose=args.verbose)

    if mode in (RunMode.ARTIFACT, RunMode.VERILOG, RunMode.SYNTH):
        if mode == RunMode.ARTIFACT:
            Path("canon").mkdir(parents=True, exist_ok=True)
            with open(Path("canon") / "ys-version", 'w') as f:
                arg = "--git-hash"
                try:
                    r([str(args.yosys[0]), "--git-hash"], stderr=subprocess.STDOUT)
                except subprocess.CalledProcessError:
                    arg = "--version"
                subprocess.run([str(args.yosys[0]), arg], stdout=f)

        for design_name, params in resolve_designs(args, mode):
            single_run(out, mode, args, design_name, params)
        out.out()
        return

    if mode == RunMode.ANALYZE:
        all_stats = []
        for yosys_bin in args.yosys:
            for design_name, params in resolve_designs(args, mode):
                stats = run_mode_analyze(yosys_bin, tag_for(design_name, params), args.flow,
                                         detailed_timing=args.detailed_timing)
                if stats:
                    all_stats.append(stats)
        assert all_stats, "No results"
        for s in all_stats:
            out.add_stats(s, args.yosys)
        out.out()
        return

    if mode == RunMode.FF:
        cell_cats = dump_cell_groups(args.yosys[0])
        results = []
        for design_name, params in resolve_designs(args, mode):
            result = run_mode_ff(args.yosys[0], tag_for(design_name, params),
                                 cell_cats=cell_cats, ff_size=args.ff_size)
            results.append(result)
        assert results, "No results"

        if OutputMode(args.output) == OutputMode.CSV:
            print("design;seq;comb;other;total;ff_ratio;abc_area;ff_area;total_area;ff_area_pct")
            for res in results:
                print(f"{res['design']};{res['seq']};{res['comb']};"
                      f"{res['other']};{res['total']};{res['ratio']:.4f};"
                      f"{res['abc_area']};{res['ff_area']};"
                      f"{res['total_area']};{res['ff_area_pct']:.2f}")
        else:
            print(f"(ff_size={args.ff_size})")
            for res in results:
                out.add_ff(res)


if __name__ == "__main__":
    main()
