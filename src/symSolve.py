#!/usr/bin/env python3
"""symSolve.py — stage 6: solve the recovered netlist for an input sequence.

Bounded model checking over the design stages 1-4 recovered. The sequential
netlist is unrolled cycle by cycle into a Boolean formula — every register
becomes one state variable per cycle, every gate one constrained variable per
cycle, and the input pins you nominate become free variables — and Z3 is asked
for an assignment that drives a chosen output high.

The circuit semantics are not restated here. Combinational cells are evaluated
through the same ``FAMILY_FUNCS`` expressions that generate the simulation
models, parsed by the same ``boolExpr`` parser stage 5 uses; the register
inventory comes from stage 4's ``CONES.json``. The one thing this file adds is
``SEQ_SPECS`` — a declarative reading of ``genCellModels.SEQ_TEMPLATES``, whose
Verilog bodies are procedural and cannot be evaluated as expressions. That table
is cross-checked against SEQ_TEMPLATES by tests/testSymSolve.py, which fails if
a family is missing from either side or names a pin the template never mentions.

Timing model
------------
One solver step per rising edge of the root clock. Each step samples the
combinational logic just before the edge, exactly where ``vcdToStimulus.py``
samples the recording, so a solution found here is directly replayable by the
bench testbenches.

  * A flop on the negedge of an inverted clock (``dfrtn`` fed by an inverter) is
    a posedge flop of the root clock, and is modelled as one. A flop that really
    does update on the opposite edge is rejected unless --allow-half-cycle.
  * Clock gates (``dlclkp``/``sdlclkp``) are resolved by walking the clock tree:
    the gated flop keeps its value on a cycle its gate is closed. GATE is sampled
    at the same point as the rest of the cycle, which is what the cell's own
    low-phase latch does.
  * Async reset/set is applied to the state a register *shows* during a cycle,
    not only at the edge, whenever the control net is a function of primary
    inputs alone (the normal case: a reset pin). When it is driven by internal
    logic the override happens at the edge only, and the count is reported.
  * Transparent latches have no cycle-accurate model here and are rejected;
    --latch-as-flop opts in to treating them as edge-triggered, which is an
    approximation, not a model.

Anything else — a sequential family with no spec, an unresolvable clock, an
unconnected data pin with no inactive value — is a hard error naming the cells,
never a silent guess. A mis-modelled clock gate produces a confidently wrong
answer, which is the one outcome worth failing loudly to avoid.

Usage
-----
    python3 src/symSolve.py out/NETLIST_GRAPH.json out/CONES.json \\
        --stimulus bench/stimulus.json --clock clk \\
        --symbolic I --symbolic-when enable \\
        --goal success --max-cycles 400 --check-unique --check-robust \\
        --out out/SOLUTION.json --bits-out out/solution.bits

    python3 src/symSolve.py ... --preflight     # structure report, no solving

Output (SOLUTION.json):

{
  "status": "sat" | "unsat" | "unknown",
  "goal": {"port": str, "value": 0|1, "cycle": int|null},
  "cycles_unrolled": int,
  "answer": {"port": str, "bits": "0101...", "cycles": [int], "n_bits": int},
  "unique": bool|null,        # --check-unique: no other assignment succeeds
  "robust": bool|null,        # --check-robust: succeeds for every initial state
  "preflight": {...},         # structural counts only
  "stats": {...},
  "warnings": [...]
}

``--smt2 FILE`` dumps the unrolled formula instead of solving it, ready to run
(it ends in ``(check-sat)``). ``--dimacs FILE`` Tseitin-transforms it to CNF for
an external SAT solver — CaDiCaL, Kissat — which on a pure Boolean unrolling is
usually much faster than Z3's default tactic. The CNF carries ``c <var> <name>``
comment lines, so an external solver's model maps straight back: the answer bits
are the variables named ``in.<port>@<cycle>``.
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict

import boolExpr
from coneDecompose import Netlist
from genCellModels import FAMILY_FUNCS, SEQ_TEMPLATES, family_of

sys.setrecursionlimit(200000)


class ModelError(Exception):
    """The netlist uses something this unroller will not guess at."""


# --------------------------------------------------------------- sequential specs
# A declarative reading of genCellModels.SEQ_TEMPLATES: which pin is the clock,
# which the data, which the asynchronous controls. Kept as data so the fixture
# suite can check it against the templates family by family.

class SeqSpec(object):
    __slots__ = ("kind", "clock", "negedge", "data", "enable", "scan",
                 "reset_b", "set_b", "gate", "gate_high", "split_qn", "outputs")

    def __init__(self, kind, clock=None, negedge=False, data=None, enable=None,
                 scan=None, reset_b=None, set_b=None, gate=None,
                 gate_high=True, split_qn=False, outputs=("Q",)):
        self.kind = kind                  # "flop" | "latch" | "clkgate"
        self.clock = clock
        self.negedge = negedge            # template triggers on negedge of `clock`
        self.data = data
        self.enable = enable              # None | "DE" | "SCE|DE"
        self.scan = scan                  # None | (select pin, data pin)
        self.reset_b = reset_b            # active-low async clear to 0
        self.set_b = set_b                # active-low async set to 1
        self.gate = gate                  # latch / clock-gate enable pin
        self.gate_high = gate_high        # gate asserted high, else low
        self.split_qn = split_qn          # Q_N is its own bit, not ~Q
        self.outputs = outputs

    def inputs(self):
        """The input pins whose value this cycle decides the next state."""
        out = set()
        if self.data:
            out.add(self.data)
        if self.scan:
            out.update(self.scan)
        if self.enable:
            out.update(self.enable.split("|"))
        for p in (self.reset_b, self.set_b, self.gate):
            if p:
                out.add(p)
        return out

    def pins(self):
        """Every pin name the spec refers to — what the fixture suite checks."""
        out = self.inputs() | set(self.outputs)
        if self.clock:
            out.add(self.clock)
        return out


def _flop(**kw):
    return SeqSpec("flop", clock=kw.pop("clock", "CLK"), data="D", **kw)


_SCAN = ("SCE", "SCD")
_QQN = ("Q", "Q_N")

SEQ_SPECS = {
    # ---- edge-triggered flops
    "dfxtp":   _flop(),
    "dfxbp":   _flop(outputs=_QQN),
    "dfrtp":   _flop(reset_b="RESET_B"),
    "dfrbp":   _flop(reset_b="RESET_B", outputs=_QQN),
    "dfrtn":   _flop(clock="CLK_N", negedge=True, reset_b="RESET_B"),
    "dfstp":   _flop(set_b="SET_B"),
    "dfsbp":   _flop(set_b="SET_B", outputs=_QQN),
    "dfbbp":   _flop(reset_b="RESET_B", set_b="SET_B", split_qn=True, outputs=_QQN),
    "dfbbn":   _flop(clock="CLK_N", negedge=True, reset_b="RESET_B", set_b="SET_B",
                     split_qn=True, outputs=_QQN),
    "edfxtp":  _flop(enable="DE"),
    "edfxbp":  _flop(enable="DE", outputs=_QQN),
    "sdfxtp":  _flop(scan=_SCAN),
    "sdfxbp":  _flop(scan=_SCAN, outputs=_QQN),
    "sdfrtp":  _flop(scan=_SCAN, reset_b="RESET_B"),
    "sdfrbp":  _flop(scan=_SCAN, reset_b="RESET_B", outputs=_QQN),
    "sdfrtn":  _flop(clock="CLK_N", negedge=True, scan=_SCAN, reset_b="RESET_B"),
    "sdfstp":  _flop(scan=_SCAN, set_b="SET_B"),
    "sdfsbp":  _flop(scan=_SCAN, set_b="SET_B", outputs=_QQN),
    "sdfbbp":  _flop(scan=_SCAN, reset_b="RESET_B", set_b="SET_B",
                     split_qn=True, outputs=_QQN),
    "sdfbbn":  _flop(clock="CLK_N", negedge=True, scan=_SCAN, reset_b="RESET_B",
                     set_b="SET_B", split_qn=True, outputs=_QQN),
    "sedfxtp": _flop(scan=_SCAN, enable="SCE|DE"),
    "sedfxbp": _flop(scan=_SCAN, enable="SCE|DE", outputs=_QQN),

    # ---- transparent latches (rejected unless --latch-as-flop)
    "dlxtp": SeqSpec("latch", data="D", gate="GATE"),
    "dlxbp": SeqSpec("latch", data="D", gate="GATE", outputs=_QQN),
    "dlxtn": SeqSpec("latch", data="D", gate="GATE_N", gate_high=False),
    "dlxbn": SeqSpec("latch", data="D", gate="GATE_N", gate_high=False, outputs=_QQN),
    "dlrtp": SeqSpec("latch", data="D", gate="GATE", reset_b="RESET_B"),
    "dlrbp": SeqSpec("latch", data="D", gate="GATE", reset_b="RESET_B", outputs=_QQN),
    "dlrtn": SeqSpec("latch", data="D", gate="GATE_N", gate_high=False,
                     reset_b="RESET_B"),
    "dlrbn": SeqSpec("latch", data="D", gate="GATE_N", gate_high=False,
                     reset_b="RESET_B", outputs=_QQN),
    "lpflow_inputisolatch": SeqSpec("latch", data="D", gate="SLEEP_B"),

    # ---- integrated clock gates: no state of their own in a cycle model, the
    # gate value sampled this cycle is the enable applied at this edge.
    "dlclkp":  SeqSpec("clkgate", clock="CLK", gate="GATE", outputs=("GCLK",)),
    "sdlclkp": SeqSpec("clkgate", clock="CLK", gate="GATE", enable="GATE|SCE",
                       outputs=("GCLK",)),
}

# Control pins with an unambiguous inactive value, used when the layout leaves
# them unconnected. Anything not listed here is an error rather than a guess.
DEASSERTED = {"RESET_B": True, "SET_B": True, "SLEEP_B": True,
              "SCE": False, "SCD": False}

BUF_FUNCS = {"X": "A"}
INV_FUNCS = {"Y": "~A"}

# A bit-blasted port name, as the layout's labels spell it: `O[3]`.
BUS_RE = re.compile(r"^(.*)\[(\d+)\]$")


# ------------------------------------------------------------------- expressions

def z3_from_ast(z3, ast, env):
    """One parsed cell expression -> a Z3 term over the pin terms in `env`."""
    kind = ast[0]
    if kind == "lit":
        return z3.BoolVal(bool(ast[1]))
    if kind == "name":
        if ast[1] not in env:
            raise ModelError("no value for pin %r" % ast[1])
        return env[ast[1]]
    if kind == "not":
        return z3.Not(z3_from_ast(z3, ast[1], env))
    if kind == "and2":
        return z3.And(z3_from_ast(z3, ast[1], env), z3_from_ast(z3, ast[2], env))
    if kind == "or2":
        return z3.Or(z3_from_ast(z3, ast[1], env), z3_from_ast(z3, ast[2], env))
    if kind == "xor2":
        return z3.Xor(z3_from_ast(z3, ast[1], env), z3_from_ast(z3, ast[2], env))
    if kind == "mux":
        return z3.If(z3_from_ast(z3, ast[1], env), z3_from_ast(z3, ast[2], env),
                     z3_from_ast(z3, ast[3], env))
    raise ModelError("unknown expression node %r" % (ast,))


def cell_expressions(cell_name):
    """{output pin: parsed AST} for a combinational cell, or None if unmodelled."""
    funcs = FAMILY_FUNCS.get(family_of(cell_name))
    if funcs is None:
        return None
    try:
        return {pin: boolExpr.parse(text) for pin, text in funcs.items()}
    except boolExpr.ParseError:
        return None                      # tri-state cells (1'bz) have no model


# ------------------------------------------------------------------- clock tree

def resolve_clock(nl, inst, pin):
    """Walk back from a register's clock pin to the root clock.

    -> (root, parity, enables)  where `root` is ("port", name) / ("const", v) /
    ("undriven", net), `parity` counts inversions, and `enables` is the list of
    clock-gate instances between the register and the root.
    """
    net = nl.net_of.get((inst, pin))
    if net is None:
        raise ModelError("%s.%s (clock) is unconnected" % (inst, pin))
    parity = 0
    enables = []
    seen = set()
    while True:
        if net in seen:
            raise ModelError("loop in the clock tree at net %d" % net)
        seen.add(net)
        if net in nl.const_of_net:
            return ("const", nl.const_of_net[net]), parity, enables
        for name, direction in nl.port_of_net.get(net, []):
            if direction == "input":
                return ("port", name), parity, enables
        drv = nl.driver.get(net)
        if drv is None:
            return ("undriven", net), parity, enables
        _k, dinst, _dpin = drv
        fam = family_of(nl.inst_cell[dinst])
        spec = SEQ_SPECS.get(fam)
        if spec is not None and spec.kind == "clkgate":
            enables.append(dinst)
            net = nl.net_of.get((dinst, spec.clock))
            if net is None:
                raise ModelError("%s.%s (clock gate input) is unconnected"
                                 % (dinst, spec.clock))
            continue
        funcs = FAMILY_FUNCS.get(fam)
        if funcs == BUF_FUNCS or funcs == INV_FUNCS:
            if funcs == INV_FUNCS:
                parity ^= 1
            net = nl.net_of.get((dinst, "A"))
            if net is None:
                raise ModelError("%s.A (clock buffer input) is unconnected" % dinst)
            continue
        raise ModelError("cannot resolve the clock of %s.%s: net %d is driven by "
                         "%s (%s), which is neither a buffer, an inverter, nor a "
                         "clock gate" % (inst, pin, net, dinst, nl.inst_cell[dinst]))


# ------------------------------------------------------------------- the unroller

class Unroller(object):
    """Builds the cycle-by-cycle Boolean encoding of one netlist."""

    def __init__(self, graph, cones, opts, warnings):
        import z3                                   # deferred: --preflight needs none
        self.z3 = z3
        self.opts = opts
        self.warnings = warnings
        self.nl = Netlist(graph, warnings)
        self.graph = graph
        self.cones = cones

        self.ports = {p["name"]: p for p in graph["ports"]}
        self.out_ports = [p for p in graph["ports"] if p["direction"] == "output"]
        self.in_ports = [p for p in graph["ports"] if p["direction"] == "input"
                         and p["name"] != opts.clock]

        self.expr_cache = {}
        self.free_vars = []          # ports/pins the solver may choose freely
        self.sym_bits = {}           # (port, cycle) -> Bool, the answer we want
        self.counts = defaultdict(int)
        self.solver = z3.Solver()
        if opts.timeout:
            self.solver.set("timeout", int(opts.timeout * 1000))

        self._classify_registers()
        self._resolve_clocks()
        self._mark_static()
        self._find_undriven()
        self._init_state()

        self.netval = {}             # (net, cycle) -> Bool term
        self.on_stack = set()
        self.reach = []              # reached[t]: goal seen at or before cycle t
        self.hit_terms = []          # hit[t]: goal held at cycle t exactly
        self.gate_out = {r["inst"] for r in self.clkgates}

    # ------------------------------------------------------------- construction

    def _classify_registers(self):
        """Split stage 4's register inventory into flops, latches and gates."""
        self.flops, self.latches, self.clkgates = [], [], []
        unsupported = defaultdict(list)
        for reg in self.cones["registers"]:
            fam = family_of(reg["cell"])
            spec = SEQ_SPECS.get(fam)
            if spec is None:
                unsupported[fam].append(reg["inst"])
                continue
            reg = dict(reg, family=fam, spec=spec)
            if spec.kind == "flop":
                self.flops.append(reg)
            elif spec.kind == "latch":
                self.latches.append(reg)
            else:
                self.clkgates.append(reg)
        if unsupported:
            raise ModelError(
                "no sequential model for %d cell famil%s: %s"
                % (len(unsupported), "y" if len(unsupported) == 1 else "ies",
                   ", ".join("%s (%d instances, e.g. %s)"
                             % (f, len(v), v[0]) for f, v in sorted(unsupported.items()))))
        if self.latches and not self.opts.latch_as_flop:
            fams = sorted({family_of(r["cell"]) for r in self.latches})
            raise ModelError(
                "%d transparent latches (%s) have no cycle-accurate model here. "
                "Pass --latch-as-flop to approximate them as edge-triggered — an "
                "approximation, not a model — or solve a design without them."
                % (len(self.latches), ", ".join(fams)))
        self.state_regs = self.flops + self.latches

    def _resolve_clocks(self):
        """Every flop's clock -> root domain, edge, and the gates above it."""
        self.clock_of = {}
        self.domains = defaultdict(int)
        half_cycle = []
        for reg in self.state_regs:
            spec = reg["spec"]
            if spec.kind == "latch":
                self.clock_of[reg["inst"]] = (None, [])
                continue
            root, parity, enables = resolve_clock(self.nl, reg["inst"], spec.clock)
            if root[0] == "const":
                self.warnings.append("%s never clocks: its clock net is constant %s"
                                     % (reg["inst"], root[1]))
                self.counts["never_clocked"] += 1
            elif root[0] == "undriven":
                self.warnings.append("%s has an undriven clock (net %d)"
                                     % (reg["inst"], root[1]))
                self.counts["never_clocked"] += 1
            else:
                self.domains[root[1]] += 1
            # negedge of an inverted clock is posedge of the root clock
            if spec.negedge ^ bool(parity):
                half_cycle.append(reg["inst"])
            self.clock_of[reg["inst"]] = (root, enables)
            self.counts["gated_flops"] += 1 if enables else 0
        if half_cycle and not self.opts.allow_half_cycle:
            raise ModelError(
                "%d registers update on the opposite edge of the root clock "
                "(e.g. %s). One solver step per rising edge cannot represent that. "
                "Pass --allow-half-cycle to model them on the rising edge anyway."
                % (len(half_cycle), half_cycle[0]))
        self.counts["half_cycle_flops"] = len(half_cycle)
        if len(self.domains) > 1:
            self.warnings.append(
                "registers are clocked from %d different root clocks (%s); every "
                "one is stepped together, which is only right if they are the same "
                "clock" % (len(self.domains), ", ".join(sorted(self.domains))))

    def _mark_static(self):
        """Nets whose fan-in reaches only primary inputs and constants.

        An async reset driven by such a net can be applied to the state a
        register shows *during* a cycle, because evaluating it never needs that
        state back. Anything else is applied at the clock edge only.
        """
        memo = {}
        stack_guard = set()

        def is_static(net):
            if net in memo:
                return memo[net]
            if net in stack_guard:
                return False
            kind, payload = self.nl.source_of(net)
            if kind == "leaf":
                memo[net] = payload["kind"] in ("port", "const")
                return memo[net]
            stack_guard.add(net)
            ok = True
            for _pin, src in self.nl.input_nets(payload):
                if isinstance(src, tuple) or src is None:
                    continue             # tied constant / unconnected
                if not is_static(src):
                    ok = False
                    break
            stack_guard.discard(net)
            memo[net] = ok
            return ok

        self.is_static = is_static

    def _find_undriven(self):
        """Nets a cell reads that nothing drives.

        Each is a free bit *every cycle* unless it is tied, which gives the solver
        a channel the real chip does not have. Worth knowing about before solving
        rather than discovering it in an answer that will not replay.
        """
        self.undriven_nets = set()
        for inst in self.graph["instances"]:
            for _pin, src in self.nl.input_nets(inst["id"]):
                if not isinstance(src, int):
                    continue
                kind, payload = self.nl.source_of(src)
                if kind == "leaf" and payload["kind"] == "undriven":
                    self.undriven_nets.add(src)
        if self.undriven_nets and self.opts.tie_undriven == "free":
            self.warnings.append(
                "%d net(s) are read as data but driven by nothing; each is a free "
                "bit every cycle that the solver may exploit. Pin them with "
                "--tie-undriven 0 (or 1), or rely on --check-robust to catch an "
                "answer that leans on one." % len(self.undriven_nets))

    def _init_state(self):
        """One state bit per register output, at cycle 0."""
        z3 = self.z3
        self.state = {}                      # (inst, "Q"/"Q_N") -> Bool at cycle t
        self.state_keys = []
        self.init_vars = []
        for reg in self.state_regs:
            spec = reg["spec"]
            keys = [(reg["inst"], "Q")]
            if spec.split_qn:
                keys.append((reg["inst"], "Q_N"))
            for k in keys:
                self.state_keys.append(k)
                v = z3.Bool("q.%s.%s@0" % k)
                self.state[k] = v
                self.init_vars.append(v)
                if self.opts.init == "zero":
                    self.solver.add(z3.Not(v) if k[1] == "Q" else v)
        self.counts["state_bits"] = len(self.state_keys)
        self.counts["unreset_regs"] = sum(
            1 for r in self.state_regs
            if not r["spec"].reset_b and not r["spec"].set_b)

    # ------------------------------------------------------------ pin resolution

    def define(self, name, expr):
        """Name a term and constrain it, so the formula stays flat."""
        v = self.z3.Bool(name)
        self.solver.add(v == expr)
        self.counts["definitions"] += 1
        return v

    def free(self, name):
        v = self.z3.Bool(name)
        self.free_vars.append(v)
        return v

    def port_value(self, name, cycle):
        """The term driven onto one primary input this cycle."""
        z3 = self.z3
        opts = self.opts
        if name in opts.drive:
            return z3.BoolVal(bool(opts.drive[name]))
        symbolic = name in opts.symbolic
        if symbolic and opts.symbolic_when:
            gate = self.stim_value(opts.symbolic_when, cycle)
            symbolic = gate == 1
        if symbolic:
            v = self.free("in.%s@%d" % (name, cycle))
            self.sym_bits[(name, cycle)] = v
            return v
        rec = self.stim_value(name, cycle)
        if rec is None:
            self.counts["unstimulated_input_bits"] += 1
            return self.free("in.%s@%d" % (name, cycle))
        return z3.BoolVal(bool(rec))

    def stim_value(self, name, cycle):
        """Recorded value of a signal at a cycle, or None if not recorded/known.

        A bit-blasted port (`O[3]`) is matched against the recording's reassembled
        bus (`O`, bit 3), because vcdToStimulus.py joins buses back up while the
        layout's labels keep them apart.
        """
        stim = self.opts.stimulus
        if not stim:
            return None
        bit = None
        if name not in stim["index"]:
            m = BUS_RE.match(name)
            if not m or m.group(1) not in stim["index"]:
                return None
            name, bit = m.group(1), int(m.group(2))
        i = stim["index"][name]
        rows = stim["cycles"]
        if cycle >= rows:
            if self.opts.extend == "hold" and rows:
                cycle = rows - 1
            elif self.opts.extend in ("zero", "one"):
                return 0 if self.opts.extend == "zero" else 1
            else:
                return None
        if not stim["known"][cycle][i]:
            return None
        value = stim["value"][cycle][i]
        if bit is not None:
            return (value >> bit) & 1
        if value not in (0, 1):
            self.counts["wide_scalar_bits"] += 1
            if self.counts["wide_scalar_bits"] == 1:
                self.warnings.append(
                    "recorded signal %r is wider than the 1-bit port of the same "
                    "name; only its low bit is used" % name)
        return value & 1

    # -------------------------------------------------------------- evaluation

    def net_value(self, net, cycle):
        """The term on one net at one cycle, memoised, gates defined lazily."""
        key = (net, cycle)
        if key in self.netval:
            return self.netval[key]
        if key in self.on_stack:
            # a combinational loop; stage 4 cut it too. Cut it as a free bit and
            # report it rather than diverging.
            self.counts["cut_loop_nets"] += 1
            v = self.free("cut.n%d@%d" % (net, cycle))
            self.netval[key] = v
            return v

        kind, payload = self.nl.source_of(net)
        if kind == "leaf":
            v = self.leaf_value(payload, cycle)
            self.netval[key] = v
            return v

        inst = payload
        self.on_stack.add(key)
        try:
            self.eval_cell(inst, cycle)
        finally:
            self.on_stack.discard(key)
        if key not in self.netval:
            raise ModelError("net %d was not produced by its driver %s" % (net, inst))
        return self.netval[key]

    def leaf_value(self, leaf, cycle):
        z3 = self.z3
        k = leaf["kind"]
        if k == "const":
            return z3.BoolVal(leaf["value"] == "1")
        if k == "port":
            return self.port_value(leaf["name"], cycle)
        if k == "reg":
            return self.reg_output(leaf["inst"], leaf["pin"], cycle)
        if k == "opaque":
            self.counts["opaque_leaf_bits"] += 1
            return self.free("bb.%s.%s@%d" % (leaf["inst"], leaf["pin"], cycle))
        self.counts["undriven_leaf_bits"] += 1
        if self.opts.tie_undriven != "free":
            return z3.BoolVal(self.opts.tie_undriven == "1")
        return self.free("undriven.n%s@%d" % (leaf.get("net", "x"), cycle))

    def reg_output(self, inst, pin, cycle):
        """A sequential cell's output as the rest of the design sees it."""
        if inst in self.gate_out:                    # a clock gate's GCLK
            raise ModelError("%s.%s (a gated clock) is read as data" % (inst, pin))
        vis = self.vis[inst]
        if pin == "Q":
            return vis[0]
        if pin == "Q_N":
            return vis[1]
        raise ModelError("%s has no output pin %s" % (inst, pin))

    def eval_cell(self, inst, cycle):
        """Define every output net of one combinational cell at one cycle."""
        cell = self.nl.inst_cell[inst]
        exprs = self.expr_cache.get(cell, "miss")
        if exprs == "miss":
            exprs = cell_expressions(cell)
            self.expr_cache[cell] = exprs
        if exprs is None:
            raise ModelError("cell %s (%s) has no Boolean model" % (inst, cell))

        env = {}
        for pin, src in self.nl.input_nets(inst):
            if isinstance(src, tuple):
                env[pin] = self.z3.BoolVal(src[1] == "1")
            elif src is None:
                self.counts["unconnected_gate_pins"] += 1
                env[pin] = self.free("nc.%s.%s@%d" % (inst, pin, cycle))
            else:
                env[pin] = self.net_value(src, cycle)

        for pin, ast in exprs.items():
            out_net = self.nl.net_of.get((inst, pin))
            if out_net is None:
                continue                            # unused output
            term = z3_from_ast(self.z3, ast, env)
            self.netval[(out_net, cycle)] = self.define(
                "g.%s.%s@%d" % (inst, pin, cycle), term)

    def pin_value(self, reg, pin, cycle):
        """A sequential cell's data/control pin this cycle."""
        z3 = self.z3
        inst = reg["inst"]
        net = self.nl.net_of.get((inst, pin))
        if net is not None:
            return self.net_value(net, cycle)
        tied = self.nl.tied.get((inst, pin))
        if tied is not None:
            return z3.BoolVal(tied == "1")
        override = self.opts.tie.get("%s.%s" % (inst, pin), self.opts.tie.get(pin))
        if override is not None:
            return z3.BoolVal(bool(override))
        if pin in DEASSERTED:
            self.counts["deasserted_pins"] += 1
            return z3.BoolVal(DEASSERTED[pin])
        if self.opts.free_unconnected:
            self.counts["free_unconnected_pins"] += 1
            return self.free("nc.%s.%s@%d" % (inst, pin, cycle))
        raise ModelError(
            "%s.%s (%s) is unconnected and has no inactive value. Give it one with "
            "--tie %s=0 (or --tie %s.%s=0), or pass --free-unconnected to let the "
            "solver choose it — which lets it choose a solution real hardware would "
            "not reproduce." % (inst, pin, reg["cell"], pin, inst, pin))

    # ---------------------------------------------------------- sequential step

    def async_override(self, reg, q, qn, pinval):
        """(Q, Q_N) after an asserted async set/reset, per the cell's priority."""
        z3 = self.z3
        spec = reg["spec"]
        rst = pinval.get(spec.reset_b) if spec.reset_b else None
        setb = pinval.get(spec.set_b) if spec.set_b else None
        if rst is None and setb is None:
            return q, qn
        if spec.split_qn:
            # Both asserted is the real cell's state: Q and Q_N both high.
            # SET_B wins on Q, RESET_B wins on Q_N.
            nq = z3.If(z3.Not(setb), z3.BoolVal(True),
                       z3.If(z3.Not(rst), z3.BoolVal(False), q))
            nqn = z3.If(z3.Not(rst), z3.BoolVal(True),
                        z3.If(z3.Not(setb), z3.BoolVal(False), qn))
            return nq, nqn
        if rst is not None:
            nq = z3.If(z3.Not(rst), z3.BoolVal(False), q)
        else:
            nq = z3.If(z3.Not(setb), z3.BoolVal(True), q)
        return nq, z3.Not(nq)

    def async_pins_static(self, reg):
        """Can this register's async controls be read before its own state is?"""
        spec = reg["spec"]
        for pin in (spec.reset_b, spec.set_b):
            if not pin:
                continue
            net = self.nl.net_of.get((reg["inst"], pin))
            if net is not None and not self.is_static(net):
                return False
        return True

    def next_state(self, reg, cycle, q, qn, pinval):
        """The register's state after this cycle's clock edge."""
        z3 = self.z3
        spec = reg["spec"]
        d = pinval[spec.data]
        if spec.scan and spec.kind == "flop":
            sel, sd = spec.scan
            d = z3.If(pinval[sel], pinval[sd], d)

        if spec.kind == "latch":
            en = pinval[spec.gate]
            if not spec.gate_high:
                en = z3.Not(en)
        elif spec.enable == "DE":
            en = pinval["DE"]
        elif spec.enable == "SCE|DE":
            en = z3.Or(pinval["SCE"], pinval["DE"])
        else:
            en = None

        gates = self.clock_of.get(reg["inst"], (None, []))[1]
        for ginst in gates:
            gen = self.gate_enable[ginst][cycle]
            en = gen if en is None else z3.And(en, gen)

        nq = d if en is None else z3.If(en, d, q)
        nqn = z3.Not(d) if en is None else z3.If(en, z3.Not(d), qn)
        if not spec.split_qn:
            nqn = z3.Not(nq)
        return self.async_override(reg, nq, nqn, pinval)

    def step(self, cycle):
        """Build one cycle: visible state, combinational logic, next state."""
        z3 = self.z3

        # -- phase A: async controls that read only primary inputs, so the value
        # a register shows this cycle already reflects an asserted reset.
        self.vis = {}
        for reg in self.state_regs:
            inst = reg["inst"]
            spec = reg["spec"]
            q = self.state[(inst, "Q")]
            qn = self.state[(inst, "Q_N")] if spec.split_qn else z3.Not(q)
            if (spec.reset_b or spec.set_b) and self.async_pins_static(reg):
                pv = {}
                for pin in (spec.reset_b, spec.set_b):
                    if pin:
                        pv[pin] = self.pin_value(reg, pin, cycle)
                q, qn = self.async_override(reg, q, qn, pv)
            elif spec.reset_b or spec.set_b:
                self.counts["edge_only_async_regs"] += 1
            self.vis[inst] = (q, qn)

        # -- phase B: clock-gate enables, then every register's data pins. The
        # net cache makes this evaluate each gate once however many pins read it.
        self.gate_enable = defaultdict(dict)
        for reg in self.clkgates:
            spec = reg["spec"]
            en = self.pin_value(reg, spec.gate, cycle)
            if spec.enable == "GATE|SCE":
                en = z3.Or(en, self.pin_value(reg, "SCE", cycle))
            self.gate_enable[reg["inst"]][cycle] = en

        next_state = {}
        for reg in self.state_regs:
            spec = reg["spec"]
            pinval = {pin: self.pin_value(reg, pin, cycle)
                      for pin in spec.inputs()}
            q, qn = self.vis[reg["inst"]]
            nq, nqn = self.next_state(reg, cycle, q, qn, pinval)
            next_state[(reg["inst"], "Q")] = nq
            if spec.split_qn:
                next_state[(reg["inst"], "Q_N")] = nqn

        # -- outputs, read at the same point the recording samples them
        self.out_value = {}
        for p in self.out_ports:
            self.out_value[p["name"]] = self.net_value(p["net"], cycle)

        # -- commit: name the next state so the formula stays flat
        for k in self.state_keys:
            self.state[k] = self.define("q.%s.%s@%d" % (k[0], k[1], cycle + 1),
                                        next_state[k])

    # -------------------------------------------------------------------- goal

    def goal_term(self, cycle):
        name = self.opts.goal
        if name not in self.out_value:
            raise ModelError("no output port named %r (outputs: %s)"
                             % (name, ", ".join(sorted(p["name"] for p in self.out_ports))))
        v = self.out_value[name]
        return v if self.opts.goal_value else self.z3.Not(v)

    def extend_reach(self, cycle):
        """reached[t] <-> the goal held at some cycle <= t and >= --not-before."""
        z3 = self.z3
        hit = self.goal_term(cycle)
        if cycle < self.opts.not_before:
            hit = z3.BoolVal(False)
        for name, cyc, val in self.opts.require:
            if cyc != cycle:
                continue
            if name not in self.out_value:
                raise ModelError("--require names unknown output %r" % name)
            v = self.out_value[name]
            self.solver.add(v if val else z3.Not(v))
        self.hit_terms.append(self.define("hit@%d" % cycle, hit))
        prev = self.reach[-1] if self.reach else z3.BoolVal(False)
        r = self.define("reached@%d" % cycle, z3.Or(prev, self.hit_terms[-1]))
        self.reach.append(r)
        return r


# --------------------------------------------------------------------- preflight

def preflight(unroller):
    c = unroller.counts
    return {
        "registers": len(unroller.cones["registers"]),
        "flops": len(unroller.flops),
        "latches": len(unroller.latches),
        "clock_gates": len(unroller.clkgates),
        "state_bits": c["state_bits"],
        "unreset_registers": c["unreset_regs"],
        "gated_flops": c["gated_flops"],
        "half_cycle_flops": c["half_cycle_flops"],
        "never_clocked": c["never_clocked"],
        "clock_domains": dict(sorted(unroller.domains.items())),
        "undriven_data_nets": len(unroller.undriven_nets),
        "input_ports": len(unroller.in_ports),
        "output_ports": sorted(p["name"] for p in unroller.out_ports),
        "combinational_cells": unroller.cones["summary"].get("combinational_cells"),
    }


# ----------------------------------------------------------------------- solving

def vacuity_reason(u, cycle):
    """Why a SAT result is not an answer, or None if it is one.

    A solution has to be *caused* by the bits we are solving for. Two ways it can
    fail to be, both of which look like a clean SAT: nothing was made symbolic at
    all, or the goal was already reached before the first symbolic bit could
    propagate — leaving a free initial state, not the input, doing the work.
    """
    if not u.sym_bits:
        return ("no input was made symbolic, so there is nothing to solve for: "
                "the goal was met by the free inputs and initial state alone. "
                "Name the input to solve for with --symbolic PORT.")
    earliest = min(c for (_p, c) in u.sym_bits)
    if cycle is not None and cycle <= earliest:
        return ("the goal is reached at cycle %d, at or before the first symbolic "
                "input bit (cycle %d), so no chosen bit can have caused it. With "
                "--init free the solver may simply pick a winning power-on state; "
                "drive a reset sequence, pass --init zero, or set --not-before "
                "past the reset." % (cycle, earliest))
    return None


def solve(unroller, opts):
    z3 = unroller.z3
    s = unroller.solver
    t0 = time.time()
    result = {"status": "unsat", "cycle": None, "cycles_unrolled": 0}

    for cycle in range(opts.max_cycles):
        unroller.step(cycle)
        reach = unroller.extend_reach(cycle)
        result["cycles_unrolled"] = cycle + 1
        if cycle + 1 < opts.min_cycles:
            continue
        if (cycle + 1) % opts.check_every and cycle + 1 != opts.max_cycles:
            continue
        r = s.check(reach)
        if opts.verbose:
            print("  cycle %d: %s (%.1fs, %d definitions)"
                  % (cycle, r, time.time() - t0, unroller.counts["definitions"]))
        if r == z3.sat:
            result["status"] = "sat"
            result["model"] = s.model()
            result["reach"] = reach
            # the earliest cycle whose own hit term is true in this model
            m = result["model"]
            for i, h in enumerate(unroller.hit_terms):
                if z3.is_true(m.eval(h, model_completion=True)):
                    result["cycle"] = i
                    break
            break
        if r == z3.unknown:
            result["status"] = "unknown"
            result["reason"] = s.reason_unknown()
            break
    result["seconds"] = round(time.time() - t0, 2)
    return result


def unroll_all(u, opts):
    """Unroll the full bound without solving, and assert the goal. -> reach term."""
    reach = None
    for cycle in range(opts.max_cycles):
        u.step(cycle)
        reach = u.extend_reach(cycle)
    if reach is None:
        raise ModelError("--max-cycles must be at least 1 to build a formula")
    u.solver.add(reach)
    return reach


def dump_dimacs(u, path):
    """Tseitin the unrolled formula to CNF, names included, for a SAT solver."""
    z3 = u.z3
    g = z3.Goal()
    g.add(u.solver.assertions())
    sub = z3.Then("simplify", "bit-blast", "tseitin-cnf")(g)
    if len(sub) != 1:
        raise ModelError("expected one CNF subgoal, got %d" % len(sub))
    with open(path, "w") as f:
        f.write(sub[0].dimacs(include_names=True))
        f.write("\n")
    return len(sub[0])


def answer_bits(unroller, model, port):
    """The solved input, as one character per cycle in cycle order."""
    z3 = unroller.z3
    cycles = sorted(c for (p, c) in unroller.sym_bits if p == port)
    bits = "".join(
        "1" if z3.is_true(model.eval(unroller.sym_bits[(port, c)],
                                     model_completion=True)) else "0"
        for c in cycles)
    return bits, cycles


def input_literals(unroller, model):
    z3 = unroller.z3
    lits = []
    for v in unroller.free_vars:
        if not v.decl().name().startswith("in."):
            continue
        lits.append(v if z3.is_true(model.eval(v, model_completion=True))
                    else z3.Not(v))
    return lits


def check_unique(unroller, model, reach):
    """Is there a second input assignment that also reaches the goal?"""
    z3 = unroller.z3
    s = unroller.solver
    s.push()
    s.add(z3.Not(z3.And(input_literals(unroller, model))))
    r = s.check(reach)
    s.pop()
    if r == z3.unknown:
        return None
    return r == z3.unsat


def check_robust(unroller, model, reach):
    """Does this input reach the goal for *every* initial state?

    The initial state of a register with no reset is a free variable, so a
    solution can silently depend on one particular power-on state. Fixing the
    input and asking whether the goal can fail settles it: UNSAT means it cannot.
    """
    z3 = unroller.z3
    r = unroller.solver.check(input_literals(unroller, model) + [z3.Not(reach)])
    if r == z3.unknown:
        return None
    return r == z3.unsat


# --------------------------------------------------------------------------- CLI

def parse_kv(items, cast=int):
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit("expected NAME=VALUE, got %r" % item)
        k, v = item.split("=", 1)
        out[k.strip()] = cast(v.strip())
    return out


def parse_require(items):
    """PORT@CYCLE=0|1 -> (port, cycle, value)."""
    out = []
    for item in items:
        m = re.match(r"^\s*([^@=]+)@(\d+)\s*=\s*([01])\s*$", item)
        if not m:
            raise SystemExit("expected PORT@CYCLE=0|1, got %r" % item)
        out.append((m.group(1).strip(), int(m.group(2)), int(m.group(3))))
    return out


def load_stimulus(path):
    with open(path) as f:
        stim = json.load(f)
    stim["index"] = {name: i for i, name in enumerate(stim["signals"])}
    return stim


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("graph_json", nargs="?", default="out/NETLIST_GRAPH.json")
    ap.add_argument("cones_json", nargs="?", default="out/CONES.json")
    ap.add_argument("--out", default="out/SOLUTION.json")
    ap.add_argument("--bits-out", default=None,
                    help="write just the solved bit string here")

    g = ap.add_argument_group("stimulus and inputs")
    g.add_argument("--stimulus", default=None,
                   help="bench/stimulus.json: drives every input not made symbolic")
    g.add_argument("--clock", default="clk", help="the clock port, never driven")
    g.add_argument("--symbolic", action="append", default=[], metavar="PORT",
                   help="input port the solver chooses (repeatable)")
    g.add_argument("--symbolic-when", default=None, metavar="SIGNAL",
                   help="only leave --symbolic ports free on cycles where this "
                        "recorded signal is 1")
    g.add_argument("--drive", action="append", default=[], metavar="PORT=0|1",
                   help="hold an input at a constant (repeatable)")
    g.add_argument("--extend", choices=["hold", "zero", "one", "free"],
                   default="hold",
                   help="what drives recorded inputs past the end of the recording")

    g = ap.add_argument_group("goal")
    g.add_argument("--goal", default="success", metavar="PORT",
                   help="output port that must reach --goal-value")
    g.add_argument("--goal-value", type=int, choices=[0, 1], default=1)
    g.add_argument("--not-before", type=int, default=0, metavar="CYCLE",
                   help="ignore the goal before this cycle")
    g.add_argument("--require", action="append", default=[], metavar="PORT@CYCLE=0|1",
                   help="additional output constraint (repeatable)")

    g = ap.add_argument_group("unrolling")
    g.add_argument("--max-cycles", type=int, default=200)
    g.add_argument("--min-cycles", type=int, default=1)
    g.add_argument("--check-every", type=int, default=1, metavar="N",
                   help="only call the solver every N cycles while deepening")
    g.add_argument("--init", choices=["free", "zero"], default="free",
                   help="initial state of every register (default: free, which "
                        "--check-robust then tests the answer against)")
    g.add_argument("--timeout", type=float, default=None, metavar="SEC",
                   help="per-check solver timeout")

    g = ap.add_argument_group("modelling escapes")
    g.add_argument("--latch-as-flop", action="store_true",
                   help="treat transparent latches as edge-triggered (approximate)")
    g.add_argument("--allow-half-cycle", action="store_true",
                   help="model opposite-edge registers on the rising edge")
    g.add_argument("--tie", action="append", default=[], metavar="PIN=0|1",
                   help="value for an unconnected sequential pin, as PIN=0 or "
                        "INST.PIN=0 (repeatable)")
    g.add_argument("--free-unconnected", action="store_true",
                   help="let the solver choose unconnected sequential pins")
    g.add_argument("--tie-undriven", choices=["free", "0", "1"], default="free",
                   help="value for nets that are read but driven by nothing "
                        "(default: free, i.e. the solver chooses them every cycle)")

    g = ap.add_argument_group("checks and dumps")
    g.add_argument("--check-unique", action="store_true",
                   help="after a solution, look for a second one")
    g.add_argument("--check-robust", action="store_true",
                   help="prove the solution works for every initial state")
    g.add_argument("--preflight", action="store_true",
                   help="report the structure and stop, without solving")
    g.add_argument("--smt2", default=None, metavar="FILE",
                   help="dump the unrolled formula as runnable SMT-LIB2 instead "
                        "of solving it")
    g.add_argument("--dimacs", default=None, metavar="FILE",
                   help="dump the unrolled formula as CNF for an external SAT "
                        "solver; answer bits are the vars named in.<port>@<cycle>")
    g.add_argument("--verbose", action="store_true")
    return ap


def parse_options(argv=None):
    """Command line -> the options object the Unroller and solve() read.

    Split out from main() so the fixture suite drives exactly the same defaults
    the command line does, rather than a second copy of them.
    """
    opts = build_parser().parse_args(argv)
    opts.drive = parse_kv(opts.drive)
    opts.tie = parse_kv(opts.tie)
    opts.require = parse_require(opts.require)
    opts.symbolic = set(opts.symbolic)
    opts.stimulus = load_stimulus(opts.stimulus) if opts.stimulus else None
    if opts.check_every < 1:
        raise SystemExit("--check-every must be at least 1")
    return opts


def main(argv=None):
    opts = parse_options(argv)

    try:
        import z3                                             # noqa: F401
    except ImportError:
        print("symSolve needs the z3 solver:  pip3 install z3-solver", file=sys.stderr)
        return 2

    with open(opts.graph_json) as f:
        graph = json.load(f)
    with open(opts.cones_json) as f:
        cones = json.load(f)

    warnings = []
    try:
        u = Unroller(graph, cones, opts, warnings)
    except ModelError as e:
        print("symSolve: %s" % e, file=sys.stderr)
        return 1

    pre = preflight(u)
    print("%s: %d registers (%d flops, %d latches, %d clock gates), %d state bits"
          % (opts.graph_json, pre["registers"], pre["flops"], pre["latches"],
             pre["clock_gates"], pre["state_bits"]))
    print("  clock domains: %s" % (pre["clock_domains"] or "none"))
    print("  %d registers have no async reset or set (their initial state is %s)"
          % (pre["unreset_registers"], opts.init))
    if pre["undriven_data_nets"]:
        print("  %d net(s) read as data are driven by nothing (--tie-undriven "
              "pins them; they are free every cycle otherwise)"
              % pre["undriven_data_nets"])
    print("  outputs: %s" % ", ".join(pre["output_ports"]))

    result = {"preflight": pre, "warnings": warnings,
              "goal": {"port": opts.goal, "value": opts.goal_value, "cycle": None}}

    if opts.preflight:
        write_result(opts, result)
        for w in warnings[:10]:
            print("warning:", w)
        return 0

    # Solving for nothing succeeds trivially and answers nothing, so refuse it
    # rather than reporting a SAT with an empty answer.
    inputs = sorted(p["name"] for p in u.in_ports)
    if not opts.symbolic:
        print("symSolve: name the input to solve for with --symbolic PORT "
              "(input ports: %s). Without one, every input is merely free and "
              "any SAT result is vacuous." % ", ".join(inputs), file=sys.stderr)
        return 1
    unknown = sorted(opts.symbolic - set(inputs))
    if unknown:
        print("symSolve: --symbolic names %s, which %s an input port of this "
              "design (input ports: %s)"
              % (", ".join(unknown), "are not" if len(unknown) > 1 else "is not",
                 ", ".join(inputs)), file=sys.stderr)
        return 1
    if opts.symbolic_when and opts.stimulus is None:
        print("symSolve: --symbolic-when needs a --stimulus to read %r from"
              % opts.symbolic_when, file=sys.stderr)
        return 1

    try:
        if opts.smt2 or opts.dimacs:
            unroll_all(u, opts)
            print("  %d cycles unrolled, %d definitions"
                  % (opts.max_cycles, u.counts["definitions"]))
            if opts.smt2:
                with open(opts.smt2, "w") as f:
                    f.write(u.solver.sexpr())
                    f.write("(check-sat)\n")
                print("  -> %s" % opts.smt2)
            if opts.dimacs:
                clauses = dump_dimacs(u, opts.dimacs)
                print("  -> %s (%d clauses)" % (opts.dimacs, clauses))
            result["status"] = "dumped"
            result["cycles_unrolled"] = opts.max_cycles
            write_result(opts, result)
            return 0

        run = solve(u, opts)
    except ModelError as e:
        print("symSolve: %s" % e, file=sys.stderr)
        return 1

    result["status"] = run["status"]
    result["cycles_unrolled"] = run["cycles_unrolled"]
    result["stats"] = {"seconds": run["seconds"],
                       "definitions": u.counts["definitions"],
                       "free_bits": len(u.free_vars),
                       "symbolic_bits": len(u.sym_bits)}
    for k in ("cut_loop_nets", "opaque_leaf_bits", "undriven_leaf_bits",
              "unconnected_gate_pins", "free_unconnected_pins", "deasserted_pins",
              "edge_only_async_regs", "unstimulated_input_bits"):
        if u.counts[k]:
            result["stats"][k] = u.counts[k]

    print()
    if run["status"] == "unknown":
        print("UNKNOWN after %d cycles (%s), %.1fs"
              % (run["cycles_unrolled"], run.get("reason", ""), run["seconds"]))
    elif run["status"] == "unsat":
        print("UNSAT: %s cannot reach %d within %d cycles (%.1fs)"
              % (opts.goal, opts.goal_value, run["cycles_unrolled"], run["seconds"]))
    else:
        result["goal"]["cycle"] = run["cycle"]
        vacuous = vacuity_reason(u, run["cycle"])
        if vacuous:
            result["status"] = "vacuous"
            result["vacuous"] = vacuous
            print("VACUOUS: %s reaches %d at cycle %s, but %s"
                  % (opts.goal, opts.goal_value, run["cycle"], vacuous))
            write_result(opts, result)
            return 1
        print("SAT: %s reaches %d at cycle %s (%.1fs)"
              % (opts.goal, opts.goal_value, run["cycle"], run["seconds"]))
        model = run["model"]
        ports = sorted({p for (p, _c) in u.sym_bits})
        answers = {}
        for port in ports:
            bits, cycles = answer_bits(u, model, port)
            answers[port] = {"bits": bits, "n_bits": len(bits),
                             "cycles": [cycles[0], cycles[-1]] if cycles else []}
            if cycles:
                print("  %s: %d bits over cycles %d..%d"
                      % (port, len(bits), cycles[0], cycles[-1]))
            else:
                print("  %s: no symbolic cycles" % port)
        result["answer"] = answers
        if opts.bits_out and len(ports) == 1:
            with open(opts.bits_out, "w") as f:
                f.write(answers[ports[0]]["bits"] + "\n")
            print("  -> %s" % opts.bits_out)

        if opts.check_unique:
            uniq = check_unique(u, model, run["reach"])
            result["unique"] = uniq
            print("  unique: %s" % {True: "yes — no other input reaches the goal",
                                    False: "NO — another input also reaches it",
                                    None: "unknown (solver gave up)"}[uniq])
        if opts.check_robust:
            rob = check_robust(u, model, run["reach"])
            result["robust"] = rob
            print("  robust: %s"
                  % {True: "yes — reaches the goal from every initial state",
                     False: "NO — depends on the power-on state of some register",
                     None: "unknown (solver gave up)"}[rob])

    write_result(opts, result)
    for w in warnings[:10]:
        print("warning:", w)
    if len(warnings) > 10:
        print("... %d more warnings (see JSON)" % (len(warnings) - 10))
    return 0


def write_result(opts, result):
    with open(opts.out, "w") as f:
        json.dump(result, f, indent=1)
        f.write("\n")
    print("  -> %s" % opts.out)


if __name__ == "__main__":
    sys.exit(main())
