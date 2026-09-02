# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## What this is

A clean-room rebuild (started 2026-08-27, competition deadline 2026-09-04) of a
structure-recovery pipeline for the Jane Street ASIC reverse-engineering puzzle. A prior
attempt was deleted in full because it violated the competition's AI rules: Claude was
shown files derived from the puzzle GDS. Its files exist only in pre-restart git history.

Scope: **stages 1–5 only** — recover the netlist, slice it into flop-to-flop cones,
group cones by function. Other downstream stages (cone graph, backward walk, block
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
  `bench/ConesVerilog.v`) stays off limits.

The restriction is on *Claude viewing content*, not on the code: the pipeline scripts are
supposed to parse `puzzle.gds` when the user runs them.

## Pipeline (general-purpose, nothing puzzle-specific baked in)

1–3. Netlist extraction — `src/klayoutNetlist.py` hands the layout to KLayout's
   connectivity engine; standard cells stay as subcircuits, pin/port names come from
   the layout's own labels. → `out/NETLIST_GRAPH.json`
4. Cone decomposition — `src/coneDecompose.py`, backward fan-in from each register data
   pin / primary output to the nearest sequential boundary. → `out/CONES.json`
5. Cone groups — `src/coneClasses.py` (+ `src/boolExpr.py`), each cone's Boolean
   function and the functional equivalence classes over cones. → `out/CONE_CLASSES.json`

`src/genCellModels.py` is the shared cell-function source of truth (imported by stages
4 and 5). Every stage ships with its synthetic-fixture test in `tests/`; a stage is done
when the fixture passes. `./runPipeline.sh` runs 1–5 end to end; the README documents it.

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

Known issue: `bench/ConesVerilog.v` does not elaborate — `cone54` appears in the
`conesTop` port list with no matching input/output declaration. Unresolved; it is a
`conesToVerilog.py` or hand-edit problem, not a stimulus one. `make -C bench netlist`
builds and replays fine.
