# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A solution-in-progress for the Jane Street ASIC reverse-engineering puzzle (see `README.md`
and the linked blog post). The only ground-truth inputs shipped by the puzzle are
`puzzle.gds` (a manufacturable layout with internal names stripped) and
`example_inputs.vcd` (recorded stimulus + response from the real design, which does *not*
assert `success`). Everything else at the top level is reconstruction: a from-scratch
Python pipeline that lifts raw GDSII geometry back up to a structural Verilog netlist and
then to named logic blocks.

`warmup/` is the puzzle authors' small reference design (two shift registers + adder +
comparator, `success` when `A + B == 496`), shipped at *every* stage of the flow
(`00_source.v` through `04_final.gds`). It is the ground truth used to validate the tools:
run the pipeline on `04_final.gds` and check the recovered netlist against
`01_netlist.v`/`00_source.v`. Do this before trusting a tool change on the real puzzle.

Python is standard-library only (no gdstk/gdspy/klayout — parsing GDSII by hand is
deliberate). Simulation needs `verilator` and a C++17 compiler; `make waves` needs
`gtkwave`.

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
  │           ├─ python3 logicGraph.py   -> LOGIC_GRAPH.json  (+ --verilog puzzleReduced.v)
  │           └─ python3 blockMatch.py   -> BLOCK_MATCH.json  (named blocks)
  │                 └─ python3 genBlockVerilog.py -> blockMatch.v (hierarchical netlist)
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
python3 blockMatch.py warmup/MODULE_GRAPH.json --out warmup/BLOCK_MATCH.json
python3 genBlockVerilog.py --module-graph warmup/MODULE_GRAPH.json \
                           --block-match warmup/BLOCK_MATCH.json --out warmup/blockMatch.v
```

The check that matters: `warmup/MODULE_GRAPH.json` must come out at 85 nodes / 84 wires,
and its 79 cell instances must match `01_netlist.v`'s 79 non-filler cells per cell type
(`01_netlist.v` also holds 151 `decap_3`/`tapvpwrvgnd_1` fillers, which carry no signal and
are correctly absent from the recovered graph).

`genBlockVerilog.py` asserts that every cell landed in some block, so today it only runs on
the warmup — blockMatch.py names all 79 warmup cells but only 125 of the puzzle's 738, and
the assert is what stops it emitting a netlist that silently drops the other 613.

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
- **`blockMatch.py`** — structural NFA matcher over gate templates, but structure is only a
  hypothesis: every match is verified by exhaustive truth-table simulation (up to output
  inversion or full NPN equivalence) before it is kept. Blocks record whether they were
  established by function, structure, or context, so a contextual label is never mistaken
  for a proof.

## Build and simulate

From the repo root (real puzzle):

```
make            # verilate + build + simulate puzzleNetlist.v, writes waveform_puzzle.vcd
make lint       # verilator --lint-only on puzzleNetlist.v + sky130_prims.v
make logic      # regenerate LOGIC_GRAPH.json
make reduced    # regenerate puzzleReduced.v, then run the SAME testbench against it
make waves      # open waveform_puzzle.vcd in gtkwave
make clean      # remove obj_dir_TL/, obj_dir_reduced/, .stamp.*, the VCD
```

`make reduced` is the equivalence check for `logicGraph.py`: if the reference frames still
pass against the buffer/inverter-reduced netlist, the reduction preserved behavior.

`warmup/` has its own Makefile with the same target names, and simulates BOTH recovered
netlists against the one testbench (`adderTestbench.cpp` selects between them on
`-DTOP_TYPE`):

```
make            # flat netlist from genVerilog.py      (adder_demo.v, top adder_demo)
make blocks     # named netlist from genBlockVerilog.py (blockMatch.v, top blockMatch)
```

`make blocks` is the warmup's analogue of `make reduced`: both tops expose the same ports
(`A, B, clk, en, rst_n, S`), so agreement between them says blockMatch.py's naming
preserved the function. Note `blockMatch.v`'s top module is `blockMatch` — `adder_compare`
inside it is a *sub*-module taking `a_reg0..7`/`b_reg0..7`, not the top.

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


