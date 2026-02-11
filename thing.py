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
        if s == "":
            return SynthMode.SYNTH
        elif s == "flatten":
            return SynthMode.SYNTH_FLATTEN
        else:
            print(f"Invalid synthesis flow: {s}", file=sys.stderr)
            sys.exit(1)


def common_parent(paths):
    if not paths:
        return Path(".")
    zipped = zip(*(p.parts for p in paths))
    common_parts = []
    for parts in zipped:
        if len(set(parts)) > 1:
            break
        common_parts.append(parts[0])
    return Path(*common_parts) if common_parts else Path(".")


def fmt_params(params):
    if not params:
        return ""
    s = ""
    first = True
    for key, value in sorted(params.items()):
        if not first:
            s += "__"
        s += f"{key}_{value}"
        first = False
    return s


def design_map():
    """Get available designs from scripts module."""
    if not HAS_SCRIPTS:
        return {}
    d = {}
    for _, modname, ispkg in pkgutil.iter_modules(scripts.__path__):
        if not ispkg:
            module = importlib.import_module(f"scripts.{modname}")
            if hasattr(module, "__all__"):
                for c in module.__all__:
                    d[c.__name__.lower()] = c
    return d


def discover_designs(artifacts_dir):
    """Discover designs from .il files in artifacts directory."""
    designs = []
    p = Path(artifacts_dir)
    if p.exists():
        for f in sorted(p.glob("*.il")):
            designs.append(f.stem)
    return designs


def params_from_str(pairs):
    ret = {}
    for pair in pairs:
        key_value = pair.split("=")
        if len(key_value) != 2:
            print(f"Can't parse {pair} as parameter", file=sys.stderr)
            sys.exit(1)
        lhs, rhs = key_value
        ret[lhs] = rhs
    return ret


def tag_for(design, params):
    return f"{design}_{fmt_params(params)}" if params else design


def artifact_path_for(tag):
    return Path("artifacts") / f"{tag}.il"


# Sequential logic group
SEQ_GROUPS = {"reg", "reg_ff", "reg_latch"}
# Memory groups
MEM_GROUPS = {"mem"}


def load_cell_groups(json_path):
    """
    Parse the JSON produced by `help -dump-cells-json`.
    Returns a dict mapping cell type name -> category string.
    """
    with open(json_path) as f:
        data = json.load(f)

    groups = data.get("groups", {})
    cats = {}

    for group_name, types in groups.items():
        if group_name in SEQ_GROUPS:
            cat = "seq"
        elif group_name in MEM_GROUPS:
            cat = "mem"
        else:
            cat = "comb"

        for t in types:
            cats[t] = cat

    return cats


def dump_cell_groups(yosys_bin):
    """
    Run `help -dump-cells-json` via Yosys and return cell classification dict.
    """
    with tempfile.NamedTemporaryFile(mode='w+', suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        cmd = [str(yosys_bin), "-p", f"help -dump-cells-json {tmp_path}"]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        return load_cell_groups(tmp_path)
    except subprocess.CalledProcessError as e:
        print(f"Warning: Failed to run Yosys cell dump ({e}). "
              "Cell classification will be incomplete.", file=sys.stderr)
        return {}
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not parse Yosys cell dump ({e}).", file=sys.stderr)
        return {}
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def classify_cells(cells_breakdown, cell_cats):
    """
    Given a dict of {cell_type: count} from stat output and a
    classification dict from dump_cell_groups, return category totals
    and per-type breakdown.

    Returns (totals, by_type) where:
      totals = {"seq": N, "mem": N, "comb": N, "other": N}
      by_type = {"seq": {type: count, ...}, ...}
    """
    totals = {"seq": 0, "mem": 0, "comb": 0, "other": 0}
    by_type = {"seq": {}, "mem": {}, "comb": {}, "other": {}}

    for cell_type, count in cells_breakdown.items():
        cat = cell_cats.get(cell_type, "other")

        totals[cat] += count
        by_type[cat][cell_type] = count

    return totals, by_type


def run_yosys(yosys_bin, script, detailed_timing=False):
    """Run Yosys with the given script, return combined stdout+stderr."""
    cmd = [str(yosys_bin)]
    if detailed_timing:
        cmd.append("-d")
    cmd.extend(["-p", script])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout + result.stderr


def parse_stat(output):
    """Parse `stat` output for cell counts, memory, timing footer."""
    stats = {}

    # Find the statistics block
    match = re.search(
        r'\+----------Count including submodules\.\s*\|\s*(.*?)(?:End of script|$)',
        output, re.DOTALL
    )
    if not match:
        match = re.search(
            r'Printing statistics\.\s*(.*?)(?:End of script|$)',
            output, re.DOTALL
        )

    if match:
        summary = match.group(1)

        for name, pattern in [
            ("wires", r'^\s*Number of wires:\s+(\d+)\s*$'),
            ("wire_bits", r'^\s*Number of wire bits:\s+(\d+)\s*$'),
            ("public_wires", r'^\s*Number of public wires:\s+(\d+)\s*$'),
            ("public_wire_bits", r'^\s*Number of public wire bits:\s+(\d+)\s*$'),
            ("ports", r'^\s*Number of ports:\s+(\d+)\s*$'),
            ("port_bits", r'^\s*Number of port bits:\s+(\d+)\s*$'),
            ("memories", r'^\s*Number of memories:\s+(\d+)\s*$'),
            ("memory_bits", r'^\s*Number of memory bits:\s+(\d+)\s*$'),
            ("processes", r'^\s*Number of processes:\s+(\d+)\s*$'),
            ("cells", r'^\s*Number of cells:\s+(\d+)\s*$'),
        ]:
            m = re.search(pattern, summary, re.MULTILINE)
            if m:
                stats[name] = int(m.group(1))
            else:
                # Fallback for short format ("   12 wires")
                short_pattern = pattern.replace(r"Number of ", r"").replace(r":\s+", r"\s+")
                m = re.search(short_pattern, summary, re.MULTILINE)
                if m:
                    stats[name] = int(m.group(1))

        # Capture specific cell usage ("   $_DFF_P_       4")
        cells = {}
        for m in re.finditer(r'^\s+(\S+)\s+(\d+)\s*$', summary, re.MULTILINE):
            name = m.group(1)
            count = int(m.group(2))
            cells[name] = count

        stats["cells_breakdown"] = cells

    # CPU/MEM
    m = re.search(
        r'CPU:\s*user\s+([\d.]+)s\s+system\s+([\d.]+)s.*?MEM:\s*([\d.]+)\s*MB',
        output
    )
    if m:
        stats["user_time"] = float(m.group(1))
        stats["sys_time"] = float(m.group(2))
        stats["time"] = stats["user_time"] + stats["sys_time"]
        stats["mem_mb"] = float(m.group(3))

    # "Time spent"
    m = re.search(r'Time spent:\s*(.+?)(?:\n|$)', output)
    if m:
        top_times = []
        for pm in re.finditer(
            r'(\d+)%\s+(\d+)x\s+(\w+)\s*\((\d+)\s*sec\)', m.group(1)
        ):
            top_times.append((
                pm.group(3),
                int(pm.group(1)),
                int(pm.group(2)),
                int(pm.group(4)),
            ))
        stats["top_times"] = top_times

    # ABC stats
    for m in re.finditer(r'ABC:.*?nd\s*=\s*(\d+).*?lev\s*=\s*(\d+)', output):
        stats["abc_nd"] = stats.get("abc_nd", 0) + int(m.group(1))
        lev = int(m.group(2))
        if lev > stats.get("abc_lev", 0):
            stats["abc_lev"] = lev

    return stats


def parse_detailed_timing(output):
    """
    Parse Yosys -d output.
    """
    pass_times = []

    for pm in re.finditer(
        r'^\s*(\d+)%\s+(\d+)\s+calls?\s+([\d.]+)\s+sec\s+(\S+)',
        output, re.MULTILINE
    ):
        count = int(pm.group(2))
        secs = float(pm.group(3))
        name = pm.group(4)
        pass_times.append((name, secs, count))

    return sorted(pass_times, key=lambda x: -x[1])


def format_pass_timing(pass_times, top_n=10):
    """Format pass timing for display."""
    if not pass_times:
        return ""
    total = sum(t for _, t, _ in pass_times)
    lines = []

    show = pass_times if top_n is None else pass_times[:top_n]
    for name, secs, count in show:
        pct = (secs / total * 100) if total > 0 else 0
        count_str = f" ({count}x)" if count > 1 else ""
        lines.append(f"    {pct:5.1f}%  {secs:6.3f}s  {name}{count_str}")

    if top_n is not None and len(pass_times) > top_n:
        shown = sum(t for _, t, _ in pass_times[:top_n])
        other_pct = ((total - shown) / total * 100) if total > 0 else 0
        lines.append(
            f"    {other_pct:5.1f}%  ... {len(pass_times) - top_n} more passes"
        )

    return '\n'.join(lines)


def parse_abc_area(output):
    """Parse ABC 'and = N' from print_stats output."""
    abc_area = 0
    for m in re.finditer(r'\band\s*=\s*(\d+)', output):
        abc_area += int(m.group(1))
    return abc_area


class HumanOut:
    def __init__(self, verbose=False):
        self._verbose = verbose


    def add(self, yosys_bin, tag, result):
        print(f"{yosys_bin}: {tag}")
        print(result)


    def add_stats(self, stats, yosys_bins):
        """Print detailed stats (analyze mode)."""
        design = stats.get("design", "unknown")
        yosys = Path(stats.get("yosys", "yosys")).name

        prefix = f"{yosys}: {design}" if len(yosys_bins) > 1 else design
        user = stats.get("user_time")
        sys_t = stats.get("sys_time")
        mem = stats.get("mem_mb")
        if user is not None:
            print(f"{prefix}: user {user:.2f}s system {sys_t:.2f}s, "
                  f"MEM: {mem:.2f} MB")
        else:
            print(f"{prefix}:")

        abc_nd = stats.get("abc_nd")
        abc_lev = stats.get("abc_lev")
        if abc_nd:
            print(f"  ABC: {abc_nd} nodes, {abc_lev} levels")

        pass_timing = stats.get("pass_timing", [])
        if pass_timing:
            top_n = None if self._verbose else 10
            print(f"  Pass timing:")
            print(format_pass_timing(pass_timing, top_n=top_n))
        else:
            top_times = stats.get("top_times", [])
            if top_times:
                parts = [
                    f"{pct}% {name} ({count}x)"
                    for name, pct, count, secs in top_times
                ]
                print(f"  Top time: {', '.join(parts)}")

        print()


    def add_ff(self, result):
        """Print cell classification result."""
        total = result["total"]
        seq = result["seq"]
        comb = result["comb"]
        other = result["other"]

        print(f"{result['design']}: {total} cells")
        if total > 0:
            print(f"  seq:   {seq:6d}  ({seq/total*100:5.1f}%)")
            print(f"  comb:  {comb:6d}  ({comb/total*100:5.1f}%)")
            if other > 0:
                print(f"  other: {other:6d}  ({other/total*100:5.1f}%)")

        abc_area = result.get("abc_area", 0)
        ff_area = result.get("ff_area", 0)
        total_area = result.get("total_area", 0)
        ff_area_pct = result.get("ff_area_pct", 0)

        if total_area > 0:
            print(f"  area: {abc_area} logic + {ff_area} FF = "
                  f"{total_area} ({ff_area_pct:.1f}% FF)")

        if self._verbose:
            for cat in ("seq", "comb", "other"):
                by_type = result.get(f"{cat}_types", {})
                if by_type:
                    print(f"  {cat} details:")
                    sorted_types = sorted(
                        by_type.items(), key=lambda x: -x[1]
                    )
                    for t, c in sorted_types:
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
        match = re.search(
            r"user ([\d.]+)s system ([\d.]+)s, MEM: ([\d.]+) MB", result
        )
        if not match:
            print(f"Unexpected formatting of \"{result}\"", file=sys.stderr)
            return
        user, system, mem = match.groups()
        self._time[yosys_bin, tag] = float(user) + float(system)
        self._memory[yosys_bin, tag] = float(mem)


    def add_stats(self, stats, yosys_bins):
        """Collect stats for CSV output."""
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
        """Collect FF result for CSV output."""
        pass


    def out(self):
        if not self._time and not self._memory:
            return

        yosyes = sorted({ys for ys, _ in self._time.keys()})
        designs = sorted({d for _, d in self._time.keys()})

        if not yosyes:
            return

        ys_root = common_parent([Path(str(y)) for y in yosyes])

        def header(yosyes):
            print("design", end=";")
            for ys in yosyes:
                print(Path(str(ys)).relative_to(ys_root), end=";")
            print()

        print("time")
        header(yosyes)
        for design in designs:
            print(design, end=";")
            for ys in yosyes:
                t = self._time.get((ys, design))
                print(f"{t:.2f}" if t else "", end=";")
            print()

        print()
        print("memory")
        header(yosyes)
        for design in designs:
            print(design, end=";")
            for ys in yosyes:
                m = self._memory.get((ys, design))
                print(f"{m:.1f}" if m else "", end=";")
            print()

        if self._cells:
            print()
            print("cells")
            header(yosyes)
            for design in designs:
                print(design, end=";")
                for ys in yosyes:
                    c = self._cells.get((ys, design))
                    print(f"{c}" if c else "", end=";")
                print()


yosys_log_end = re.compile("End of script.*")


def run_mode_basic(out, mode, design, synth_mode, yosys, params):
    """Run artifact/verilog/synth mode"""
    design_name, design_class = design
    tag = tag_for(design_name, params)
    ap = artifact_path_for(tag)

    read_sv_ys = design_class.sv(params)
    synth_ys = synth_mode.value

    if mode == RunMode.ARTIFACT:
        script = f"{read_sv_ys}\nwrite_rtlil {ap}"
    elif mode == RunMode.VERILOG:
        script = f"{read_sv_ys}\n{synth_ys}"
    elif mode == RunMode.SYNTH:
        script = f"read_rtlil {ap}\n{synth_ys}"
    else:
        assert False, f"unexpected mode {mode}"

    log = r([str(yosys), "-p", script])
    m = yosys_log_end.search(log)
    if m:
        res = m.group(0)
    else:
        res = "(no end-of-script marker found)"
    out.add(yosys, f"{tag}-{synth_mode.name}", res)


def run_mode_analyze(yosys_bin, tag, flow, detailed_timing=False):
    """
    Run analyze mode: synthesize from artifact and collect detailed stats.
    """
    ap = artifact_path_for(tag)
    if not ap.exists():
        print(f"Artifact not found: {ap}", file=sys.stderr)
        return None

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


def run_mode_ff(yosys_bin, tag, flow, cell_cats, ff_size=6):
    ap = artifact_path_for(tag)
    if not ap.exists():
        print(f"Artifact not found: {ap}", file=sys.stderr)
        return None

    synth_cmd = "synth -flatten -noabc" if flow == "flatten" else "synth -noabc"

    script = (
        f"read_rtlil {ap}\n"
        f"{synth_cmd}\n"
        f"stat\n"
        f"abc -script +strash;print_stats\n"
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
    ff_area_pct = (ff_area / total_area * 100) if total_area > 0 else 0.0

    return {
        "design": tag,
        "seq": seq_count,
        "mem": totals["mem"],
        "comb": totals["comb"],
        "other": totals["other"],
        "total": total_cells,
        "ratio": seq_count / total_cells if total_cells > 0 else 0.0,
        "abc_area": abc_area,
        "ff_area": ff_area,
        "total_area": total_area,
        "ff_area_pct": ff_area_pct,
        "seq_types": by_type["seq"],
        "mem_types": by_type["mem"],
        "other_types": by_type["other"],
        "comb_types": by_type["comb"],
    }


def resolve_designs(args, mode):
    """
    Resolve the list of (design_name, params) to run.
    """
    params = params_from_str(args.param)

    if mode in (RunMode.ARTIFACT, RunMode.VERILOG, RunMode.SYNTH):
        if args.auto:
            return [
                ("jpeg", {}),
                ("ibex", {}),
                ("fft64", {"width": "64"}),
            ]
        elif args.design:
            return [(args.design, params)]
        else:
            print("Missing --design without --auto", file=sys.stderr)
            sys.exit(1)
    else:
        if args.design:
            return [(args.design, params)]
        else:
            discovered = discover_designs("artifacts")
            if not discovered:
                print("No designs found in artifacts/", file=sys.stderr)
                sys.exit(1)
            return [(d, {}) for d in discovered]


def single_run(out, mode, args, design_name, params):
    """Execute a single design in artifact/verilog/synth mode."""
    designs = design_map()
    if design_name not in designs:
        print(f"Design {design_name} not found in scripts module",
              file=sys.stderr)
        return

    if mode != RunMode.SYNTH and args.flow != "":
        print("--flow specified outside of synth mode", file=sys.stderr)
        sys.exit(1)

    for yosys in args.yosys:
        run_mode_basic(
            out, mode,
            (design_name, designs[design_name]()),
            SynthMode.from_str(args.flow),
            yosys,
            params,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Yosys design profiling and analysis"
    )
    parser.add_argument(
        "mode",
        choices=[m.value for m in RunMode],
        help=(
            "artifact: generate .il from Verilog; "
            "verilog: read Verilog + synth; "
            "synth: read .il + synth; "
            "analyze: detailed profiling (timing, cells, ABC); "
            "ff: FF count and area analysis"
        ),
    )
    parser.add_argument(
        "--yosys", default=[Path("yosys")], type=Path, nargs="+",
        help="Path to the Yosys binary (multiple for comparison)",
    )
    parser.add_argument(
        "--design", type=str,
        help="Specific design name (default: all in artifacts/ for "
             "analyze/ff modes)",
    )
    parser.add_argument(
        "--flow", default="", type=str,
        help="Synthesis flow variant: '' (default) or 'flatten'",
    )
    parser.add_argument(
        "--param", nargs="*", default=[],
        help="Design parameters (e.g., width=64)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Run predefined set of designs (artifact/verilog/synth modes)",
    )
    parser.add_argument(
        "--output", choices=list(map(str, OutputMode)),
        default=OutputMode.HUMAN,
        help="Output format: human (default) or csv",
    )
    parser.add_argument(
        "-d", "--detailed-timing", action="store_true",
        help="Per-pass CPU timing via Yosys -d flag (analyze mode)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show all passes in timing output (not just top 10)",
    )
    parser.add_argument(
        "--ff-size", type=int, default=6,
        help="AIG-equivalent size per FF for area estimate (ff mode, "
             "default: 6)",
    )

    args = parser.parse_args()
    mode = RunMode(args.mode)
    out_mode = OutputMode(args.output)

    if out_mode == OutputMode.CSV:
        out = CsvOut()
    else:
        out = HumanOut(verbose=args.verbose)

    if mode in (RunMode.ARTIFACT, RunMode.VERILOG, RunMode.SYNTH):
        if mode == RunMode.ARTIFACT:
            Path("canon").mkdir(parents=True, exist_ok=True)
            with open(Path("canon") / "ys-version", 'w') as f:
                arg = "--git-hash"
                try:
                    r([str(args.yosys[0]), "--git-hash"],
                      stderr=subprocess.STDOUT)
                except subprocess.CalledProcessError:
                    arg = "--version"
                subprocess.run([str(args.yosys[0]), arg], stdout=f)

        design_list = resolve_designs(args, mode)
        for design_name, params in design_list:
            single_run(out, mode, args, design_name, params)

        out.out()
        return

    if mode == RunMode.ANALYZE:
        design_list = resolve_designs(args, mode)
        all_stats = []

        for yosys_bin in args.yosys:
            for design_name, params in design_list:
                tag = tag_for(design_name, params)
                stats = run_mode_analyze(
                    yosys_bin, tag, args.flow,
                    detailed_timing=args.detailed_timing,
                )
                if stats:
                    all_stats.append(stats)

        if not all_stats:
            print("No results", file=sys.stderr)
            sys.exit(1)

        if out_mode == OutputMode.CSV:
            for s in all_stats:
                out.add_stats(s, args.yosys)
            out.out()
        else:
            for s in all_stats:
                out.add_stats(s, args.yosys)
        return

    if mode == RunMode.FF:
        cell_cats = dump_cell_groups(args.yosys[0])

        if not cell_cats:
            print("Warning: Cell classification failed. All cells will be 'other'.",
                  file=sys.stderr)

        design_list = resolve_designs(args, mode)
        results = []

        for design_name, params in design_list:
            tag = tag_for(design_name, params)
            result = run_mode_ff(
                args.yosys[0], tag, args.flow,
                cell_cats=cell_cats,
                ff_size=args.ff_size,
            )
            if result:
                results.append(result)

        if not results:
            print("No results", file=sys.stderr)
            sys.exit(1)

        if out_mode == OutputMode.CSV:
            print("design;seq;comb;other;total;ff_ratio;"
                  "abc_area;ff_area;total_area;ff_area_pct")
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
