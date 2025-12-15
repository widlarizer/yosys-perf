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

def fmt_params(params):
    s = ""
    first = True
    for key, value in sorted(params.items()):
        s += f"{key}_{value}"
        if not first:
            s += "__"
        first = False
    return s

yosys_log_eol = re.compile("End of script.*")
def run(mode, design, synth_mode, yosys, params):
    design_name, design_class = design
    artifact_file = f"{design_name}_{fmt_params(params)}.il" if params else f"{design_name}.il"
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
    print(f"{yosys}: {artifact_file} {synth_mode.name}")
    log = r([yosys, "-p", script])
    print(yosys_log_eol.search(log).group(0))

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

def single_run(mode, args, design, params):
    if mode != RunMode.SYNTH and args.flow != "":
        print("--flow specified outside of synth mode")
        exit(1)

    for yosys in args.yosys:
        run(mode,
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

    args = parser.parse_args()
    mode = RunMode(args.mode)
    if (mode == RunMode.ARTIFACT):
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
            single_run(mode, args, design, params)
    else:
        if not args.design:
            print("Missing --design without --auto")
            exit(1)
        single_run(mode, args, args.design, args.param)



if __name__ == "__main__":
    main()
