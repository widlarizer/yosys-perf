#!/usr/bin/env python3
"""
Analyze and profile Yosys designs.
Combines profiling (like thing.py) with design statistics.
"""

from pathlib import Path
import argparse
import subprocess
import re
import sys
import pkgutil
import importlib

try:
	import scripts
	HAS_SCRIPTS = True
except ImportError:
	HAS_SCRIPTS = False


def run_yosys(yosys_bin, script):
	result = subprocess.run(
		[yosys_bin, "-p", script],
		capture_output=True,
		text=True
	)
	return result.stdout + result.stderr


def parse_output(output):
	stats = {}
	
	# Try to find totals section (with submodules)
	match = re.search(
		r'\+----------Count including submodules\.\s*\|\s*(.*?)(?:End of script|$)',
		output, re.DOTALL
	)
	
	# If not found, try the simpler format for flattened designs
	if not match:
		match = re.search(
			r'Printing statistics\.\s*(.*?)(?:End of script|$)',
			output, re.DOTALL
		)
	
	if match:
		summary = match.group(1)
		
		for name, pattern in [
			("wires", r'^\s*(\d+)\s+wires\s*$'),
			("wire_bits", r'^\s*(\d+)\s+wire bits\s*$'),
			("public_wires", r'^\s*(\d+)\s+public wires\s*$'),
			("public_wire_bits", r'^\s*(\d+)\s+public wire bits\s*$'),
			("ports", r'^\s*(\d+)\s+ports\s*$'),
			("port_bits", r'^\s*(\d+)\s+port bits\s*$'),
			("memories", r'^\s*(\d+)\s+memories\s*$'),
			("memory_bits", r'^\s*(\d+)\s+memory bits\s*$'),
			("processes", r'^\s*(\d+)\s+processes\s*$'),
			("cells", r'^\s*(\d+)\s+cells\s*$'),
		]:
			m = re.search(pattern, summary, re.MULTILINE)
			if m:
				stats[name] = int(m.group(1))
		
		cells = {}
		for m in re.finditer(r'^\s+(\d+)\s+(\$_?\w+)\s*$', summary, re.MULTILINE):
			cells[m.group(2)] = int(m.group(1))
		stats["cells_breakdown"] = cells
		
		ff_count = 0
		logic_count = 0
		for cell, count in cells.items():
			if "DFF" in cell or "SDFF" in cell or "$ff" in cell:
				ff_count += count
			else:
				logic_count += count
		stats["ff_count"] = ff_count
		stats["logic_count"] = logic_count
	
	m = re.search(r'CPU:\s*user\s+([\d.]+)s\s+system\s+([\d.]+)s.*?MEM:\s*([\d.]+)\s*MB', output)
	if m:
		stats["user_time"] = float(m.group(1))
		stats["sys_time"] = float(m.group(2))
		stats["time"] = stats["user_time"] + stats["sys_time"]
		stats["mem_mb"] = float(m.group(3))
	
	# Parse top time consumers from
	m = re.search(r'Time spent:\s*(.+?)(?:\n|$)', output)
	if m:
		top_times = []
		for pm in re.finditer(r'(\d+)%\s+(\d+)x\s+(\w+)\s*\((\d+)\s*sec\)', m.group(1)):
			pct = int(pm.group(1))
			count = int(pm.group(2))
			name = pm.group(3)
			secs = int(pm.group(4))
			top_times.append((name, pct, count, secs))
		stats["top_times"] = top_times
	
	# ABC stats
	for m in re.finditer(r'ABC:.*?nd\s*=\s*(\d+).*?lev\s*=\s*(\d+)', output):
		stats["abc_nd"] = stats.get("abc_nd", 0) + int(m.group(1))
		lev = int(m.group(2))
		if lev > stats.get("abc_lev", 0):
			stats["abc_lev"] = lev
	
	return stats


def discover_designs(artifacts_dir):
	designs = []
	for f in sorted(Path(artifacts_dir).glob("*.il")):
		designs.append(f.stem)
	return designs


def design_map():
	"""Get available designs from scripts module."""
	if not HAS_SCRIPTS:
		return {}
	d = {}
	for _, modname, ispkg in pkgutil.iter_modules(scripts.__path__):
		if not ispkg:
			module = importlib.import_module(f"scripts.{modname}")
			for c in module.__all__:
				d[c.__name__.lower()] = c
	return d


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


def analyze(yosys_bin, design, params, mode, flow):
	"""Analyze a single design with given yosys binary."""
	designs = design_map()
	
	tag = f"{design}_{fmt_params(params)}" if params else design
	artifact_file = tag + ".il"
	artifact_path = Path("artifacts") / artifact_file
	
	# Build script based on mode
	if mode == "artifact":
		if design not in designs:
			print(f"Design {design} not found in scripts module", file=sys.stderr)
			return None
		design_class = designs[design]()
		read_sv = design_class.sv(params)
		script = f"{read_sv}; write_rtlil {artifact_path}"
	elif mode == "verilog":
		if design not in designs:
			print(f"Design {design} not found in scripts module", file=sys.stderr)
			return None
		design_class = designs[design]()
		read_sv = design_class.sv(params)
		synth_cmd = "synth -flatten" if flow == "flatten" else "synth"
		script = f"{read_sv}; {synth_cmd}; stat"
	else:  # synth mode
		if not artifact_path.exists():
			print(f"Artifact not found: {artifact_path}", file=sys.stderr)
			return None
		synth_cmd = "synth -flatten -noabc" if flow == "flatten" else "synth -noabc"
		script = f"read_rtlil {artifact_path}; {synth_cmd}; abc -g AND,NAND,OR,NOR,XOR,XNOR,ANDNOT,ORNOT,MUX -script +print_stats; stat"
	
	output = run_yosys(yosys_bin, script)
	stats = parse_output(output)
	stats["design"] = tag
	stats["yosys"] = str(yosys_bin)
	
	return stats


def print_human(all_stats, yosys_bins):
	"""Print human-readable output."""
	# Group by design
	by_design = {}
	for s in all_stats:
		design = s.get("design", "unknown")
		if design not in by_design:
			by_design[design] = []
		by_design[design].append(s)
	
	for design, stats_list in by_design.items():
		for s in stats_list:
			yosys = Path(s.get('yosys', 'yosys')).name
			user = s.get('user_time')
			sys_t = s.get('sys_time')
			mem = s.get('mem_mb')
			
			# Header with yosys binary if multiple
			if len(yosys_bins) > 1:
				if user is not None:
					print(f"{yosys}: {design}: user {user:.2f}s system {sys_t:.2f}s, MEM: {mem:.2f} MB")
				else:
					print(f"{yosys}: {design}:")
			else:
				if user is not None:
					print(f"{design}: user {user:.2f}s system {sys_t:.2f}s, MEM: {mem:.2f} MB")
				else:
					print(f"{design}:")
			
			# Design stats
			wires = s.get('wires', 0)
			if wires:
				print(f"  {wires} wires, {s.get('wire_bits', 0)} wire bits")
				print(f"  {s.get('public_wires', 0)} public wires, {s.get('public_wire_bits', 0)} public wire bits")
			
			ports = s.get('ports', 0)
			if ports:
				print(f"  {ports} ports, {s.get('port_bits', 0)} port bits")
			
			mem_count = s.get('memories')
			if mem_count:
				print(f"  {mem_count} memories, {s.get('memory_bits', 0)} memory bits")
			
			proc = s.get('processes')
			if proc:
				print(f"  {proc} processes")
			
			# Cells with FF/logic split
			cells = s.get('cells', 0)
			ff = s.get('ff_count', 0)
			logic = s.get('logic_count', 0)
			if cells:
				print(f"  {cells} cells ({logic} logic, {ff} FF)")
				
				breakdown = s.get("cells_breakdown", {})
				for cell, count in sorted(breakdown.items(), key=lambda x: -x[1]):
					print(f"    {count} {cell}")
			
			# ABC stats
			abc_nd = s.get('abc_nd')
			abc_lev = s.get('abc_lev')
			if abc_nd:
				print(f"  ABC: {abc_nd} nodes, {abc_lev} levels")
			
			# Top time consumers
			top_times = s.get('top_times', [])
			if top_times:
				parts = [f"{pct}% {name} ({count}x)" for name, pct, count, secs in top_times]
				print(f"  Top time: {', '.join(parts)}")
			
			print()


def print_csv(all_stats, yosys_bins):
	"""Print CSV output, comparing yosys binaries side by side like thing.py."""
	# Group by design
	by_design = {}
	for s in all_stats:
		design = s.get("design", "unknown")
		yosys = s.get("yosys", "yosys")
		if design not in by_design:
			by_design[design] = {}
		by_design[design][yosys] = s
	
	yosys_bins = [str(y) for y in yosys_bins]
	ys_root = common_parent([Path(y) for y in yosys_bins])
	
	# Time table
	print("time")
	print("design", end="\t")
	for ys in yosys_bins:
		print(Path(ys).relative_to(ys_root), end="\t")
	print()
	for design in sorted(by_design.keys()):
		print(design, end="\t")
		for ys in yosys_bins:
			s = by_design[design].get(ys, {})
			t = s.get("time")
			print(f"{t:.2f}" if t else "", end="\t")
		print()
	
	print()
	
	# Memory table
	print("memory")
	print("design", end="\t")
	for ys in yosys_bins:
		print(Path(ys).relative_to(ys_root), end="\t")
	print()
	for design in sorted(by_design.keys()):
		print(design, end="\t")
		for ys in yosys_bins:
			s = by_design[design].get(ys, {})
			m = s.get("mem_mb")
			print(f"{m:.1f}" if m else "", end="\t")
		print()
	
	print()
	
	# Cells table
	print("cells")
	print("design", end="\t")
	for ys in yosys_bins:
		print(Path(ys).relative_to(ys_root), end="\t")
	print()
	for design in sorted(by_design.keys()):
		print(design, end="\t")
		for ys in yosys_bins:
			s = by_design[design].get(ys, {})
			c = s.get("cells")
			print(f"{c}" if c else "", end="\t")
		print()


def main():
	parser = argparse.ArgumentParser(description="Analyze and profile Yosys designs")
	parser.add_argument("mode", nargs="?", default="synth", choices=["artifact", "verilog", "synth"],
		help="Run mode: artifact (generate .il), verilog (read+synth), synth (read .il+synth)")
	parser.add_argument("--yosys", default=["yosys"], type=Path, nargs="+",
		help="Path to Yosys binary (can specify multiple for comparison)")
	parser.add_argument("--design",
		help="Specific design (default: all in artifacts/)")
	parser.add_argument("--param", nargs="*", default=[],
		help="Design parameters (e.g., width=64)")
	parser.add_argument("--flow", default="", choices=["", "flatten"],
		help="Synthesis flow variant")
	parser.add_argument("--csv", action="store_true",
		help="CSV output (for comparing multiple yosys binaries)")
	
	args = parser.parse_args()
	
	params = params_from_str(args.param)
	
	if args.design:
		designs_to_run = [(args.design, params)]
	else:
		# Discover from artifacts
		discovered = discover_designs("artifacts")
		if not discovered:
			print("No designs found in artifacts/", file=sys.stderr)
			sys.exit(1)
		designs_to_run = [(d, {}) for d in discovered]
	
	all_stats = []
	for yosys_bin in args.yosys:
		for design, design_params in designs_to_run:
			merged_params = {**design_params, **params}
			stats = analyze(yosys_bin, design, merged_params, args.mode, args.flow)
			if stats:
				all_stats.append(stats)
	
	if not all_stats:
		print("No results", file=sys.stderr)
		sys.exit(1)
	
	if args.csv:
		print_csv(all_stats, args.yosys)
	else:
		print_human(all_stats, args.yosys)


if __name__ == "__main__":
	main()

