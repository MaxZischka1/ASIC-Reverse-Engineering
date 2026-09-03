#!/usr/bin/env python3
"""decomposeCone.py — what drives one cone's output to a chosen value.

An analysis tool over stage 4's cones, not a pipeline stage: nothing in the
pipeline depends on it and it writes no pipeline output. Given a cone and a
target value, it asks Z3 which assignments to the cone's *boundary inputs*
force the cone's root net to that value, and reports the answer four ways:

  implicants  an irredundant cover of prime implicants of the on-set (or the
              off-set, for target 0) -- cubes over the boundary inputs, with
              don't-cares shown as '-'. The compact answer, and the default.
  models      every satisfying assignment, one row each. Rows are over the
              functional support: an input nothing depends on shows as '-'
              rather than doubling the row count for no information.
  support     which boundary inputs can actually affect the output. An input
              the wiring provides but the function ignores is reported as
              irrelevant, proven by UNSAT rather than assumed.
  count       how many assignments work, by summing disjoint cubes rather than
              one solve per solution, with a hard stop at --limit.

Nothing here enumerates a truth table. Each mode is a loop of SAT queries whose
cost tracks the answer's structure rather than 2^k: a point is widened into a
cube by asking whether dropping a literal escapes the target set, and the unsat
core of that question drops most of the literals in one go.

A search that runs out of --limit, --budget or --timeout keeps what it has
already proved, says which one stopped it, and marks the answer partial. Nothing
silently becomes UNSAT because the clock ran out.

The circuit semantics are not restated here. Combinational cells are evaluated
through the same ``genCellModels.FAMILY_FUNCS`` expressions that generate the
simulation models, parsed by the same ``boolExpr`` parser stage 5 uses and
turned into Z3 terms by the same ``symSolve.z3_from_ast`` stage 6 uses. There is
deliberately no second cell table in this file: a table that disagreed with
FAMILY_FUNCS about a polarity would make this tool and the rest of the pipeline
silently contradict each other, which is the failure this tool exists to catch.
``genCellModels.py`` validates that table against every macro in the merged LEF
at generation time, so a cell missing a model here is a cell missing a model
everywhere.

A cell in the cone with no entry in FAMILY_FUNCS is a hard error naming the
cell. So is a tri-state cell (whose function is not Boolean), and so is a
sequential cell found inside a cone. Nothing is ever guessed at, defaulted to a
generic gate, or skipped: a silently wrong model is worse than a crash.

Cone boundaries
---------------
Stage 4 already cuts cones at the nearest sequential boundary, so the register
assumption this tool needs is structural, not asserted: a cone's root is a
register data pin or a primary output, and its leaves are register outputs,
primary inputs, constants, blackbox pins, or undriven nets. Reasoning never
crosses a register. If a cone ever violates that -- a sequential cell inside the
cone body -- it is rejected by name rather than modelled.

Every boundary input is reported with where it comes from, so one report says
what to read next: a register output names the cone feeding that register's data
pin (`CONE45`), and a primary input, a black box, a constant or an undriven net
says what it is, because nothing upstream of those is a cone. The tag is stage
4's `from_cone`, recomputed here with stage 4's own function when CONES.json
predates it — so an older file still gets origins, and the two can never
disagree. Where no single cone feeds a register's data pin, the report says so
rather than guessing.

Boundary inputs are keyed by *net*, not by the leaf descriptors stage 4 records,
because ``coneDecompose.build_cone`` collapses every unconnected pin into a
single "undriven" leaf; two independent dangling pins are two independent free
bits here, keyed by (instance, pin). A leaf net carrying a constant folds to that
constant and is not an input.

Usage
-----
    python3 src/decomposeCone.py CONE7 1
    python3 src/decomposeCone.py 7 0 --mode implicants --limit 50
    python3 src/decomposeCone.py CONE7 1 --mode support --json
    python3 src/decomposeCone.py --by-root 412 1 --mode count
    python3 src/decomposeCone.py CONE7 1 --also CONE9=1     # both at once

A block with two outputs is two cones. ``--also`` puts them in one query over a
shared boundary, which is not the same as running the tool twice: two separately
reachable targets can be jointly unreachable, and two separate runs can pick
assignments that contradict each other. Cones that share logic share its
variables, so the shared part is encoded once.

``CONE7`` is the bench Verilog module name for cone id 7 (bench/conesToVerilog.py
names modules ``<prefix><id + start>``, defaulting to ``CONE`` and 0). The
pipeline's own identifier is the plain integer id; ``CONE7``, ``cone7`` and ``7``
all mean the same cone, and ``--by-root`` addresses one by root net instead.

Exit codes: 0 success, 1 usage/model error, 2 the target value is unreachable
(the cone is constant), 3 the solver returned unknown (see --timeout).

``--budget SEC`` caps the whole run rather than one solve; ``--seed`` fixes Z3's
random seed so the same question gives the same answer and the output can be
diffed. Every report ends with the solver calls and time the answer cost.
"""

import argparse
import json
import os
import sys
import time

import boolExpr
from coneDecompose import Netlist, annotate_leaf_origins
from genCellModels import FAMILY_FUNCS, SEQ_TEMPLATES, family_of
from symSolve import z3_from_ast

sys.setrecursionlimit(200000)

DEFAULT_GRAPH = "out/NETLIST_GRAPH.json"
DEFAULT_CONES = "out/CONES.json"

EXIT_ERROR = 1
EXIT_UNSAT = 2
EXIT_UNKNOWN = 3


class ConeError(Exception):
    """The cone uses something this tool will not guess at."""


# ------------------------------------------------------------------ cell models

def cell_model(cell_name):
    """{output pin: parsed AST} for one combinational cell type.

    The single source of truth is genCellModels.FAMILY_FUNCS; this only
    dispatches on it, so that a missing family and a non-Boolean (tri-state)
    cell can be told apart in the error message. symSolve.cell_expressions
    returns None for both, which is right for an unroller and too coarse here.
    """
    fam = family_of(cell_name)
    if fam in SEQ_TEMPLATES:
        raise ConeError(
            "%s is a sequential cell (family %r). A cone must not contain one: "
            "stage 4 cuts cones at the register boundary, so this netlist or "
            "CONES.json is inconsistent." % (cell_name, fam))
    funcs = FAMILY_FUNCS.get(fam)
    if funcs is None:
        raise ConeError(
            "cell %s (family %r) has no entry in genCellModels.FAMILY_FUNCS. "
            "Add its function there -- where stages 4, 5 and 6 will pick it up "
            "too -- rather than guessing one here." % (cell_name, fam))
    out = {}
    for pin, text in funcs.items():
        try:
            out[pin] = boolExpr.parse(text)
        except boolExpr.ParseError as e:
            raise ConeError(
                "cell %s (family %r) is not a Boolean function: pin %s is %r "
                "(%s). Tri-state cells cannot be modelled as a Boolean formula."
                % (cell_name, fam, pin, text, e))
    return out


# ------------------------------------------------------------------ the formula

def leaf_label(leaf):
    """A short, stable, greppable name for a boundary input."""
    kind = leaf["kind"]
    if kind == "port":
        return leaf["name"]
    if kind in ("reg", "opaque"):
        return "%s.%s" % (leaf["inst"], leaf["pin"])
    if kind == "const":
        return "const%s" % leaf["value"]
    return "undriven.n%s" % leaf.get("net", "?")


class ConeFormula(object):
    """The Boolean function of one cone, as a Z3 encoding over its boundary.

    The encoding is definitional -- one Bool per net, one equality per gate
    output -- so reconvergent fan-out costs a constraint rather than a duplicated
    subterm. ``emit`` can be called more than once with different suffixes to get
    independent copies sharing nothing but the structure, which is what the
    support check needs.
    """

    def __init__(self, nl, cone):
        self.nl = nl
        self.cone = cone
        self.root_net = cone["root_net"]
        self.inputs = []        # [(key, label, descriptor)] in stable order
        self._index = {}        # key -> position in self.inputs
        self.const_nets = {}    # net -> "0" | "1"
        self._collect()

    # -- boundary -----------------------------------------------------------

    def _add_input(self, key, leaf):
        if key in self._index:
            return
        self._index[key] = len(self.inputs)
        self.inputs.append((key, leaf_label(leaf), leaf))

    def _collect(self):
        """Walk the cone's cells in stage 4's topological order and record every
        signal that enters it from outside, in first-use order."""
        nl = self.nl
        inside = set(self.cone["cells"])

        for inst in self.cone["cells"]:
            cell = nl.inst_cell[inst]
            cell_model(cell)                      # reject unmodelled cells early
            for pin, src in nl.input_nets(inst):
                if isinstance(src, tuple):        # tied off in the layout
                    continue
                if src is None:                   # unconnected pin: its own bit
                    self._add_input(("nc", inst, pin),
                                    {"kind": "undriven", "net": "%s.%s"
                                     % (inst, pin)})
                    continue
                drv = nl.driver.get(src)
                if drv is not None and drv[1] in inside:
                    continue                      # internal signal
                self._boundary_net(src)

        if not self.cone["cells"]:
            # A cone with no logic is the boundary signal itself.
            self._boundary_net(self.root_net)

    def _boundary_net(self, net):
        if net in self.nl.const_of_net:
            self.const_nets[net] = self.nl.const_of_net[net]
            return
        kind, payload = self.nl.source_of(net)
        if kind != "leaf":
            # Driven by a combinational cell the cone does not list: stage 4
            # builds cones by fan-in closure, so this cannot happen for a cone
            # it produced. Say so rather than silently making it a free bit.
            raise ConeError(
                "net %d enters cone %d from cell %s, which the cone does not "
                "list. CONES.json does not match this netlist."
                % (net, self.cone["id"], payload))
        if payload["kind"] == "const":
            self.const_nets[net] = payload["value"]
            return
        self._add_input(("net", net), payload)

    def input_labels(self):
        return [label for _k, label, _d in self.inputs]

    def n_inputs(self):
        return len(self.inputs)

    # -- encoding -----------------------------------------------------------

    def emit(self, z3, solver, suffix="", overrides=None):
        """Constrain `solver` with this cone's logic.

        -> (root term, [one Bool per boundary input, in self.inputs order])

        `overrides` maps a boundary input key to a term to use instead of a
        fresh variable, which is how an input is pinned to a constant.
        """
        overrides = overrides or {}
        invars = []
        env_of_key = {}
        for i, (key, label, _leaf) in enumerate(self.inputs):
            if key in overrides:
                term = overrides[key]
            else:
                # index-qualified: two boundary signals can never share a
                # variable just because their labels happen to collide.
                term = z3.Bool("in%d.%s%s" % (i, label, suffix))
            env_of_key[key] = term
            invars.append(term)

        value = {}                                   # net -> term
        for net, bit in self.const_nets.items():
            value[net] = z3.BoolVal(bit == "1")
        for key, term in env_of_key.items():
            if key[0] == "net":
                value[key[1]] = term

        nl = self.nl
        for inst in self.cone["cells"]:
            cell = nl.inst_cell[inst]
            env = {}
            for pin, src in nl.input_nets(inst):
                if isinstance(src, tuple):
                    env[pin] = z3.BoolVal(src[1] == "1")
                elif src is None:
                    env[pin] = env_of_key[("nc", inst, pin)]
                else:
                    if src not in value:
                        raise ConeError(
                            "cone %d: %s.%s reads net %d before anything drives "
                            "it. The cone's cell order is not topological."
                            % (self.cone["id"], inst, pin, src))
                    env[pin] = value[src]
            for pin, ast in cell_model(cell).items():
                out_net = nl.net_of.get((inst, pin))
                if out_net is None:
                    continue                          # unused output of the cell
                v = z3.Bool("g.%s.%s%s" % (inst, pin, suffix))
                solver.add(v == z3_from_ast(z3, ast, env))
                value[out_net] = v

        if self.root_net not in value:
            raise ConeError("cone %d: nothing produces its root net %d"
                            % (self.cone["id"], self.root_net))
        return value[self.root_net], invars


class JointFormula(object):
    """Several cones asked to hit their targets *at the same time*.

    Two outputs of one block are two cones, and solving them one at a time
    answers a different question: each run may pick a different assignment, so
    two separately satisfiable targets can still be jointly impossible. This
    shares one boundary variable per signal across every cone, so the answer is
    an assignment that works for all of them at once.

    Cones that share logic also share its gate variables: the definitions are
    emitted under the same names from the same shared inputs, so a shared
    subexpression is constrained once and reused rather than duplicated.

    ``emit`` returns a single Bool that is true exactly when every cone is at
    its target, so the modes need no notion of "several goals" -- to them a
    joint query is one function of the merged boundary, like any other.
    """

    def __init__(self, nl, goals):
        self.nl = nl
        self.goals = list(goals)                  # [(cone, target value)]
        self.parts = [(ConeFormula(nl, cone), value) for cone, value in goals]
        self.inputs = []
        self._index = {}
        self.const_nets = {}
        for formula, _value in self.parts:
            for key, label, leaf in formula.inputs:
                if key not in self._index:
                    self._index[key] = len(self.inputs)
                    self.inputs.append((key, label, leaf))
            self.const_nets.update(formula.const_nets)

    def input_labels(self):
        return [label for _k, label, _d in self.inputs]

    def n_inputs(self):
        return len(self.inputs)

    def emit(self, z3, solver, suffix="", overrides=None):
        shared = dict(overrides or {})
        invars = []
        for i, (key, label, _leaf) in enumerate(self.inputs):
            if key not in shared:
                shared[key] = z3.Bool("in%d.%s%s" % (i, label, suffix))
            invars.append(shared[key])

        met = []
        for formula, value in self.parts:
            root, _iv = formula.emit(z3, solver, suffix=suffix, overrides=shared)
            met.append(root if value else z3.Not(root))
        goal = z3.Bool("goal%s" % suffix)
        solver.add(goal == z3.And(met))
        return goal, invars


# ----------------------------------------------------------------------- modes
# Every mode reports "unknown" as unknown. A timeout is not a proof of anything,
# and folding it into UNSAT would turn "I ran out of time" into "impossible".
# A run that hits a timeout, a budget or a limit keeps the answers it already
# has and says why it stopped, rather than throwing the work away.

STOP_COMPLETE = ""          # the search finished; the answer is the whole answer
STOP_LIMIT = "limit"        # --limit reached, and there is genuinely more
STOP_BUDGET = "budget"      # --budget spent
STOP_UNKNOWN = "unknown"    # a solve timed out (--timeout)


class Unknown(Exception):
    """Z3 gave up on one solve (usually --timeout)."""


class Budget(object):
    """A wall-clock cap for the whole run, and the solver-call accounting.

    One Budget threads through every solve of a run, so `--budget` bounds the
    search as a whole rather than each query, and the stats it collects say what
    the answer actually cost.
    """

    def __init__(self, seconds=None):
        self.seconds = seconds
        self.started = time.time()
        self.deadline = None if not seconds else self.started + seconds
        self.calls = 0
        self.solve_seconds = 0.0

    def expired(self):
        return self.deadline is not None and time.time() >= self.deadline

    def per_solve(self, timeout):
        """The timeout for the next solve: never longer than what is left."""
        if self.deadline is None:
            return timeout
        left = max(0.0, self.deadline - time.time())
        return left if not timeout else min(timeout, left)

    def stats(self):
        return {"solver_calls": self.calls,
                "solve_seconds": round(self.solve_seconds, 3),
                "wall_seconds": round(time.time() - self.started, 3)}


def _new_solver(z3, opts):
    s = z3.Solver()
    # Fixed seeds: the same question asked twice should give the same answer,
    # because this output is meant to be diffed and piped onward.
    s.set("random_seed", opts.seed)
    if opts.timeout:
        s.set("timeout", int(opts.timeout * 1000))
    return s


def _check(z3, solver, budget, assumptions=(), opts=None):
    """One solve, charged to the budget. -> True for SAT, False for UNSAT."""
    if budget.expired():
        raise OutOfBudget()
    if opts is not None and budget.deadline is not None:
        solver.set("timeout", max(1, int(budget.per_solve(opts.timeout) * 1000)))
    t = time.time()
    r = solver.check(*assumptions)
    budget.calls += 1
    budget.solve_seconds += time.time() - t
    if r == z3.unknown:
        # The per-solve timeout is clamped to whatever is left of the budget, so
        # the last solve of a budgeted run gives up for the budget's reason, not
        # the user's --timeout. Report the one that actually bit.
        if budget.expired():
            raise OutOfBudget()
        raise Unknown(solver.reason_unknown())
    return r == z3.sat


class OutOfBudget(Exception):
    """The whole-run --budget is spent."""


def _assignment(model, invars, z3):
    """The model's value for each boundary input, as 0/1."""
    return [1 if z3.is_true(model.eval(v, model_completion=True)) else 0
            for v in invars]


def _cube_str(cube):
    return "".join("-" if b is None else str(b) for b in cube)


def _block(z3, solver, invars, cube):
    """Forbid every point of `cube` from here on."""
    lits = [(z3.Not(v) if b else v)
            for v, b in zip(invars, cube) if b is not None]
    solver.add(z3.Or(lits) if lits else z3.BoolVal(False))


# ------------------------------------------------------------ functional support

def support_vector(z3, formula, opts, budget):
    """[bool] -- can each boundary input change the output?

    Two copies of the function share nothing, their outputs are forced to
    differ, and every input is tied across the copies by a named equality.
    Releasing one tie and asking for a difference is exactly "can this input
    alone change the output?"; UNSAT proves it cannot, for any value of the
    rest. One solver, one assumption-set per input.
    """
    solver = _new_solver(z3, opts)
    root_a, vars_a = formula.emit(z3, solver, suffix="@a")
    root_b, vars_b = formula.emit(z3, solver, suffix="@b")
    solver.add(root_a != root_b)

    eq = []
    for i, (a, b) in enumerate(zip(vars_a, vars_b)):
        e = z3.Bool("eq.%d" % i)
        solver.add(e == (a == b))
        eq.append(e)

    out = []
    for i in range(len(eq)):
        out.append(_check(z3, solver, budget,
                          [eq[j] for j in range(len(eq)) if j != i], opts))
    return out


def run_support(z3, formula, opts, budget=None):
    budget = budget or Budget(getattr(opts, "budget", None))
    # No partial answer here: a support list missing an entry reads as "this
    # input is irrelevant", which is the opposite of "I did not get to it".
    vec = support_vector(z3, formula, opts, budget)
    stopped = STOP_COMPLETE
    rows = [{"input": label, "relevant": vec[i]}
            for i, (_k, label, _l) in enumerate(formula.inputs)]
    return {"rows": rows, "n_relevant": sum(1 for r in rows if r["relevant"]),
            "support": vec, "stopped": stopped, "stats": budget.stats()}


# ------------------------------------------------------------------- enumeration

def _enumerator(z3, formula, target, opts, budget):
    """A solver holding "the goal is met", plus the boundary variables."""
    solver = _new_solver(z3, opts)
    root, invars = formula.emit(z3, solver)
    solver.add(root if target else z3.Not(root))
    return solver, invars


def _projection(z3, formula, target, opts, budget):
    """(support flags, the indices worth enumerating over, the multiplier).

    An input outside the functional support cannot change the answer, so every
    solution is repeated once per free choice of it. Enumerating over the
    support alone and multiplying is exact and exponentially cheaper -- and it
    stops the output being 2^m copies of the same real answer.
    """
    vec = support_vector(z3, formula, opts, budget)
    keep = [i for i, rel in enumerate(vec) if rel]
    return vec, keep, 1 << (len(vec) - len(keep))


def run_models(z3, formula, target, opts, budget=None):
    """Satisfying assignments, one row each.

    Rows are over the functional support; an input nothing depends on is shown
    as '-' rather than doubling the row count for no information. The reported
    count is still the exact number of full assignments.
    """
    budget = budget or Budget(getattr(opts, "budget", None))
    rows, stopped, total, mult = [], STOP_COMPLETE, 0, 1
    try:
        _vec, keep, mult = _projection(z3, formula, target, opts, budget)
        solver, invars = _enumerator(z3, formula, target, opts, budget)
        proj = [invars[i] for i in keep]
        kept = set(keep)
        while True:
            if not _check(z3, solver, budget, (), opts):
                break
            bits = _assignment(solver.model(), invars, z3)
            row = [bits[i] if i in kept else None
                   for i in range(len(invars))]
            rows.append(row)
            total += mult
            _block(z3, solver, proj, [bits[i] for i in keep])
            if len(rows) >= opts.limit:
                stopped = (STOP_LIMIT if _check(z3, solver, budget, (), opts)
                           else STOP_COMPLETE)
                break
    except Unknown:
        stopped = STOP_UNKNOWN
    except OutOfBudget:
        stopped = STOP_BUDGET
    return {"rows": rows, "truncated": stopped != STOP_COMPLETE,
            "stopped": stopped, "count": total,
            "exact": stopped == STOP_COMPLETE,
            "multiplier": mult,
            "stats": budget.stats()}


def _cube_bits(cube):
    """A cube as (fixed positions, values), packed into two integers.

    Disjointness is then one integer expression instead of a walk over both
    cubes, which matters because the count below tests every candidate against
    every cube it has already emitted.
    """
    mask = val = 0
    for i, b in enumerate(cube):
        if b is None:
            continue
        mask |= 1 << i
        if b:
            val |= 1 << i
    return mask, val


def _disjoint_bits(mask, val, others):
    """Does this cube share no point with any of `others`?

    Two cubes are disjoint exactly when some position both of them fix is fixed
    to opposite values -- `mask & m` are the shared fixed positions and
    `val ^ v` are the disagreements, so a non-zero intersection proves it.
    """
    for m, v in others:
        if not (mask & m & (val ^ v)):
            return False
    return True


def run_count(z3, formula, target, opts, budget=None):
    """How many assignments meet the goal.

    Not one solver call per solution. Each model found is widened into a cube
    that still implies the goal *and* stays disjoint from every cube already
    emitted -- disjointness is a syntactic test on two cubes, so most widening
    steps cost nothing. Disjoint cubes can simply be added up: a cube with f
    free literals is 2^f assignments, and no assignment is counted twice. The
    cube is then blocked, so the search moves on to what is left.

    Counting runs over the functional support only and multiplies by the free
    choices outside it, so inputs nothing depends on cost nothing to count.

    --limit caps the running total rather than the cubes: past it the answer is
    reported as a lower bound instead of the search hanging.
    """
    budget = budget or Budget(getattr(opts, "budget", None))
    n, mult, stopped, cubes = 0, 1, STOP_COMPLETE, []
    try:
        _vec, keep, mult = _projection(z3, formula, target, opts, budget)
        enum, invars = _enumerator(z3, formula, target, opts, budget)
        neg = _new_solver(z3, opts)
        nroot, nvars = formula.emit(z3, neg, suffix="~")
        neg.add(z3.Not(nroot) if target else nroot)
        ev = [invars[i] for i in keep]
        kv = [nvars[i] for i in keep]
        # Built once. z3.Not() allocates an AST node, and the widening loop
        # below asks for these literals thousands of times.
        lit = [(z3.Not(v), v) for v in kv]

        while True:
            if n >= opts.limit:
                stopped = STOP_LIMIT
                break
            if not _check(z3, enum, budget, (), opts):
                break
            full = _assignment(enum.model(), invars, z3)
            cube = [full[i] for i in keep]
            mask, val = _cube_bits(cube)
            for i in range(len(cube)):
                bit = 1 << i
                held = cube[i]
                cube[i] = None
                trial_mask = mask & ~bit
                widened = _disjoint_bits(trial_mask, val & ~bit, cubes)
                if widened:
                    assume = [lit[j][b] for j, b in enumerate(cube)
                              if b is not None]
                    widened = not _check(z3, neg, budget, assume, opts)
                if widened:
                    mask, val = trial_mask, val & ~bit
                else:
                    cube[i] = held
            cubes.append((mask, val))
            n += 1 << (len(cube) - bin(mask).count("1"))
            _block(z3, enum, ev, cube)
    except Unknown:
        stopped = STOP_UNKNOWN
    except OutOfBudget:
        stopped = STOP_BUDGET
    return {"count": n * mult, "support_models": n, "multiplier": mult,
            "cubes": len(cubes), "exact": stopped == STOP_COMPLETE,
            "stopped": stopped, "limit_hit": opts.limit,
            "stats": budget.stats()}


# -------------------------------------------------------------- prime implicants

def _reduce_to_prime(z3, neg, nvars, cube, opts, budget):
    """Widen one point of the target set into a prime implicant.

    Two stages. First one query: a full point of the set implies the function,
    so `neg` (which holds its complement) answers UNSAT, and the unsat core
    names the literals that were actually needed -- every literal outside the
    core is dropped at once, which is most of them on a wide cone. Then a
    greedy pass over what is left, keeping a literal only when dropping it
    makes the negation satisfiable, which is what makes the result prime.
    """
    lit_of = {}
    for i, b in enumerate(cube):
        if b is not None:
            lit_of[i] = nvars[i] if b else z3.Not(nvars[i])

    assumptions = [lit_of[i] for i in sorted(lit_of)]
    if not _check(z3, neg, budget, assumptions, opts):
        core = {str(c) for c in neg.unsat_core()}
        if core:                       # a core of nothing means the cube is all of it
            for i in list(lit_of):
                if str(lit_of[i]) not in core:
                    cube[i] = None
                    del lit_of[i]

    for i in sorted(lit_of):
        keep = cube[i]
        cube[i] = None
        rest = [lit_of[j] for j in sorted(lit_of) if j != i]
        if _check(z3, neg, budget, rest, opts):
            cube[i] = keep            # dropping it escaped the set: it is needed
        else:
            del lit_of[i]
    return cube


def _drop_redundant_cubes(z3, rows, opts, budget, cap=4000):
    """Remove any cube whose points the others already cover.

    Blocking guarantees each cube contributes a point no *earlier* cube had; it
    does not stop an early cube being swallowed by later ones. This pass makes
    the cover irredundant in the real sense. It is pure Boolean algebra over the
    input variables -- the cone's logic is not involved.

    One solver, built once: each cube j gets a selector with ``sel_j -> ~cube_j``,
    so asking whether cube i has a point of its own is a single check under the
    assumptions {sel_j : j kept, j != i} plus cube i's own literals. That is
    linear in the number of cubes; asserting the other cubes afresh for each
    one would be quadratic, which on a wide cone costs more than the search it
    is tidying up.

    -> (kept rows, dropped count, whether the pass actually ran)
    """
    if len(rows) < 2:
        return rows, 0, True
    if len(rows) > cap:
        # Polish, not correctness: past this size it is not worth the wait.
        return rows, 0, False

    k = len(rows[0])
    v = [z3.Bool("cov%d" % i) for i in range(k)]

    def lits(cube):
        return [(v[i] if b else z3.Not(v[i]))
                for i, b in enumerate(cube) if b is not None]

    solver = _new_solver(z3, opts)
    sel = []
    for j, cube in enumerate(rows):
        s_j = z3.Bool("sel%d" % j)
        body = lits(cube)
        solver.add(z3.Implies(s_j, z3.Not(z3.And(body)) if body
                              else z3.BoolVal(False)))
        sel.append(s_j)

    keep = set(range(len(rows)))
    dropped = 0
    for i in range(len(rows)):
        if budget.expired():
            return [rows[j] for j in sorted(keep)], dropped, False
        others = [sel[j] for j in keep if j != i]
        if not others:
            break
        if not _check(z3, solver, budget, others + lits(rows[i]), opts):
            keep.discard(i)           # nothing of its own left
            dropped += 1
    return [rows[j] for j in sorted(keep)], dropped, True


def run_implicants(z3, formula, target, opts, budget=None):
    """An irredundant cover of prime implicants of the target set.

    Two solvers: `enum` yields a point of the set not yet covered, and `neg`
    holds the complement and answers "does this cube still imply the goal?".
    Each row is a genuine prime implicant and the rows together cover the set;
    this is not the complete list of all prime implicants, which is usually far
    longer and no more useful.
    """
    budget = budget or Budget(getattr(opts, "budget", None))
    rows, stopped, dropped, tidied = [], STOP_COMPLETE, 0, True
    try:
        enum, invars = _enumerator(z3, formula, target, opts, budget)
        neg = _new_solver(z3, opts)
        nroot, nvars = formula.emit(z3, neg, suffix="~")
        neg.add(z3.Not(nroot) if target else nroot)

        while True:
            if not _check(z3, enum, budget, (), opts):
                break
            cube = _reduce_to_prime(z3, neg, nvars,
                                    _assignment(enum.model(), invars, z3),
                                    opts, budget)
            rows.append(list(cube))
            _block(z3, enum, invars, cube)
            if len(rows) >= opts.limit:
                stopped = (STOP_LIMIT if _check(z3, enum, budget, (), opts)
                           else STOP_COMPLETE)
                break
        if stopped == STOP_COMPLETE:
            rows, dropped, tidied = _drop_redundant_cubes(z3, rows, opts,
                                                          budget)
    except Unknown:
        stopped = STOP_UNKNOWN
    except OutOfBudget:
        stopped = STOP_BUDGET

    fixed = [sum(1 for b in r if b is not None) for r in rows]
    return {"rows": rows, "truncated": stopped != STOP_COMPLETE,
            "stopped": stopped, "redundant_dropped": dropped,
            "redundancy_checked": tidied,
            "avg_literals": round(sum(fixed) / float(len(fixed)), 2) if fixed
                            else 0,
            "stats": budget.stats()}


# ------------------------------------------------------------------- cone lookup

def parse_cone_id(text):
    s = text.strip()
    low = s.lower()
    if low.startswith("cone"):
        s = s[4:]
    s = s.lstrip("#")
    try:
        return int(s)
    except ValueError:
        raise ConeError(
            "%r is not a cone identifier. A cone is identified by its integer "
            "id in CONES.json (0 .. n-1); 'CONE7', 'cone7' and '7' all mean id "
            "7, which is also the name bench/conesToVerilog.py gives that cone's "
            "Verilog module. Use --by-root to address a cone by its root net."
            % text)


def parse_also(text):
    """`CONE9=1` -> (9, 1)."""
    if "=" not in text:
        raise ConeError("--also wants CONE=VALUE (e.g. --also CONE9=1), got %r"
                        % text)
    ident, _eq, value = text.rpartition("=")
    value = value.strip()
    if value not in ("0", "1"):
        raise ConeError("--also %r: the target value must be 0 or 1, not %r"
                        % (text, value))
    return parse_cone_id(ident), int(value)


def find_cone(cones_data, cone_id=None, root_net=None):
    cones = cones_data["cones"]
    if root_net is not None:
        for c in cones:
            if c["root_net"] == root_net:
                return c
        raise ConeError("no cone is rooted at net %d (%d cones, roots %d..%d)"
                        % (root_net, len(cones),
                           min(c["root_net"] for c in cones),
                           max(c["root_net"] for c in cones)))
    for c in cones:
        if c["id"] == cone_id:
            return c
    raise ConeError("no cone with id %d; CONES.json holds %d cones, ids 0..%d"
                    % (cone_id, len(cones), len(cones) - 1))


# ------------------------------------------------------------------- reporting

def ensure_leaf_origins(cones_data, warnings):
    """Make sure every leaf carries stage 4's ``from_cone`` tag.

    A CONES.json written before the tag existed simply lacks it. Recomputing it
    here with stage 4's own function costs nothing, means this tool never needs
    the file regenerated to say which cone feeds an input, and — because it is
    literally the same function — can never disagree with what stage 4 writes.
    """
    cones = cones_data.get("cones", [])
    if all("from_cone" in leaf for c in cones for leaf in c["leaves"]):
        return False
    annotate_leaf_origins(cones, cones_data.get("registers", []), warnings)
    return True


def producer_index(goals):
    """(register instance, output pin) -> the cone driving that register's data."""
    out = {}
    for cone, _value in goals:
        for leaf in cone["leaves"]:
            if leaf["kind"] == "reg" and leaf.get("from_cone") is not None:
                out[(leaf["inst"], leaf["pin"])] = leaf["from_cone"]
    return out


def origin_of(leaf, producers):
    """Where one boundary input comes from, in a few words.

    A register output is the only boundary kind that leads anywhere, so it is the
    only one that can name a cone. Everything else says what it is instead, which
    is always true even where it is not a cone: reasoning stops at the boundary
    either way.
    """
    kind = leaf["kind"]
    if kind == "port":
        return "primary input %s" % leaf["name"]
    if kind == "reg":
        cone_id = producers.get((leaf["inst"], leaf["pin"]))
        if cone_id is not None:
            return "CONE%d" % cone_id
        return "register %s (no single cone feeds its data pin)" % leaf["inst"]
    if kind == "opaque":
        return "black box %s" % leaf["inst"]
    if kind == "undriven":
        return "undriven %s" % leaf.get("net", "?")
    if kind == "const":
        return "constant %s" % leaf.get("value", "?")
    return "unknown boundary kind %r" % kind


def sink_labels(cone):
    out = []
    for s in cone["sinks"]:
        if s["kind"] == "port":
            out.append("port %s" % s["name"])
        else:
            out.append("%s.%s" % (s["inst"], s["pin"]))
    return out


def _stop_phrase(result, opts):
    return {STOP_LIMIT: "stopped at --limit %d; there is genuinely more"
                        % opts.limit,
            STOP_BUDGET: "the --budget of %ss ran out" % opts.budget,
            STOP_UNKNOWN: "a solve hit the --timeout of %ss" % opts.timeout,
            }.get(result.get("stopped"), "stopped early")


def _stop_advice(result, opts):
    return {STOP_LIMIT: "Raise --limit for the rest.",
            STOP_BUDGET: "Raise --budget, or narrow the question.",
            STOP_UNKNOWN: "Raise --timeout. What is reported was still proved; "
                          "what is missing was not disproved.",
            }.get(result.get("stopped"), "")


def _print_stats(w, result):
    st = result.get("stats")
    if st:
        w("[%d solver calls, %.2fs solving, %.2fs wall]\n"
          % (st["solver_calls"], st["solve_seconds"], st["wall_seconds"]))


def print_human(result, formula, goals, opts, out=None):
    # Resolved on the call, not bound at import: a default of sys.stdout would
    # freeze the stream this module was imported with and ignore a redirect.
    w = (out or sys.stdout).write
    labels = formula.input_labels()
    target = goals[0][1]
    for cone, value in goals:
        w("cone %d (CONE%d)  root net %d  target %d  drives %s\n"
          % (cone["id"], cone["id"], cone["root_net"], value,
             ", ".join(sink_labels(cone)) or "(nothing)"))
    if len(goals) > 1:
        w("goal: all %d cones at their target together, over one shared "
          "boundary\n" % len(goals))
    w("cells: %d   boundary inputs: %d\n"
      % (len({i for cone, _v in goals for i in cone["cells"]}), len(labels)))
    origins = result.get("input_origins") or [""] * len(labels)
    width = max([len(l) for l in labels] or [0])
    for i, (label, origin) in enumerate(zip(labels, origins)):
        w("  input %2d  %-*s  <-  %s\n" % (i, width, label, origin))
    if formula.const_nets:
        w("constants folded in: %d net(s)\n" % len(formula.const_nets))
    w("\n")

    mode = opts.mode
    if mode == "support":
        for row in result["rows"]:
            w("support %-28s %s\n"
              % (row["input"], "yes" if row["relevant"] else "NO (irrelevant)"))
        w("\nsummary: %d of %d inputs in functional support\n"
          % (result["n_relevant"], len(result["rows"])))
        _print_stats(w, result)
        return

    if mode == "count":
        goalstr = ("drive the output to %d" % target if len(goals) == 1
                   else "satisfy all %d cones at once" % len(goals))
        if result["multiplier"] > 1:
            w("counted over the %d-input functional support; the %d input(s) "
              "nothing depends on multiply it by %d\n"
              % (len(labels) - (result["multiplier"].bit_length() - 1),
                 result["multiplier"].bit_length() - 1, result["multiplier"]))
        if result["exact"]:
            w("count %d\n\nsummary: %d assignments %s\n"
              % (result["count"], result["count"], goalstr))
        else:
            w("count >=%d (%s)\n\nsummary: at least %d assignments %s -- %s\n"
              % (result["count"], _stop_phrase(result, opts), result["count"],
                 goalstr, _stop_advice(result, opts)))
        _print_stats(w, result)
        return

    header = "implicant" if mode == "implicants" else "model"
    for row in result["rows"]:
        w("%s %s\n" % (header, _cube_str(row)))
    w("\n")
    what = ("the %d-set" % target if len(goals) == 1
            else "the set where all %d cones hit their targets" % len(goals))
    drives = ("driving the output to %d" % target if len(goals) == 1
              else "satisfying all %d cones at once" % len(goals))
    if mode == "implicants":
        w("summary: %d prime implicant%s covering %s over %d inputs "
          "('-' is a don't-care)\n"
          % (len(result["rows"]), "" if len(result["rows"]) == 1 else "s",
             what, len(labels)))
    else:
        w("summary: %d model%s %s over %d inputs\n"
          % (len(result["rows"]), "" if len(result["rows"]) == 1 else "s",
             drives, len(labels)))
    if mode == "implicants" and not result.get("redundancy_checked", True):
        w("(too many cubes to check for redundancy; some may be covered by "
          "the others)\n")
    if mode == "implicants" and result.get("redundant_dropped"):
        n = result["redundant_dropped"]
        w("(%d further cube%s the others already covered %s dropped)\n"
          % (n, "" if n == 1 else "s", "was" if n == 1 else "were"))
    if mode == "implicants" and result["rows"] and not result["truncated"]:
        dc = len(labels) - result["avg_literals"]
        w("average %.2f of %d inputs fixed per cube (%.2f don't-care)%s\n"
          % (result["avg_literals"], len(labels), dc,
             " -- this function has no don't-cares, so its implicants are just "
             "its minterms" if dc == 0 else ""))
    if mode == "models" and result["multiplier"] > 1:
        w("'-' marks an input nothing depends on; each row therefore stands "
          "for %d full assignments (%d in total)\n"
          % (result["multiplier"], result["count"]))
    if result["truncated"]:
        w("PARTIAL: %s. %s\n" % (_stop_phrase(result, opts),
                                  _stop_advice(result, opts)))
    _print_stats(w, result)


# ------------------------------------------------------------------------- CLI

def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="CONE7 means cone id 7. Reasoning stops at register boundaries: "
               "a flop's Q is a boundary input, its D is a cone output.")
    ap.add_argument("cone", nargs="?",
                    help="cone identifier: CONE7, cone7 or 7 (omit with --by-root)")
    ap.add_argument("target", type=int, choices=(0, 1),
                    help="the value to drive the cone's output to")
    ap.add_argument("--by-root", type=int, metavar="NET",
                    help="address the cone by its root net instead of its id")
    ap.add_argument("--also", action="append", default=[], metavar="CONE=VALUE",
                    help="a second cone to satisfy at the same time, e.g. "
                         "--also CONE9=1. Repeatable. All the cones share one "
                         "boundary, so the answer holds for them together -- "
                         "which is not what running the tool twice gives you.")
    ap.add_argument("--graph", default=DEFAULT_GRAPH,
                    help="netlist graph JSON (default %s)" % DEFAULT_GRAPH)
    ap.add_argument("--cones", default=DEFAULT_CONES,
                    help="cone decomposition JSON (default %s)" % DEFAULT_CONES)
    ap.add_argument("--mode", default="implicants",
                    choices=("implicants", "models", "support", "count"))
    ap.add_argument("--limit", type=int, default=100,
                    help="cap on result rows / counted models (default 100)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="per-solve Z3 timeout in seconds (default 30)")
    ap.add_argument("--budget", type=float, default=None, metavar="SEC",
                    help="wall-clock cap for the whole run. On expiry the "
                         "answers already found are reported, marked partial, "
                         "rather than thrown away")
    ap.add_argument("--seed", type=int, default=0,
                    help="Z3 random seed (default 0). The same question gives "
                         "the same answer, so output can be diffed")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output on stdout")
    return ap


def analyse(graph, cones_data, opts, warnings):
    """The whole analysis, as data. Shared by the CLI and the fixture suite."""
    import z3

    ensure_leaf_origins(cones_data, warnings)
    if opts.by_root is not None:
        first = find_cone(cones_data, root_net=opts.by_root)
    else:
        first = find_cone(cones_data, cone_id=parse_cone_id(opts.cone))
    goals = [(first, opts.target)]
    for text in getattr(opts, "also", []) or []:
        cid, value = parse_also(text)
        goals.append((find_cone(cones_data, cone_id=cid), value))

    nl = Netlist(graph, warnings)
    # Always joint, even for one cone: one code path, and the single-cone case
    # is just the degenerate case of it.
    formula = JointFormula(nl, goals)
    cone = first

    budget = Budget(opts.budget)
    if opts.mode == "support":
        result = run_support(z3, formula, opts, budget)
        result["status"] = "sat"
    elif opts.mode == "count":
        result = run_count(z3, formula, 1, opts, budget)
        result["status"] = "unsat" if result["count"] == 0 else "sat"
    elif opts.mode == "models":
        result = run_models(z3, formula, 1, opts, budget)
        result["status"] = "unsat" if not result["rows"] else "sat"
    else:
        result = run_implicants(z3, formula, 1, opts, budget)
        result["status"] = "unsat" if not result["rows"] else "sat"

    # A run that stopped early proved nothing about emptiness.
    if result.get("stopped") in (STOP_UNKNOWN, STOP_BUDGET) \
            and result["status"] == "unsat":
        result["status"] = "unknown"

    result["cone"] = cone["id"]
    result["module"] = "CONE%d" % cone["id"]
    result["root_net"] = cone["root_net"]
    result["target"] = opts.target
    result["mode"] = opts.mode
    result["inputs"] = formula.input_labels()
    producers = producer_index(goals)
    result["input_origins"] = [origin_of(leaf, producers)
                               for _key, _label, leaf in formula.inputs]
    result["input_cones"] = [producers.get((leaf["inst"], leaf["pin"]))
                             if leaf["kind"] == "reg" else None
                             for _key, _label, leaf in formula.inputs]
    result["n_inputs"] = formula.n_inputs()
    result["n_cells"] = len({i for c, _v in goals for i in c["cells"]})
    result["sinks"] = sink_labels(cone)
    result["goals"] = [{"cone": c["id"], "module": "CONE%d" % c["id"],
                        "root_net": c["root_net"], "target": v,
                        "sinks": sink_labels(c), "n_cells": len(c["cells"])}
                       for c, v in goals]
    return result, formula, goals


def main(argv=None):
    opts = build_parser().parse_args(argv)
    if opts.cone is None and opts.by_root is None:
        print("error: give a cone identifier (CONE7) or --by-root NET",
              file=sys.stderr)
        return EXIT_ERROR
    if opts.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return EXIT_ERROR

    for path in (opts.graph, opts.cones):
        if not os.path.exists(path):
            print("error: %s does not exist. Run ./runPipeline.sh first, or "
                  "pass --graph/--cones." % path, file=sys.stderr)
            return EXIT_ERROR

    try:
        import z3                                    # noqa: F401
    except ImportError:
        print("error: this tool needs Z3. Install it with: "
              "python3 -m pip install z3-solver", file=sys.stderr)
        return EXIT_ERROR

    with open(opts.graph) as f:
        graph = json.load(f)
    with open(opts.cones) as f:
        cones_data = json.load(f)

    warnings = []
    try:
        result, formula, goals = analyse(graph, cones_data, opts, warnings)
    except ConeError as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_ERROR
    except Unknown as e:
        print("UNKNOWN: the solver gave up (%s). Raise --timeout." % e,
              file=sys.stderr)
        return EXIT_UNKNOWN

    result["warnings"] = warnings
    if opts.json:
        json.dump(result, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print_human(result, formula, goals, opts)
        for msg in warnings[:10]:
            print("warning:", msg)

    if result["status"] == "unsat":
        if len(goals) == 1:
            msg = ("UNSAT: no assignment to cone %d's %d boundary inputs drives "
                   "its output to %d. The cone is constant %d."
                   % (goals[0][0]["id"], formula.n_inputs(), opts.target,
                      1 - opts.target))
        else:
            msg = ("UNSAT: no assignment to the %d shared boundary inputs "
                   "satisfies %s at once. Each may still be reachable on its "
                   "own -- these targets are not simultaneously reachable."
                   % (formula.n_inputs(),
                      " and ".join("CONE%d=%d" % (c["id"], v) for c, v in goals)))
        if opts.json:
            print(msg, file=sys.stderr)
        else:
            print(msg)
        return EXIT_UNSAT
    return 0


if __name__ == "__main__":
    sys.exit(main())
