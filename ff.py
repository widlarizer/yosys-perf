#!/usr/bin/env python3
"""Print FF count / total cell count ratio for Yosys designs."""

from pathlib import Path
import argparse
import subprocess
import re
import sys

from analyze import discover_designs, params_from_str, fmt_params

# from discussion #4124
FF_TYPES = [
	# Word-level
	"$sr", "$ff", "$dff", "$dffe", "$dffsr", "$dffsre",
	"$adff", "$adffe", "$aldff", "$aldffe",
	"$sdff", "$sdffe", "$sdffce",
	"$dlatch", "$adlatch", "$dlatchsr",
	# Gate-level DFF variants
	"$_DFF_N_", "$_DFF_P_",
	"$_DFF_NN0_", "$_DFF_NN1_", "$_DFF_NP0_", "$_DFF_NP1_",
	"$_DFF_PN0_", "$_DFF_PN1_", "$_DFF_PP0_", "$_DFF_PP1_",
	"$_DFFE_NN_", "$_DFFE_NP_", "$_DFFE_PN_", "$_DFFE_PP_",
	"$_DFFE_NN0N_", "$_DFFE_NN0P_", "$_DFFE_NN1N_", "$_DFFE_NN1P_",
	"$_DFFE_NP0N_", "$_DFFE_NP0P_", "$_DFFE_NP1N_", "$_DFFE_NP1P_",
	"$_DFFE_PN0N_", "$_DFFE_PN0P_", "$_DFFE_PN1N_", "$_DFFE_PN1P_",
	"$_DFFE_PP0N_", "$_DFFE_PP0P_", "$_DFFE_PP1N_", "$_DFFE_PP1P_",
	"$_DFFSR_NNN_", "$_DFFSR_NNP_", "$_DFFSR_NPN_", "$_DFFSR_NPP_",
	"$_DFFSR_PNN_", "$_DFFSR_PNP_", "$_DFFSR_PPN_", "$_DFFSR_PPP_",
	"$_DFFSRE_NNNN_", "$_DFFSRE_NNNP_", "$_DFFSRE_NNPN_", "$_DFFSRE_NNPP_",
	"$_DFFSRE_NPNN_", "$_DFFSRE_NPNP_", "$_DFFSRE_NPPN_", "$_DFFSRE_NPPP_",
	"$_DFFSRE_PNNN_", "$_DFFSRE_PNNP_", "$_DFFSRE_PNPN_", "$_DFFSRE_PNPP_",
	"$_DFFSRE_PPNN_", "$_DFFSRE_PPNP_", "$_DFFSRE_PPPN_", "$_DFFSRE_PPPP_",
	"$_SDFF_NN0_", "$_SDFF_NN1_", "$_SDFF_NP0_", "$_SDFF_NP1_",
	"$_SDFF_PN0_", "$_SDFF_PN1_", "$_SDFF_PP0_", "$_SDFF_PP1_",
	"$_SDFFE_NN0N_", "$_SDFFE_NN0P_", "$_SDFFE_NN1N_", "$_SDFFE_NN1P_",
	"$_SDFFE_NP0N_", "$_SDFFE_NP0P_", "$_SDFFE_NP1N_", "$_SDFFE_NP1P_",
	"$_SDFFE_PN0N_", "$_SDFFE_PN0P_", "$_SDFFE_PN1N_", "$_SDFFE_PN1P_",
	"$_SDFFE_PP0N_", "$_SDFFE_PP0P_", "$_SDFFE_PP1N_", "$_SDFFE_PP1P_",
	"$_SDFFCE_NN0N_", "$_SDFFCE_NN0P_", "$_SDFFCE_NN1N_", "$_SDFFCE_NN1P_",
	"$_SDFFCE_NP0N_", "$_SDFFCE_NP0P_", "$_SDFFCE_NP1N_", "$_SDFFCE_NP1P_",
	"$_SDFFCE_PN0N_", "$_SDFFCE_PN0P_", "$_SDFFCE_PN1N_", "$_SDFFCE_PN1P_",
	"$_SDFFCE_PP0N_", "$_SDFFCE_PP0P_", "$_SDFFCE_PP1N_", "$_SDFFCE_PP1P_",
	# Latches
	"$_DLATCH_N_", "$_DLATCH_P_",
	"$_DLATCH_NN0_", "$_DLATCH_NN1_", "$_DLATCH_NP0_", "$_DLATCH_NP1_",
	"$_DLATCH_PN0_", "$_DLATCH_PN1_", "$_DLATCH_PP0_", "$_DLATCH_PP1_",
	"$_DLATCHSR_NNN_", "$_DLATCHSR_NNP_", "$_DLATCHSR_NPN_", "$_DLATCHSR_NPP_",
	"$_DLATCHSR_PNN_", "$_DLATCHSR_PNP_", "$_DLATCHSR_PPN_", "$_DLATCHSR_PPP_",
	# Async load
	"$_ALDFF_NN_", "$_ALDFF_NP_", "$_ALDFF_PN_", "$_ALDFF_PP_",
	"$_ALDFFE_NNN_", "$_ALDFFE_NNP_", "$_ALDFFE_NPN_", "$_ALDFFE_NPP_",
	"$_ALDFFE_PNN_", "$_ALDFFE_PNP_", "$_ALDFFE_PPN_", "$_ALDFFE_PPP_",
	# SR latches
	"$_SR_NN_", "$_SR_NP_", "$_SR_PN_", "$_SR_PP_",
]


def run_yosys(yosys_bin, script):
	result = subprocess.run(
		[yosys_bin, "-p", script],
		capture_output=True,
		text=True
	)
	return result.stdout + result.stderr


def get_counts(yosys_bin, artifact_path, flow):
	"""Get FF and total cell counts using select -count, plus ABC area."""
	synth_cmd = "synth -flatten -noabc" if flow == "flatten" else "synth -noabc"
	
	ff_select = " ".join(f"t:{t}" for t in FF_TYPES)
	
	script = f"""
read_rtlil {artifact_path}
{synth_cmd}
select -count {ff_select}
select -count *
stat
abc -script +strash;print_stats
"""
	
	output = run_yosys(yosys_bin, script)
	counts = re.findall(r'(\d+) objects', output)
	ff_count = int(counts[0]) if len(counts) >= 1 else 0
	total_from_select = int(counts[1]) if len(counts) >= 2 else 0
	m = re.search(r'^\s*(\d+)\s+cells\s*$', output, re.MULTILINE)
	total_cells = int(m.group(1)) if m else total_from_select
	
	# Parse ABC stats: "and = X" (AIG node count)
	abc_area = 0
	for m in re.finditer(r'\band\s*=\s*(\d+)', output):
		abc_area += int(m.group(1))
	
	return ff_count, total_cells, abc_area, output


def main():
	parser = argparse.ArgumentParser(
		description="Print FF count over total cell count for designs"
	)
	parser.add_argument(
		"--yosys", default="yosys", type=Path,
		help="Path to Yosys binary"
	)
	parser.add_argument(
		"--design",
		help="Specific design (default: all in artifacts/)"
	)
	parser.add_argument(
		"--param", nargs="*", default=[],
		help="Design parameters (e.g., width=64)"
	)
	parser.add_argument(
		"--flow", default="", choices=["", "flatten"],
		help="Synthesis flow variant"
	)
	parser.add_argument(
		"--ff-size", type=int, default=6,
		help="AIG-equivalent size per FF (default: 6)"
	)
	parser.add_argument(
		"--csv", action="store_true",
		help="CSV output format"
	)
	parser.add_argument(
		"-v", "--verbose", action="store_true",
		help="Show yosys output"
	)

	args = parser.parse_args()
	params = params_from_str(args.param)

	if args.design:
		tag = f"{args.design}_{fmt_params(params)}" if params else args.design
		designs_to_run = [(args.design, tag)]
	else:
		discovered = discover_designs("artifacts")
		if not discovered:
			print("No designs found in artifacts/", file=sys.stderr)
			sys.exit(1)
		designs_to_run = [(d, d) for d in discovered]

	results = []
	for design, tag in designs_to_run:
		artifact_path = Path("artifacts") / f"{tag}.il"
		if not artifact_path.exists():
			print(f"Artifact not found: {artifact_path}", file=sys.stderr)
			continue
		
		ff, total, abc_area, output = get_counts(args.yosys, artifact_path, args.flow)
		
		if args.verbose:
			print(f"=== {design} ===")
			print(output)
			print()
		
		logic = total - ff
		ratio = ff / total if total > 0 else 0.0
		ff_area = ff * args.ff_size
		total_area = abc_area + ff_area
		ff_area_pct = (ff_area / total_area * 100) if total_area > 0 else 0.0
		results.append({
			"design": tag,
			"ff": ff,
			"logic": logic,
			"total": total,
			"ratio": ratio,
			"abc_area": abc_area,
			"ff_area": ff_area,
			"total_area": total_area,
			"ff_area_pct": ff_area_pct,
		})

	if not results:
		print("No results", file=sys.stderr)
		sys.exit(1)

	if args.csv:
		print("design,ff,logic,total,ff_ratio,abc_area,ff_area,total_area,ff_area_pct")
		for r in results:
			print(f"{r['design']},{r['ff']},{r['logic']},{r['total']},{r['ratio']:.4f},{r['abc_area']},{r['ff_area']},{r['total_area']},{r['ff_area_pct']:.2f}")
	else:
		print(f"(ff_size={args.ff_size})")
		for r in results:
			pct = r["ratio"] * 100
			print(f"{r['design']}: {r['ff']}/{r['total']} FF ({pct:.1f}%), area: {r['abc_area']} logic + {r['ff_area']} FF = {r['total_area']} ({r['ff_area_pct']:.1f}% FF)")


if __name__ == "__main__":
	main()
