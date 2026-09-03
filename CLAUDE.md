# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## What this is

A clean-room rebuild (started 2026-08-27, competition deadline 2026-09-04) of a
structure-recovery pipeline for the Jane Street ASIC reverse-engineering puzzle. A prior
attempt was deleted in full because it violated the competition's AI rules: Claude was
shown files derived from the puzzle GDS. Its files exist only in pre-restart git history.

Scope: **stages 1–6** — recover the netlist, slice it into flop-to-flop cones,
group cones by function, then solve the design for an input sequence that drives a
chosen output high. Other downstream stages (cone graph, backward walk, block
decomposition) were built and then removed on 2026-09-01.

Layout: `src/` holds the pipeline modules, `tests/` one fixture suite each, `lib/` the
LEF, `out/` the (git-ignored) outputs, `warmup/` the authors' reference design.
`bench/` is a self-contained Verilator sim harness (netlist / cone Verilog generators +
testbenches) — a tweaking aid, **not** a pipeline stage; nothing in `src/` depends on it.

Shipped puzzle inputs (the only non-tool files that belong at top level):

- `puzzle.gds` — the flattened layout to reverse-engineer
- `layout.png` — a render of the layout
- `example_inputs.vcd` — a recorded waveform of the design's pins, used as `bench/`
  stimulus (readable by Claude — see the exception in the rule below)
- `warmup/00_source.v` … `warmup/04_final.gds` — the authors' small reference design at
  every flow stage

## Rule: Claude never views the puzzle or anything derived from it

This is the competition's AI-usage rule and it is absolute:

- Claude must not open, read, dump, grep, render, screenshot, or otherwise inspect
  `puzzle.gds`,  `layout.png`, `warmup/04_final.gds`, or **any file
  produced by parsing or simulating them** — extracted netlists, connectivity graphs,
  JSON dumps, truth tables, waveforms, logs containing their contents. Not even for
  debugging. No throwaway scripts that print their contents.
- Reading vs using (see README, "The rule, and the line it draws"): Claude *may* run a
  stage or the whole pipeline on the real files and read **verification signals only** —
  exit codes, test pass/fail, structural counts like "N registers, 0 orphan cells".
  Claude must *not* read or reason about the circuit's content — the extracted netlist, a
  cone's truth table, what any signal or block computes. Check that a build works; do not
  look at what it says. If a check can't be made without seeing content, stop and hand it
  to the user.
- Claude builds general-purpose tooling and validates every stage on small **synthetic
  fixtures** it constructs itself, with hand-computed expected output. The user runs the
  finished pipeline on the real files locally, outside any AI session, and does not paste
  its output back.
- If a task appears to require the contents of a forbidden file, stop and say so rather
  than working around the rule.
- Claude CAN reconfigure input files of any kind and read any files to extract data to put into files but never give clues as to how the circuit function.
- Exception, decided 2026-09-02: the shipped stimulus recording `example_inputs.vcd`
  and everything derived from it — `bench/stimulus.json`, the `STIM_*` strings
  inlined into `bench/tb_*.cpp` — are readable. They are waveforms of the design's
  pins (a tool input), not the circuit: they show what was driven in and what came
  out, never how anything is computed. The forbidden list above is unchanged
  otherwise, and the recovered logic itself (`out/*.json`, `bench/netlistVerilog.v`,
  `bench/ConesVerilog.v`) stays off limits — as does stage 6's answer,
  `out/solution.bits`, which is the puzzle's solution outright. What Claude may
  read back from a stage-6 run is the verification signal only: sat/unsat, the
  cycle count, the structural counts in the preflight report, solve time, and
  whether the unique and robust checks passed.

The restriction is on *Claude viewing content*, not on the code: the pipeline scripts are
supposed to parse `puzzle.gds` when the user runs them.

## Pipeline (general-purpose, nothing puzzle-specific baked in)

1–3. Netlist extraction — `src/klayoutNetlist.py` hands the layout to KLayout's
   connectivity engine; standard cells stay as subcircuits, pin/port names come from
   the layout's own labels. → `out/NETLIST_GRAPH.json`
4. Cone decomposition — `src/coneDecompose.py`, backward fan-in from each register data
   pin / primary output to the nearest sequential boundary. Each leaf also carries
   `from_cone`: for a register output, the cone feeding that register's data pin, so a
   reader can walk from a cone to the ones upstream of it. Other leaf kinds — and a
   register whose data pin has no cone, or several ambiguous ones — get `null`, and the
   consumer names the net instead. → `out/CONES.json`
5. Cone groups — `src/coneClasses.py` (+ `src/boolExpr.py`), each cone's Boolean
   function and the functional equivalence classes over cones. → `out/CONE_CLASSES.json`
6. Input solving — `src/symSolve.py`, bounded model checking. Unrolls the
   sequential netlist one step per rising clock edge, makes the nominated input
   pins free Booleans, and asks Z3 for an assignment that drives a chosen output
   high; deepens the bound one cycle at a time so it reports the *earliest*
   cycle. `--check-unique` looks for a second answer, `--check-robust` proves the
   answer works from every power-on state. Needs `z3-solver`.
   → `out/SOLUTION.json`, `out/solution.bits`

`src/decomposeCone.py` is an **analysis tool, not a pipeline stage**: nothing in the
pipeline depends on it and `runPipeline.sh` does not run it. Given one cone and a target
value it asks Z3 which assignments to that cone's boundary inputs force its output --
`--mode implicants` (the default: an irredundant cover of prime implicants, don't-cares
shown as `-`), `models`, `support` (which inputs can affect the output at all, proven by
UNSAT), or `count`. `CONE7`, `cone7` and `7` all name cone id 7, which is also the module
name `bench/conesToVerilog.py` gives it; `--by-root NET` addresses one by root net.
Every boundary input is listed with where it comes from — the cone feeding it via
stage 4's `from_cone`, or what it is (primary input, black box, undriven) when nothing
upstream is a cone — in the human report and as `input_origins` / `input_cones` in
`--json`. A CONES.json predating the tag still works: it is recomputed on load.
A block with two outputs is two cones: `--also CONE9=1` puts them in one query over a
shared boundary, which is not the same as two separate runs (separately reachable
targets can be jointly unreachable). Nothing enumerates a truth table: cubes are grown
by SAT queries and shrunk by their unsat cores, `count` sums disjoint cubes rather than
solving once per solution, and `--budget SEC` caps the whole run — a search stopped by
`--limit`, `--budget` or `--timeout` keeps what it proved and says so instead of
reporting UNSAT. `--seed` makes runs reproducible. It
adds no cell semantics of its own -- combinational cells go through `FAMILY_FUNCS` and
the `boolExpr` parser, and `symSolve.z3_from_ast` turns them into Z3 terms -- and it
refuses rather than guesses on an unmodelled family, a tri-state cell, or a sequential
cell inside a cone. Needs `z3-solver`. Its output is circuit content, so the
reading-vs-using rule applies to it in full.

`src/genCellModels.py` is the shared cell-function source of truth (imported by stages
4, 5 and 6, and by `decomposeCone`). Every stage ships with its synthetic-fixture test
in `tests/`; a stage is done when the fixture passes. `./runPipeline.sh` runs 1–5 end to
end; the README documents it.

Stage 6 is opt-in, because it is the expensive stage and has to be told what to solve
for. `SOLVE=preflight ./runPipeline.sh` adds a structure report (register counts, clock
domains, how many registers have no reset) and stops there; `SOLVE=1 SYMBOLIC=I
SYMBOLIC_WHEN=enable MAX_CYCLES=400 ./runPipeline.sh` solves. It restates no circuit
semantics: combinational cells go through `FAMILY_FUNCS` and the `boolExpr` parser, the
register inventory comes from stage 4. The one table it adds is `SEQ_SPECS`, a
declarative reading of `SEQ_TEMPLATES` (whose Verilog bodies are procedural and cannot
be evaluated as expressions); `tests/testSymSolve.py` fails if the two disagree about a
family or a pin. Anything it cannot model — an unknown sequential family, an
unresolvable clock, a transparent latch, an unconnected data pin with no inactive value
— is a hard error naming the cells, never a silent guess, because a mis-modelled clock
gate yields a confidently wrong answer.

## bench/ — the replay harness (not a pipeline stage)

`bench/vcdToStimulus.py` samples `example_inputs.vcd` once per rising clock edge and
rewrites `bench/tb_cones.cpp` and `bench/tb_netlist.cpp` **in place**, replacing the block
between their `// ---- BEGIN/END GENERATED STIMULUS` markers. Each signal becomes one
binary string covering the whole run — `STIM_W_<sig>` chars per cycle, MSB first, back to
back, `'x'` for a cycle the recording left unknown — read with `STIM(sig, c)` and
`STIM_KNOWN(sig, c)`. The two testbenches must end up with byte-identical stimulus blocks.

The testbenches are tracked and compile as they stand; there is no `tb_*.gen.cpp`
indirection any more (removed 2026-09-02). `make -C bench stimulus` edits tracked source,
so it is never a build prerequisite — run it by hand when the recording changes, and after
running it check that the file is not open in an editor with a stale buffer (one such
save silently corrupted a `STIM_I` string on 2026-09-02).

`bench/conesToVerilog.py` puts a comment above each cone module naming every input and
where it comes from — the upstream cone from stage 4's `from_cone`, the port name for a
primary input, the net itself when neither applies. `--no-origins` omits it.

`bench/ConesVerilog.v` is git-ignored and may carry hand edits that the generator will
not reproduce — regenerate it only when the user asks, and back it up first.
