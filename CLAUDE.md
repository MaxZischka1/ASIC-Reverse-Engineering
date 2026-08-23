# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A solution-in-progress for the Jane Street ASIC reverse-engineering puzzle (see `README.md`
and the linked blog post). The only ground-truth inputs shipped by the puzzle are
`puzzle.gds` (a manufacturable layout with internal names stripped) and
`example_inputs.vcd` (recorded stimulus + response from the real design, which does *not*
assert `success`). Everything else at the top level is reconstruction: a from-scratch
Python pipeline that lifts raw GDSII geometry back up to a structural Verilog netlist,
then to per-flop logic cones grouped by the Boolean function they compute.

`warmup/` is the puzzle authors' small reference design (two shift registers + adder +
comparator, `success` when `A + B == 496`), shipped at *every* stage of the flow
(`00_source.v` through `04_final.gds`). It is the ground truth used to validate the tools:
run the pipeline on `04_final.gds` and check the recovered netlist against
`01_netlist.v`/`00_source.v`. Do this before trusting a tool change on the real puzzle.

Python is standard-library only (no gdstk/gdspy/klayout — parsing GDSII by hand is
deliberate). Simulation needs `verilator` and a C++17 compiler; `make waves` needs
`gtkwave`.

## Rule: never read waveform or GDS files directly

Claude must not open, read, dump, grep, or otherwise inspect the contents of any
waveform or layout file:

- `*.vcd` — `example_inputs.vcd`, `waveform_puzzle.vcd`, `warmup/waveform_adderDemo.vcd`
- `*.gds` — `puzzle.gds`, `warmup/04_final.gds`

This covers reading them by any means: `Read`, `cat`/`head`/`sed`, `grep`, a throwaway
Python snippet, or a script written to print their contents. Inspect them only through the
committed pipeline tools, and reason from those tools' outputs.

This restricts *Claude*, not the code. `gdsParser.py` parses `puzzle.gds` and the testbench
consumes stimulus transcribed from `example_inputs.vcd` — that is the point of the project
and is unaffected. Verilator may write VCDs and `make waves` may open one in gtkwave; what
is forbidden is Claude reading the bytes.

If a task seems to need the contents of one of these files, say so and stop rather than
working around the rule.

## The extraction pipeline

Each stage is a standalone script with argparse defaults wired to the previous stage's
output, so the whole chain runs with no arguments from the repo root:

```
puzzle.gds
  │  python3 gdsParser.py                 # GDSII binary -> per-structure geometry
  ├─> puzzleNetlist.json
  │     │  python3 netGraph.py            # union-find over touching shapes -> nets
  │     ├─> puzzleGraph.json              # carries instance_pins (net -> named pin)
  │     │  python3 moduleGraph.py         # nodes=cells, edges=nets, pin directions
  │     └─> MODULE_GRAPH.json
  │           ├─ python3 genVerilog.py   -> puzzleNetlist.v   (flat structural netlist)
  │           └─ python3 logicGraph.py   -> LOGIC_GRAPH.json  (+ --verilog puzzleReduced.v)
  │                 │  python3 coneClasses.py   # cones -> truth tables -> equivalence groups
  │                 ├─> CONE_CLASSES.json
  │                 │     │  python3 coneGraph.py   # groups as nodes, flop dataflow as edges
  │                 │     └─> CONE_GRAPH.json + CONE_GRAPH.md
```

Regenerating a stage invalidates everything downstream of it; the JSON files are committed
build products, not sources.

To run the same chain on the warmup, every path must be passed explicitly (the argparse
defaults all point at the real puzzle), and the warmup uses its own filenames:

```
python3 gdsParser.py  warmup/04_final.gds --out warmup/OUT.json
python3 netGraph.py   warmup/OUT.json     --out warmup/NET_GRAPH.json
python3 moduleGraph.py warmup/OUT.json    --out warmup/MODULE_GRAPH.json --verilog warmup/sky130_prims.v
python3 genVerilog.py --module-graph warmup/MODULE_GRAPH.json --net-graph warmup/NET_GRAPH.json \
                      --out warmup/adder_demo.v --module-name adder_demo
python3 logicGraph.py warmup/MODULE_GRAPH.json --out warmup/LOGIC_GRAPH.json \
                      --prims warmup/sky130_prims.v
python3 coneClasses.py warmup/LOGIC_GRAPH.json --out warmup/CONE_CLASSES.json \
                       --prims warmup/sky130_prims.v
```

The check that matters: `warmup/MODULE_GRAPH.json` must come out at 85 nodes / 84 wires,
and its 79 cell instances must match `01_netlist.v`'s 79 non-filler cells per cell type
(`01_netlist.v` also holds 151 `decap_3`/`tapvpwrvgnd_1` fillers, which carry no signal and
are correctly absent from the recovered graph).

The scripts carry long module docstrings explaining the *why*
of each design decision — read the docstring before changing a stage's logic.

Key stage semantics that are easy to get wrong:

- **`gdsParser.py`** — hand-written GDSII record reader. GDS has no concept of a net; layer
  numbers are bare integers mapped to sky130 materials by a table in this file. Emits
  BOUNDARY polygons, PATH segments and every SREF instantiation in *local* coordinates plus
  its placement transform.
- **`netGraph.py`** — flattens placements to absolute coordinates, then union-finds every
  conductive shape into nets using exact polygon intersection (bbox-only overlap snowballs
  into one giant net). Only interconnect layers (li1, met1..met5) and their contacts/vias
  participate; poly/diff/tap are excluded on purpose. `bond_intra_cell_pins()` rejoins li1
  islands that a cell ties together *below* li1 through poly — computed once per cell type,
  and deliberately never applied to power rails.
- **`moduleGraph.py`** — reuses netGraph's pipeline and re-assembles it as one edge per net
  (not a pairwise clique), each edge listing every (instance, pin) endpoint. Pin directions
  come from parsing `sky130_prims.v`, which is the source of truth for cell pinouts. Power
  nets (identified from VPWR/VGND/VPB/VNB TEXT labels), vias, TOP routing, and single-endpoint
  intra-cell nets are all dropped here — downstream stages never need to re-filter them.
- **`genVerilog.py`** — nothing about the design is hardcoded: port names, port directions,
  and per-cell-type pinouts are all derived from the graphs. A hardcoded port table here
  previously emitted a header for the warmup adder's ports on the real puzzle; don't
  reintroduce one.
- **`logicGraph.py`** — splices buffers out (lossless) and absorbs inverters into consuming
  pins as a `(net, inverted)` pair (also lossless), then cuts the graph at flip-flops into
  per-sink cones. Cones overlap by design: a gate feeding five flops appears in five cones,
  so cone sizes sum to more than the gate count.
- **`cellLibrary.py`** — parses `sky130_prims.v` into `{class: CellSpec}`: pins, Boolean
  function compiled from the continuous assignments, sequential flag, and pin symmetry
  *derived* by swapping inputs and re-testing the truth table (so an AND2's A/B are known
  interchangeable and a MUX2's A0/A1/S are known not to be). Import-only, no CLI. It is the
  single source of cell semantics for the cone stage.
- **`coneClasses.py`** — evaluates each cone's full truth table (numpy bit-packed, 64
  assignments per machine op), then canonicalizes it under leaf permutation so two cones
  computing the same function group together even when synthesis built one as the De Morgan
  dual of the other or landed the operand bits on different pins. Grouping is by hash of the
  canonical table, so it is exact: a functional group can never be a false positive, only a
  missed merge. Constants are folded as fixed columns, which lowers k and is what makes the
  wide cones tractable. Groups flagged `structural` fell out of the functional path and are
  heuristic.
- **`coneGraph.py`** — promotes those groups to graph nodes and recovers the edges: group A
  -> group B when a flop in A drives a leaf of a cone in B. Each node is labelled by matching
  its canonical truth table against a primitive library (AND/OR/NAND/NOR/XOR/XNOR of each
  arity including mixed-polarity product/sum families, adder sum/carry, 2:1 and 4:1 muxes);
  unmatched nodes are named `unknown_k<N>_<hash>`.

## Build and simulate

From the repo root (real puzzle):

```
make            # verilate + build + simulate puzzleNetlist.v, writes waveform_puzzle.vcd
make lint       # verilator --lint-only on puzzleNetlist.v + sky130_prims.v
make logic      # regenerate LOGIC_GRAPH.json
make cones      # regenerate CONE_CLASSES.json, then CONE_GRAPH.json + CONE_GRAPH.md
make reduced    # regenerate puzzleReduced.v, then run the SAME testbench against it
make waves      # open waveform_puzzle.vcd in gtkwave
make clean      # remove obj_dir_TL/, obj_dir_reduced/, .stamp.*, the VCD
```

`make reduced` is the equivalence check for `logicGraph.py`: if the reference frames still
pass against the buffer/inverter-reduced netlist, the reduction preserved behavior.

`warmup/` has its own Makefile with the same target names, simulating the recovered flat
netlist (`adder_demo.v`, top `adder_demo`) under `adderTestbench.cpp`:

```
make            # flat netlist from genVerilog.py (adder_demo.v, top adder_demo)
```

The testbench reaches its top through `-DTOP_TYPE` (default `Vadder_demo`), so any other
netlist exposing the same ports (`A, B, clk, en, rst_n, S`) can be checked against it.

## The testbench is the regression test

`puzzleTestbench.cpp` is the only end-to-end test of the whole extraction chain. There is
no reference model — recovering the function *is* the puzzle — so it replays the two frames
transcribed from `example_inputs.vcd` and asserts the netlist reproduces the recorded
message ("TRY AGAIN") with `success` low. If those pass, GDS → nets → cells → netlist is
behaving like the real silicon on the only inputs with ground truth.

Frame protocol (recovered from the VCD, not guessed): 3 cycles `rst_n=0, enable=0`;
1 cycle idle; 121 cycles `enable=1` shifting bits in on `I`; 35 cycles readout, during
which the design drives one ASCII byte per character on `O[7:0]` with zero bytes between.

```
./obj_dir_TL/Vpuzzle                 # replay both reference frames and check them
./obj_dir_TL/Vpuzzle <121 bits>      # drive one custom frame ('0'/'1' string, shift order)
```

The custom-frame mode is how candidate solutions get tried. Ports are `clk, rst_n, enable, I`
in and `O[7:0], success` out. The top module is named `puzzle` so signal paths line up with
`example_inputs.vcd` when both are opened in a waveform viewer.


