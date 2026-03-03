from enum import StrEnum
from pathlib import Path
import argparse, pkgutil, importlib, subprocess, functools
import tempfile, json, re, sys, os

try:
    import scripts
except ImportError:
    scripts = None

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
            case _:         assert False, f"invalid flow: {s}"


ABC_GATES = "AND,NAND,OR,NOR,XOR,XNOR,ANDNOT,ORNOT,MUX"
SEQ_GROUPS = {"reg", "reg_ff", "reg_latch"}
MEM_GROUPS = {"mem"}


def common_parent(paths):
    common = []
    for parts in zip(*(p.parts for p in paths)):
        if len(set(parts)) > 1: break
        common.append(parts[0])
    return Path(*common) if common else Path(".")


def fmt_params(params):
    return "__".join(f"{k}_{v}" for k, v in sorted(params.items())) if params else ""


def tag_for(design, params):
    fp = fmt_params(params)
    return f"{design}_{fp}" if fp else design


def artifact_path(tag):
    return Path("artifacts") / f"{tag}.il"


def design_map():
    if scripts is None: return {}
    d = {}
    for _, modname, ispkg in pkgutil.iter_modules(scripts.__path__):
        if ispkg: continue
        mod = importlib.import_module(f"scripts.{modname}")
        for c in getattr(mod, "__all__", []):
            d[c.__name__.lower()] = c
    return d


def discover_designs():
    return sorted(f.stem for f in Path("artifacts").glob("*.il"))


def params_from_str(pairs):
    ret = {}
    for pair in pairs:
        kv = pair.split("=")
        assert len(kv) == 2, f"bad param: {pair}"
        ret[kv[0]] = kv[1]
    return ret


def load_cell_groups(json_path):
    with open(json_path) as f:
        groups = json.load(f).get("groups", {})
    cats = {}
    for gname, types in groups.items():
        cat = "seq" if gname in SEQ_GROUPS else "mem" if gname in MEM_GROUPS else "comb"
        for t in types: cats[t] = cat
    return cats


def dump_cell_groups(yosys_bin):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run([str(yosys_bin), "-p", f"help -dump-cells-json {tmp_path}"],
                       capture_output=True, text=True, check=True)
        return load_cell_groups(tmp_path)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: cell dump failed ({e}).", file=sys.stderr)
        return {}
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


def classify_cells(breakdown, cell_cats):
    cats = ("seq", "mem", "comb", "other")
    totals = {c: 0 for c in cats}
    by_type = {c: {} for c in cats}
    for typ, count in breakdown.items():
        cat = cell_cats.get(typ, "other")
        totals[cat] += count
        by_type[cat][typ] = count
    return totals, by_type


def run_yosys(yosys_bin, script, detailed_timing=False):
    cmd = [str(yosys_bin)] + (["-d"] if detailed_timing else []) + ["-p", script]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout + result.stderr


def synth_and_abc(yosys_bin, tag, flow="flatten", detailed_timing=False):
    """common path for analyze and ff: read artifact, synth -noabc, abc, stat"""
    ap = artifact_path(tag)
    assert ap.exists(), f"artifact not found: {ap}"
    synth_cmd = "synth -flatten -noabc" if flow == "flatten" else "synth -noabc"
    return run_yosys(yosys_bin,
        f"read_rtlil {ap}; {synth_cmd}; stat; abc -g {ABC_GATES} -script +print_stats",
        detailed_timing=detailed_timing)


_STAT_FIELDS = [
    "wires", "wire bits", "public wires", "public wire bits",
    "ports", "port bits", "memories", "memory bits", "processes",
]


def parse_stat(output):
    """parse yosys `stat`"""
    stats = {}

    # new format
    cells_matches = list(re.finditer(r'^\s+(\d+)\s+cells\s*$', output, re.MULTILINE))
    if cells_matches:
        last = cells_matches[-1]
        stats["cells"] = int(last.group(1))
        cells = {}
        for line in output[last.end():].split('\n'):
            m = re.match(r'^\s+(\d+)\s{3,}(\S+)\s*$', line)
            if m:     cells[m.group(2)] = int(m.group(1))
            elif line.strip(): break
        stats["cells_breakdown"] = cells
        block_start = max(output.rfind('+---', 0, last.start()), last.start() - 2000, 0)
        block = output[block_start:last.start()]
        for field in _STAT_FIELDS:
            m = re.search(rf'^\s+(\d+)\s+{re.escape(field)}\s*$', block, re.MULTILINE)
            if m: stats[field.replace(' ', '_')] = int(m.group(1))
    else:
        # old format
        match = re.search(
            r'(?:Printing statistics|Count including submodules).*?\n(.*?)(?=End of script|\Z)',
            output, re.DOTALL | re.IGNORECASE)
        summary = match.group(1) if match else ""
        for field in _STAT_FIELDS + ["cells"]:
            m = re.search(rf'Number of {field}:\s+(\d+)', summary, re.IGNORECASE)
            if m: stats[field] = int(m.group(1))
        cells = {}
        cs = re.search(r'Number of cells:\s+\d+\s*\n((?:\s+\S+\s+\d+\s*\n)*)', summary, re.IGNORECASE)
        if cs:
            for m in re.finditer(r'^\s+(\S+)\s+(\d+)\s*$', cs.group(1), re.MULTILINE):
                cells[m.group(1)] = int(m.group(2))
        stats["cells_breakdown"] = cells

    # TODO parse wall time when available (yosys#5708)
    m = re.search(r'Wall:\s*([\d.]+)s', output)
    if m:
        stats["wall_time"] = float(m.group(1))
        stats["time"] = stats["wall_time"]

    m = re.search(r'CPU:\s*user\s+([\d.]+)s\s+system\s+([\d.]+)s.*?MEM:\s*([\d.]+)\s*MB', output)
    if m:
        stats["user_time"] = float(m.group(1))
        stats["sys_time"] = float(m.group(2))
        if "time" not in stats:
            stats["time"] = stats["user_time"] + stats["sys_time"]
        stats["mem_mb"] = float(m.group(3))

    m = re.search(r'Time spent:\s*(.+?)(?:\n|$)', output)
    if m:
        stats["top_times"] = [
            (pm.group(3), int(pm.group(1)), int(pm.group(2)), int(pm.group(4)))
            for pm in re.finditer(r'(\d+)%\s+(\d+)x\s+(\w+)\s*\((\d+)\s*sec\)', m.group(1))]

    for m in re.finditer(r'ABC:.*?nd\s*=\s*(\d+).*?lev\s*=\s*(\d+)', output):
        stats["abc_nd"] = stats.get("abc_nd", 0) + int(m.group(1))
        lev = int(m.group(2))
        if lev > stats.get("abc_lev", 0): stats["abc_lev"] = lev

    return stats


def parse_detailed_timing(output):
    return sorted(
        [(m.group(4), float(m.group(3)), int(m.group(2)))
         for m in re.finditer(r'^\s*(\d+)%\s+(\d+)\s+calls?\s+([\d.]+)\s+sec\s+(\S+)', output, re.MULTILINE)],
        key=lambda x: -x[1])


def format_pass_timing(pass_times, top_n=10):
    if not pass_times: return ""
    total = sum(t for _, t, _ in pass_times)
    show = pass_times if top_n is None else pass_times[:top_n]
    lines = [f"    {s/total*100 if total else 0:5.1f}%  {s:6.3f}s  {n}{f' ({c}x)' if c > 1 else ''}"
             for n, s, c in show]
    if top_n and len(pass_times) > top_n:
        rest = total - sum(t for _, t, _ in show)
        lines.append(f"    {rest/total*100 if total else 0:5.1f}%  ... {len(pass_times) - top_n} more passes")
    return '\n'.join(lines)


def parse_abc_area(output):
    return sum(int(m.group(1)) for m in re.finditer(r'\bnd\s*=\s*(\d+)', output))


class HumanOut:
    def __init__(self, verbose=False): self._verbose = verbose
    def out(self): pass

    def add(self, yosys_bin, tag, result):
        print(f"{yosys_bin}: {tag}\n{result}")

    def add_stats(self, stats, yosys_bins):
        design = stats.get("design", "?")
        yosys = Path(stats.get("yosys", "yosys")).name
        prefix = f"{yosys}: {design}" if len(yosys_bins) > 1 else design

        if (u := stats.get("user_time")) is not None:
            cpu = f"user {u:.2f}s system {stats['sys_time']:.2f}s"
            if (w := stats.get("wall_time")) is not None:
                print(f"{prefix}: wall {w:.2f}s ({cpu}), MEM: {stats['mem_mb']:.2f} MB")
            else:
                print(f"{prefix}: CPU {cpu}, MEM: {stats['mem_mb']:.2f} MB")
        else:
            print(f"{prefix}:")

        if nd := stats.get("abc_nd"):
            print(f"  ABC: {nd} nodes, {stats.get('abc_lev', 0)} levels")

        if pt := stats.get("pass_timing"):
            print(f"  Pass timing:\n{format_pass_timing(pt, top_n=None if self._verbose else 10)}")
        elif tt := stats.get("top_times"):
            print(f"  Top time: {', '.join(f'{p}% {n} ({c}x)' for n, p, c, _ in tt)}")
        print()

    def add_ff(self, res):
        total, seq, comb, other = res["total"], res["seq"], res["comb"], res["other"]
        print(f"{res['design']}: {total} cells")
        if total:
            print(f"  seq:   {seq:6d}  ({seq/total*100:5.1f}%)")
            print(f"  comb:  {comb:6d}  ({comb/total*100:5.1f}%)")
            if other: print(f"  other: {other:6d}  ({other/total*100:5.1f}%)")

        abc, ff, ta = res.get("abc_area", 0), res.get("ff_area", 0), res.get("total_area", 0)
        if ta: print(f"  area: {abc} logic + {ff} FF = {ta} ({res['ff_area_pct']:.1f}% FF)")

        if self._verbose:
            for cat in ("seq", "comb", "other"):
                if bt := res.get(f"{cat}_types"):
                    print(f"  {cat} details:")
                    for t, c in sorted(bt.items(), key=lambda x: -x[1]):
                        print(f"    {c:6d}  {t}")
        print()


class CsvOut:
    def __init__(self):
        self._time, self._memory, self._cells = {}, {}, {}

    def add_ff(self, result): pass

    def add(self, yosys_bin, tag, result):
        m = re.search(r"user ([\d.]+)s system ([\d.]+)s, MEM: ([\d.]+) MB", result)
        assert m, f"unexpected format: {result}"
        self._time[yosys_bin, tag] = float(m.group(1)) + float(m.group(2))
        self._memory[yosys_bin, tag] = float(m.group(3))

    def add_stats(self, stats, yosys_bins):
        key = (Path(stats.get("yosys", "yosys")), stats.get("design", "?"))
        if (t := stats.get("time")) is not None: self._time[key] = t
        if (m := stats.get("mem_mb")) is not None: self._memory[key] = m
        if (c := stats.get("cells")) is not None: self._cells[key] = c

    def out(self):
        if not self._time: return
        yosyes = sorted({ys for ys, _ in self._time})
        designs = sorted({d for _, d in self._time})
        ys_root = common_parent([Path(str(y)) for y in yosyes])

        def header():
            print("design;" + ";".join(str(Path(str(ys)).relative_to(ys_root)) for ys in yosyes) + ";")

        for label, data, fmt in [("time", self._time, ".2f"), ("memory", self._memory, ".1f"),
                                  ("cells", self._cells, "")]:
            if not data: continue
            print(label); header()
            for d in designs:
                vals = ";".join(f"{data[(ys,d)]:{fmt}}" if (ys, d) in data else "" for ys in yosyes)
                print(f"{d};{vals};")
            print()


yosys_log_end = re.compile("End of script.*")


def run_mode_basic(out, mode, design, synth_mode, yosys, params):
    name, cls = design
    tag = tag_for(name, params)
    ap = artifact_path(tag)
    sv, syn = cls.sv(params), synth_mode.value

    match mode:
        case RunMode.ARTIFACT: script = f"{sv}\nwrite_rtlil {ap}"
        case RunMode.VERILOG:  script = f"{sv}\n{syn}"
        case RunMode.SYNTH:    script = f"read_rtlil {ap}\n{syn}"

    log = r([str(yosys), "-p", script])
    m = yosys_log_end.search(log)
    out.add(yosys, f"{tag}-{synth_mode.name}", m.group(0) if m else "(no end-of-script marker)")


def run_mode_analyze(yosys_bin, tag, flow, detailed_timing=False):
    output = synth_and_abc(yosys_bin, tag, flow=flow, detailed_timing=detailed_timing)
    stats = parse_stat(output)
    stats["design"] = tag
    stats["yosys"] = str(yosys_bin)
    if detailed_timing: stats["pass_timing"] = parse_detailed_timing(output)
    return stats


def run_mode_ff(yosys_bin, tag, cell_cats, ff_size=6):
    output = synth_and_abc(yosys_bin, tag, flow="flatten")
    breakdown = parse_stat(output).get("cells_breakdown", {})
    totals, by_type = classify_cells(breakdown, cell_cats)
    total_cells = sum(totals.values())
    seq = totals["seq"]
    abc_area = parse_abc_area(output)
    ff_area = seq * ff_size
    total_area = abc_area + ff_area
    return {
        "design": tag, "total": total_cells,
        "seq": seq, "mem": totals["mem"], "comb": totals["comb"], "other": totals["other"],
        "ratio": seq / total_cells if total_cells else 0.0,
        "abc_area": abc_area, "ff_area": ff_area, "total_area": total_area,
        "ff_area_pct": (ff_area / total_area * 100) if total_area else 0.0,
        **{f"{c}_types": by_type[c] for c in by_type},
    }


def resolve_designs(args, mode):
    params = params_from_str(args.param)
    if mode in (RunMode.ARTIFACT, RunMode.VERILOG, RunMode.SYNTH):
        if args.auto: return [("jpeg", {}), ("ibex", {}), ("fft64", {"width": "64"})]
        assert args.design, "need --design or --auto"
        return [(args.design, params)]
    if args.design: return [(args.design, params)]
    found = discover_designs()
    assert found, "no .il files in artifacts/"
    return [(d, {}) for d in found]


def run_basic_modes(out, mode, args, design_list):
    if mode == RunMode.ARTIFACT:
        with open(Path("canon") / "ys-version", 'w') as f:
            subprocess.run([str(args.yosys[0]), "--version"], stdout=f)

    designs = design_map()
    for design_name, params in design_list:
        assert design_name in designs, f"unknown design: {design_name}"
        assert mode == RunMode.SYNTH or args.flow == "", "--flow only valid for synth mode"
        for yosys in args.yosys:
            run_mode_basic(out, mode, (design_name, designs[design_name]()),
                           SynthMode.from_str(args.flow), yosys, params)
    out.out()


def run_analyze(out, args, design_list):
    stats = [run_mode_analyze(ys, tag_for(d, p), args.flow, detailed_timing=args.detailed_timing)
             for ys in args.yosys for d, p in design_list]
    stats = [s for s in stats if s]
    assert stats, "no results"
    for s in stats: out.add_stats(s, args.yosys)
    out.out()


def run_ff(out, args, design_list):
    cell_cats = dump_cell_groups(args.yosys[0])
    results = [run_mode_ff(args.yosys[0], tag_for(d, p), cell_cats=cell_cats, ff_size=args.ff_size)
               for d, p in design_list]
    assert results, "no results"
    if OutputMode(args.output) == OutputMode.CSV:
        print("design;seq;comb;other;total;ff_ratio;abc_area;ff_area;total_area;ff_area_pct")
        for res in results:
            print(f"{res['design']};{res['seq']};{res['comb']};{res['other']};{res['total']};"
                  f"{res['ratio']:.4f};{res['abc_area']};{res['ff_area']};{res['total_area']};{res['ff_area_pct']:.2f}")
    else:
        print(f"(ff_size={args.ff_size})")
        for res in results: out.add_ff(res)


def main():
    p = argparse.ArgumentParser(description="Yosys design profiling and analysis")
    p.add_argument("mode", choices=[m.value for m in RunMode])
    p.add_argument("--yosys", default=[Path("yosys")], type=Path, nargs="+")
    p.add_argument("--design", type=str)
    p.add_argument("--flow", default="", type=str)
    p.add_argument("--param", nargs="*", default=[])
    p.add_argument("--auto", action="store_true")
    p.add_argument("--output", choices=list(map(str, OutputMode)), default=OutputMode.HUMAN)
    p.add_argument("-d", "--detailed-timing", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--ff-size", type=int, default=6)
    args = p.parse_args()

    mode = RunMode(args.mode)
    out = CsvOut() if OutputMode(args.output) == OutputMode.CSV else HumanOut(verbose=args.verbose)
    design_list = resolve_designs(args, mode)

    match mode:
        case RunMode.ARTIFACT | RunMode.VERILOG | RunMode.SYNTH:
            run_basic_modes(out, mode, args, design_list)
        case RunMode.ANALYZE:
            run_analyze(out, args, design_list)
        case RunMode.FF:
            run_ff(out, args, design_list)


if __name__ == "__main__":
    main()
