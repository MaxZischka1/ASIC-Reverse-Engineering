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

# ------------------------------------- stage 6: solve for an input (opt-in)
# Off by default: it is the expensive stage and it needs to be told what to
# solve for. Run it with, e.g.
#
#   SOLVE=1 GOAL=success SYMBOLIC=I SYMBOLIC_WHEN=enable MAX_CYCLES=400 \
#       ./runPipeline.sh
#
# Start with SOLVE=preflight to see the structure report — register counts,
# clock domains, how many registers have no reset — before committing to a solve.
if [ -n "${SOLVE:-}" ]; then
    echo
    echo "## SOLVE — unroll and search for an input that reaches the goal ##"
    # Built as positional parameters rather than one word-split string, so an
    # empty or spaced value cannot silently become the wrong argument list.
    set -- --goal "${GOAL:-success}" --max-cycles "${MAX_CYCLES:-200}" \
           --init "${INIT:-free}" --clock "${CLOCK:-clk}"
    if [ -n "${SYMBOLIC:-}" ]; then
        set -- "$@" --symbolic "$SYMBOLIC"
    fi
    if [ -n "${SYMBOLIC_WHEN:-}" ]; then
        set -- "$@" --symbolic-when "$SYMBOLIC_WHEN"
    fi
    if [ -f "${STIMULUS:-bench/stimulus.json}" ]; then
        set -- "$@" --stimulus "${STIMULUS:-bench/stimulus.json}"
    fi
    if [ "$SOLVE" = "preflight" ]; then
        set -- "$@" --preflight
    else
        set -- "$@" --check-unique --check-robust
    fi
    python3 src/symSolve.py "$OUT/NETLIST_GRAPH.json" "$OUT/CONES.json" "$@" \
        --out "$OUT/SOLUTION.json" --bits-out "$OUT/solution.bits"
fi

echo
echo "Done. Outputs in $OUT/: NETLIST_GRAPH.json, CONES.json, CONE_CLASSES.json"
if [ -n "${SOLVE:-}" ]; then
    echo "        plus SOLUTION.json, solution.bits"
fi
