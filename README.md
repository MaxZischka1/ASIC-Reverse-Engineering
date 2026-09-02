# ASIC puzzle — cones & cone groups

Clean-room tooling for the Jane Street ASIC reverse-engineering puzzle: take the
flattened layout, recover the gate-level netlist, slice it into flop-to-flop
combinational **cones**, and group the cones that compute the same function
(**cone groups**). Stages 1–5 only — everything downstream of that has been
removed from this tree.

Every stage is general-purpose and validated against synthetic fixtures with
hand-computed expected output (`tests/`).

## The rule, and the line it draws

The constraint is on *using* puzzle-derived information to solve the puzzle — not
on the tool code touching it.

- **Allowed:** running a stage or the whole pipeline on the real files, and
  reading *verification signals* — exit codes, test pass/fail, "N registers, 0
  orphan cells". These say whether the **tooling** is correct.
- **Also allowed:** the recorded stimulus — `example_inputs.vcd` and anything
  derived from it (`bench/stimulus.json`, the `STIM_*` strings inlined in the
  testbenches). It is a shipped waveform of the design's pins, a *tool input*;
  it says nothing about how the circuit computes.
- **Not allowed:** reading or reasoning about the **circuit's content** — the
  extracted netlist, a cone's truth table, what any signal or block computes — or
  anything derived from it, to help solve the puzzle. And never reading
  `puzzle.gds`, `layout.png`, or pre-restart artifacts.

Check that a build *works*; don't look at what it *says*. The decoding is done by
a person, locally, outside any AI session. `CLAUDE.md` states the rule.

Requirements: Python 3 (standard library only). `tests/testKlayoutNetlist.py`
needs the `klayout` Python module; `tests/testCellModels.py` needs `verilator`.
Both skip cleanly if the dependency is absent.

## Layout

```
puzzle.gds  layout.png                         shipped puzzle inputs (never read by an AI)
example_inputs.vcd                             shipped stimulus recording (readable: pin waveforms, not logic)
runPipeline.sh                                 stages 1-5, end to end
src/          pipeline modules (stages 1-5)
lib/          sky130_fd_sc_hd_merged.lef  (+ internal_cells.lef.template)
tests/        one synthetic-fixture suite per module
warmup/       the authors' small reference design, used as an end-to-end oracle
out/          pipeline outputs (git-ignored, reproducible)
bench/        optional Verilator sim harness — NOT part of stages 1-5
```

Run everything from the repo root — the tools resolve `lib/…lef` relative to the
working directory.

## What each file is for

| File | Stage | What it does |
|------|-------|--------------|
| `src/klayoutNetlist.py` | 1–3 | **Extraction.** Layout → gate-level netlist via KLayout's connectivity engine; pin and port names come from the layout's own labels. |
| `src/lefLib.py` | 1–3 | Cell outlines, pin geometry and directions from the LEF. |
| `src/genCellModels.py` | — | Cell behaviour: `FAMILY_FUNCS` / `SEQ_TEMPLATES` / `family_of`, the single source of truth for what a cell does. Also emits `lib/sky130_cells_sim.v` for the model test. |
| `src/coneDecompose.py` | 4 | **Flop-to-flop slicing.** Backward fan-in from every flop data pin and primary output to the nearest sequential boundary. → `CONES.json` |
| `src/boolExpr.py` | 5 | Boolean parsing, canonical normal form, bit-parallel evaluation. |
| `src/coneClasses.py` | 5 | **Cone groups.** Each cone's Boolean function + the functional equivalence classes over cones. → `CONE_CLASSES.json` |

## Running it

```sh
./runPipeline.sh          # GDS / OUT env vars override the defaults (puzzle.gds, out/)
```

which is:

```sh
python3 src/klayoutNetlist.py puzzle.gds        --out out/NETLIST_GRAPH.json
python3 src/coneDecompose.py  out/NETLIST_GRAPH.json --out out/CONES.json
python3 src/coneClasses.py    out/NETLIST_GRAPH.json out/CONES.json --out out/CONE_CLASSES.json
```

Run the tests with:

```sh
for t in tests/test*.py; do python3 "$t"; done
```

## Stages 1–3: `src/klayoutNetlist.py`

Hands the layout to KLayout's own connectivity extractor (`LayoutToNetlist`)
instead of intersecting polygons by hand. KLayout walks the routing stack
hierarchically, so the standard cells stay as subcircuits rather than dissolving
into transistors, and the pin labels the sky130 cells carry in their own GDS
(li1 text on 67/5, plus the metal text layers) give every pin and every primary
port its real name — `CLK`, `RESET_B`, `A`, `B`, `S` — rather than one inferred
from geometry.

No PDK LVS deck is needed and none is used: device recognition is what an LVS
deck is for, and a gate-level netlist does not want it. The only declaration is
which layers conduct and which vias join them. Output is a flat schema
(`top, cells, instances, ports, nets, tied_pins, const_nets, …`) that
`coneDecompose` consumes directly.

```sh
python3 src/klayoutNetlist.py puzzle.gds --out out/NETLIST_GRAPH.json
```

`tests/testKlayoutNetlist.py` checks a synthetic layout with a hand-worked
answer (two cells, one wire, a deliberate two-level via stack, a power strap that
must stay out of the netlist), then checks the warmup two ways: cell-for-cell
against the authors' own post-place-and-route DEF, and that `coneDecompose` runs
on the result.

**On instance counts.** A placed layout's GDS holds far more placements than it
holds logic. In `warmup/04_final.gds` the top cell has 1099 placements: 869 via
subcells, 151 fill/decap/tap, and 79 actual logic cells — logic is 7.2% of the
total. A raw placement count is not a cell count.

## Stage 4: `src/coneDecompose.py`

Walks backward from every sequential element's data inputs and every primary
output, collecting combinational logic until it reaches the nearest sequential
boundary — a register output, a primary input, or a constant. Each fan-in is one
cone, keyed by the net it computes, so logic shared by several registers yields
one cone with several recorded sinks.

Sequential-vs-combinational comes from the same cell-function tables that
generate the simulation models (`genCellModels`), so the two can never disagree,
and clock-like pins are excluded from the traversal (a flop's `CLK`, a latch's
`GATE`) — that is what makes the boundary sequential rather than merely
structural. Cells within a cone are listed in topological order, inputs first,
so stage 5 can evaluate a cone in one pass.

Cells outside every cone are classified rather than lumped together. Logic that
*drives* clock pins is the clock tree. So is a cell that merely *hangs off* a
clock net with its output unconnected: clock-tree synthesis inserts buffers as
pure capacitive load to balance skew, and their outputs are intentionally left
dangling. Only a cell that reaches neither a register, an output, nor a clock is
reported as genuinely dead.

```sh
python3 src/coneDecompose.py out/NETLIST_GRAPH.json --out out/CONES.json
```

Tested by `tests/testConeDecompose.py` against hand-built netlists with cones
worked out on paper: fan-in and topological order, sharing between registers,
register/port/constant/tied/opaque boundaries, a scan flop's several data roots,
a latch whose `GATE` is its clock, clock-tree vs dead-logic classification, and
a combinational loop that must be cut with a warning rather than hang.

## Stage 5: `src/coneClasses.py` (+ `src/boolExpr.py`)

Derives each cone's Boolean function from the cell-function tables and groups
cones computing the same function. The comparison depends on the cone's leaf
count *k*:

* **k ≤ 8** — an exact truth table (2^k entries), compared *under permutation of
  the inputs* so the same logic applied to different signals (a datapath bit
  slice) lands in one class. Sound and complete.
* **8 < k < 20** — the function is normalised rather than tabulated: DeMorgan
  pushes negations to the leaves, constants fold away, associative operators
  flatten, commutative operands are deduplicated and sorted, and identical
  subexpressions are shared (`boolExpr.py`). Equal keys prove equivalence;
  unequal keys do not disprove it, so these classes are reported as
  `normalised`, not exact.
* **k ≥ 20** — left unclassified with a stated reason rather than guessed at.

Each cone also gets its real functional *support* — the leaves its folded
function actually depends on, which can be a strict subset of what it is wired
to.

```sh
python3 src/coneClasses.py out/NETLIST_GRAPH.json out/CONES.json --out out/CONE_CLASSES.json
```

`tests/testConeClasses.py`, checked against functions worked out by hand:
DeMorgan pairs and mux expansions must normalise identically, truth tables must
match hand-computed values, input reordering must collapse while genuinely
different functions stay apart, a nand and a DeMorgan-expanded or must share a
class, three identical bit slices must collapse to one class of three, and a
9-leaf cone must use the normalised path rather than a table.

## Cell models: `src/genCellModels.py`

`coneDecompose` and `coneClasses` import `FAMILY_FUNCS`, `SEQ_TEMPLATES` and
`family_of` from here — the one place a cell's function is defined, so
sequential/combinational classification and Boolean evaluation can never drift
apart.

Run as a script it also emits `lib/sky130_cells_sim.v`: behavioral, zero-delay
models for all 441 macros in the LEF (Verilator can't simulate the official
sky130 sequential UDP primitives), port lists from the LEF signal pins, functions
from documented cell behaviour, every expression validated against the LEF pin
list at generation time. `tests/testCellModels.py` regenerates that file and
runs an exhaustive 256-vector truth-table check over 22 representative cells
against hand-written C++ expectations, plus a sequential scenario covering async
reset/set, both-asserted, enable/scan flops, latches, and clock gating.

## Tests

Everything in `tests/` is a synthetic fixture with hand-computed expectations;
`warmup/` is the authors' reference design, used as an end-to-end oracle. Run
from the repo root.

| Suite | Covers |
|-------|--------|
| `testKlayoutNetlist.py` | extraction — synthetic layout + warmup vs the authors' DEF |
| `testCellModels.py` | `genCellModels` — exhaustive comb + sequential scenarios (needs verilator) |
| `testConeDecompose.py` | stage 4 — cone slicing, boundaries, clock tree, loops |
| `testConeClasses.py` | stage 5 — `boolExpr` normal form, truth tables, equivalence classes |

## `bench/` — Verilator replay harness (optional, not a pipeline stage)

Replays a recorded waveform against the recovered logic. Two DUT generators read
`out/*.json`:

- `bench/netlistVerilog.py` → `netlistVerilog.v` — the flat cell-level netlist,
  buffers/inverters spliced out.
- `bench/conesToVerilog.py` → `ConesVerilog.v` — every cone as its own module
  (`CONE0`, `CONE1`, …) plus a `conesTop` that wires the cones + registers +
  clock tree back into the whole design.

Both expose the same interface (`I, clk, enable, rst_n` in; `O[7:0], success`
out). `bench/verilatorTB.h` is the shared harness (`setup` / `tick` / `finish`, a
sim-time counter, an error tally). `bench/tb_netlist.cpp` and `bench/tb_cones.cpp`
are matching testbenches in the house transaction / driver / monitor / scoreboard
style: each cycle drives `I` / `enable` / `rst_n` from the stimulus, then checks
the DUT's `O` / `success` against the recorded values and reports the first cycle
they diverge.

The stimulus lives *inside* each testbench rather than in a file it reads.
`bench/vcdToStimulus.py` samples the recorded VCD once per rising clock edge and
rewrites each `tb_*.cpp` in place, replacing the block between its
`// ---- BEGIN/END GENERATED STIMULUS` markers with

- `STIM_NAMES` / `STIM_WIDTHS` — comma-joined strings of the signals it found,
- **one binary string per signal covering the whole run**, `STIM_W_<sig>` chars
  per cycle, MSB first, back to back — `STIM_I` reads `"0100101101001…"`, the
  8-bit `STIM_O` `"0101010001001000…"` — with `'x'` marking a cycle the recording
  left at x/z. `STIM(sig, cycle)` decodes one cycle to a `uint64_t`, so the TB
  writes `tx->I = STIM(I, c)`,
- `STIM_KNOWN(sig, cycle)` — 1 if that cycle's chars were all `0`/`1`, 0 if any
  was `x`; the scoreboard compares only known cycles,
- `STIM_<sig>_TEXT` for every 8-bit signal: the same bytes as ASCII, one char per
  cycle (octal-escaped where not printable, `?` where the recording had x/z), so
  a recorded output that spells words is readable in the source,
- `STIM_HAS_<sig>` / `STIM_IX_<sig>` / `STIM_W_<sig>` for each signal, so a
  recording without `O` compiles (and skips the comparison) while a missing input
  is a compile error.

The Makefile builds the tracked `tb_*.cpp` directly — they carry their stimulus
and compile as they stand. `make stimulus` edits tracked source, so it is never a
build prerequisite; run it by hand when the recording changes.

```sh
make -C bench stimulus       # parse the recording -> the STIM block inside tb_*.cpp  (VCD=<path>, default ../example_inputs.vcd)
make -C bench netlist        # regen, build, replay -> "REPLAY: ... on all N cycles" / "first divergence at ..."
make -C bench cones
make -C bench lint-cones
make -C bench clean
```

Pass `+verbose` to the built binary for per-cycle detail. Everything under
`bench/` is git-ignored generated output (`*.v`, `stimulus.json`, `obj_dir_*`,
`*.vcd`) except the source files (`*.py`, `verilatorTB.h`, `tb_*.cpp`,
`Makefile`).

Under the rule above: the recording and the `STIM_*` strings it produces are pin
waveforms — readable. The DUT Verilog (`netlistVerilog.v`, `ConesVerilog.v`) is
the recovered circuit and is not. Running a bench target and reading the
`REPLAY:` summary line (cycle counts) or a lint result is a verification signal.
