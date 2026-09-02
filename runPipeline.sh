#!/bin/sh
# runPipeline.sh — stages 1-5 on the real puzzle files: netlist -> cones -> cone
# groups (functional equivalence classes).
#
# Run locally from the repo root:  ./runPipeline.sh
# The tools resolve lib/sky130_fd_sc_hd_merged.lef relative to the working
# directory, so this must run from the repo root. Everything it writes is
# derived from the puzzle — the reading-vs-using rule in README applies.
set -e

GDS=${GDS:-puzzle.gds}
OUT=${OUT:-out}
mkdir -p "$OUT"

# ------------------------------------------------- stages 1-3: netlist extraction
echo "## EXTRACT — KLayout connectivity, pin names from the layout's labels ##"
python3 src/klayoutNetlist.py "$GDS" --out "$OUT/NETLIST_GRAPH.json"

# --------------------------------------------------------- stage 4: cones
echo
echo "## CONES — flop-to-flop combinational slices ##"
python3 src/coneDecompose.py "$OUT/NETLIST_GRAPH.json" --out "$OUT/CONES.json"

# ----------------------------------------- stage 5: cone groups (equivalence)
echo
echo "## CONE GROUPS — cone truth tables and functional equivalence classes ##"
python3 src/coneClasses.py "$OUT/NETLIST_GRAPH.json" "$OUT/CONES.json" \
    --out "$OUT/CONE_CLASSES.json"

echo
echo "Done. Outputs in $OUT/: NETLIST_GRAPH.json, CONES.json, CONE_CLASSES.json"
