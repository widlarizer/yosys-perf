from enum import Enum, StrEnum
from pathlib import Path
import argparse
import pkgutil
import importlib
import subprocess
import functools
import scripts
import re
r = functools.partial(subprocess.check_output, text=True)

class OutputMode(StrEnum):
    HUMAN = "human"
    CSV = "csv"

class RunMode(StrEnum):
    ARTIFACT = "artifact"
    VERILOG = "verilog"
    SYNTH = "synth"

class SynthMode(StrEnum):
    SYNTH = "synth"
    SYNTH_FLATTEN = "synth -flatten"
    def from_str(s):
        if s == "":
            return SynthMode.SYNTH
        elif s == "flatten":
            return SynthMode.SYNTH_FLATTEN
        else:
            print(f"Invalid synthesis flow: {s}")
            exit(1)
    # Note: hard-coded
    # SYNTH_SKY130 = "synth; -lib canon/OpenROAD-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"

def common_parent(paths):
    zipped = zip(*(p.parts for p in paths))
    common_parts = []
    for parts in zipped:
        if len(set(parts)) > 1: break
        common_parts.append(parts[0])
    return Path(*common_parts)


def fmt_params(params):
    s = ""
    first = True
    for key, value in sorted(params.items()):
        s += f"{key}_{value}"
        if not first:
            s += "__"
        first = False
    return s


class HumanOut():
    def add(self, ys, design, result):
        print(f"{ys}: {design}")
        print(result)

    def out(self):
        pass

class CsvOut():
    _time = dict()
    _memory = dict()

    def add(self, ys, design, result):
        match = re.search(r"user ([\d.]+)s system ([\d.]+)s, MEM: ([\d.]+) MB", result)
        if not match:
            print(f"Unexpected formatting of \"{result}\"")
        user, system, mem = match.groups()
        self._time[ys, design] = float(user) + float(system)
        self._memory[ys, design] = float(mem)

    def out(self):
        yosyes = set()
        designs = set()
        for ys, design in self._time.keys():
            yosyes.add(ys)
            designs.add(design)

        ys_root = common_parent(yosyes)
        def print_one_dataset(dataset):
            print("design", end=";")
            for ys in sorted(yosyes):
                print(ys.relative_to(ys_root), end=";")
            print()

            for design in sorted(designs):
                print(design, end=";")
                for ys in sorted(yosyes):
                    print(dataset[ys, design], end=";")
                print()

        print("time")
        print_one_dataset(self._time)
        print()
        print("memory")
        print_one_dataset(self._memory)


yosys_log_end = re.compile("End of script.*")
def run(out, mode, design, synth_mode, yosys, params):
    design_name, design_class = design
    tag = f"{design_name}_{fmt_params(params)}" if params else f"{design_name}"
    artifact_file = tag + ".il"
    artifact_path = Path("artifacts") / artifact_file
    write_rtlil_ys = f"write_rtlil {artifact_path}"
    read_rtlil_ys = f"read_rtlil {artifact_path}"
    read_sv_ys = design_class.sv(params)
    synth_ys = synth_mode.value
    script = ""
    match mode:
        case RunMode.ARTIFACT:
            script = read_sv_ys + "\n" + write_rtlil_ys
        case RunMode.VERILOG:
            script = read_sv_ys + "\n" + synth_ys
        case RunMode.SYNTH:
            script = read_rtlil_ys + "\n" + synth_ys
        case _:
            assert False, "out of sync RunMode with run"
    log = r([yosys, "-p", script])
    res = yosys_log_end.search(log).group(0)
    out.add(yosys, f"{tag}-{synth_mode.name}", res)


def design_map():
    d = dict()
    for _, modname, ispkg in pkgutil.iter_modules(scripts.__path__):
        if not ispkg:
            module = importlib.import_module(f"scripts.{modname}")
            for c in module.__all__:
                d[c.__name__.lower()] = c
    return d


def params_from_str(pairs):
    ret = dict()
    for pair in pairs:
        key_value = pair.split("=")
        if (len(key_value)) != 2:
            print(f"Can't parse {pair} as parameter")
            exit(1)
        lhs, rhs = key_value
        ret[lhs] = rhs
    return ret


def single_run(out_mode, mode, args, design, params):
    if mode != RunMode.SYNTH and args.flow != "":
        print("--flow specified outside of synth mode")
        exit(1)

    for yosys in args.yosys:
        run(out_mode,
            mode,
            (design, design_map()[design]()),
            SynthMode.from_str(args.flow),
            yosys,
            params_from_str(params)
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=[mode.value for mode in RunMode])
    parser.add_argument("--yosys",
                        default=[Path("yosys")],
                        type=Path,
                        nargs="+",
                        help="Path to the Yosys binary")
    parser.add_argument("--design",
                        type=str,
                        choices=design_map().keys(),
                        help="Path to the Yosys binary")
    parser.add_argument("--flow",
                        default="",
                        type=str,
                        help="Alternate flow, valid only if mode is synth")
    parser.add_argument("--param",
                        nargs='*',
                        default=[],
                        help="Design parameters")
    parser.add_argument("--auto",
                        action="store_true",
                        help="Design parameters")
    parser.add_argument("--output",
                        choices=list(map(str, OutputMode)),
                        default=OutputMode.HUMAN,
                        help="Design parameters")

    args = parser.parse_args()
    mode = RunMode(args.mode)
    out_mode = OutputMode(args.output)

    if out_mode == OutputMode.CSV:
        out = CsvOut()
    else:
        out = HumanOut()

    if mode == RunMode.ARTIFACT:
        with open(Path("canon") / "ys-version", 'w') as myoutput:
            arg = "--git-hash"
            try:
                r([args.yosys, "--git-hash"], stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError:
                arg = "--version"
            subprocess.run([args.yosys, arg], stdout=myoutput)

    if args.auto:
        for design, params in [
            ("jpeg", []),
            ("ibex", []),
            ("fft64", ["width=64"]),]:
            single_run(out, mode, args, design, params)
    else:
        if not args.design:
            print("Missing --design without --auto")
            exit(1)
        single_run(out, mode, args, args.design, args.param)

    out.out()


if __name__ == "__main__":
    main()
