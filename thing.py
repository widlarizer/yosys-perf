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
    # Note: hard-coded
    # SYNTH_SKY130 = "synth; -lib canon/OpenROAD-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib"

def fmt_params(params):
    s = ""
    first = True
    for key, value in params:
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

def main():
    designs = design_map()
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=[mode.value for mode in RunMode])
    parser.add_argument("--yosys",
                        default=Path("yosys"),
                        type=Path,
                        help="Path to the Yosys binary")
    parser.add_argument("--design",
                        required=True,
                        type=str,
                        choices=designs.keys(),
                        help="Path to the Yosys binary")

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
    design = (args.design, designs[args.design]())
    # TODO SynthMode from args
    run(mode, design, SynthMode.SYNTH, args.yosys, dict())


if __name__ == "__main__":
    main()
