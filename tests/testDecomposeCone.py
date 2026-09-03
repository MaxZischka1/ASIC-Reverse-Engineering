#!/usr/bin/env python3
"""Synthetic tests for decomposeCone.py. Nothing here reads the puzzle: every
cone is hand-built from the fixture cells in testConeDecompose.CELLS, and every
expected answer is either worked out by hand or cross-checked against an
independent evaluator.

Two layers, because they catch different mistakes:

  * hand-computed truth tables for AND2/OR2/XOR2/MUX2 and the inverting cells
    (nand2, nor2, o21ai, a21oi) -- this is the layer that would catch a polarity
    error in genCellModels.FAMILY_FUNCS itself;
  * an exhaustive cross-check of --mode count over randomly generated cones,
    against three evaluators that fail for different reasons: stage 5's
    canonical normal form (coneClasses.cone_function + boolExpr.evaluate), a
    dumb per-assignment interpreter, and -- crucially -- an evaluator driven by
    the hand tables above rather than by FAMILY_FUNCS. The first two catch a
    mistake in the Z3 encoding; the third also catches a polarity or pin-order
    error in the cell table itself, which the other two cannot, because they
    read that table too.

Run:  python3 tests/testDecomposeCone.py
"""

import itertools
import os
import random
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boolExpr                                          # noqa: E402
import decomposeCone                                     # noqa: E402
from coneClasses import cone_function                    # noqa: E402
from coneDecompose import Netlist, decompose             # noqa: E402
from decomposeCone import (ConeError, ConeFormula, JointFormula,  # noqa: E402
                           cell_model, find_cone, parse_also, parse_cone_id,
                           run_count, run_implicants, run_models, run_support)
from testConeDecompose import CELLS, HD, make_graph      # noqa: E402

import z3                                                # noqa: E402


class Opts(object):
    """The handful of option fields the mode functions read."""

    def __init__(self, limit=1000, timeout=30.0, target=1, mode="implicants",
                 budget=None, seed=0):
        self.limit = limit
        self.timeout = timeout
        self.target = target
        self.mode = mode
        self.budget = budget
        self.seed = seed


# --------------------------------------------------------------- cone building

ROOT_NET = 100
CLK_NET = 999


def single_output_cone(instances, nets, ports):
    """Build a graph whose one cone is rooted at the flop's D pin.

    The caller wires its logic to ROOT_NET; this bolts a flop onto that net so
    stage 4 produces exactly one cone for it. Every cone in this suite therefore
    comes out of the real stage-4 decomposition, never out of a hand-written
    cone record.
    """
    merged = []
    seen_root = False
    for nid, eps in nets:
        eps = list(eps)
        if nid == ROOT_NET:
            eps.append(("uff", "D", "input"))
            seen_root = True
        merged.append((nid, eps))
    assert seen_root, "the fixture must drive net %d" % ROOT_NET
    merged.append((CLK_NET, [("uff", "CLK", "input")]))

    g = make_graph(instances=list(instances) + [("uff", HD + "dfxtp_1")],
                   nets=merged,
                   ports=list(ports) + [("CLK", "input", CLK_NET)])
    res = decompose(g, [])
    cones = [c for c in res["cones"] if c["root_net"] == ROOT_NET]
    assert len(cones) == 1, [c["root_net"] for c in res["cones"]]
    return Netlist(g, []), cones[0], g, res


def gate_cone(cell, pin_nets, out_pin):
    """One gate driving the cone root, its inputs coming from primary inputs.

    pin_nets maps input pin -> a small net id, one primary input each.
    """
    endpoints = {}
    ports = []
    for pin, net in sorted(pin_nets.items()):
        endpoints.setdefault(net, []).append(("u0", pin, "input"))
    nets = [(net, eps) for net, eps in sorted(endpoints.items())]
    for net in sorted(endpoints):
        ports.append(("I%d" % net, "input", net))
    nets.append((ROOT_NET, [("u0", out_pin, "output")]))
    return [("u0", cell)], nets, ports


# ----------------------------------------------------------------- evaluation

def expand(rows):
    """Model rows carry '-' (None) for inputs the function ignores. Expand each
    row back into the full assignments it stands for, so a test can compare
    against an exhaustive reference."""
    out = set()
    for row in rows:
        free = [i for i, b in enumerate(row) if b is None]
        for fill in itertools.product((0, 1), repeat=len(free)):
            full = list(row)
            for i, v in zip(free, fill):
                full[i] = v
            out.add(tuple(full))
    return out


def z3_table(nl, cone, target=1):
    """The set of input assignments driving the cone to `target`, from the Z3
    encoding, as a set of tuples in ConeFormula input order."""
    formula = ConeFormula(nl, cone)
    res = run_models(z3, formula, target, Opts(limit=1 << 16))
    assert not res["truncated"], res["stopped"]
    got = expand(res["rows"])
    # the reported count must match what the rows actually stand for
    assert res["count"] == len(got), (res["count"], len(got))
    return formula, got


def reference_table(nl, cone, formula, target=1):
    """The same set, computed without Z3: stage 5's canonical normal form,
    evaluated exhaustively over the same boundary inputs."""
    node, extra = cone_function(nl, cone, {}, [])
    assert node is not None, extra
    leaf_nets = extra

    # cone_function numbers its variables by its own traversal; map them onto
    # ConeFormula's input order so the two sets are comparable.
    pos_of_net = {}
    for i, (key, _label, _leaf) in enumerate(formula.inputs):
        if key[0] == "net":
            pos_of_net[key[1]] = i

    used = sorted(boolExpr.variables(node))
    out = set()
    k = formula.n_inputs()
    for bits in itertools.product((0, 1), repeat=k):
        vals = {}
        for vi in used:
            net = leaf_nets[vi]
            assert net in pos_of_net, "leaf net %d is not a boundary input" % net
            vals[vi] = bits[pos_of_net[net]]
        if boolExpr.evaluate(node, vals, 1) == target:
            out.add(bits)
    return out


def brute_force_table(nl, cone, formula, target=1):
    """A third, dumbest possible evaluator: walk the cone's gates one assignment
    at a time, evaluating each cell's parsed expression directly in Python."""
    out = set()
    k = formula.n_inputs()
    for bits in itertools.product((0, 1), repeat=k):
        value = {}
        for net, bit in formula.const_nets.items():
            value[net] = 1 if bit == "1" else 0
        assign = {}
        for i, (key, _label, _leaf) in enumerate(formula.inputs):
            assign[key] = bits[i]
            if key[0] == "net":
                value[key[1]] = bits[i]
        for inst in cone["cells"]:
            cell = nl.inst_cell[inst]
            env = {}
            for pin, src in nl.input_nets(inst):
                if isinstance(src, tuple):
                    env[pin] = 1 if src[1] == "1" else 0
                elif src is None:
                    env[pin] = assign[("nc", inst, pin)]
                else:
                    env[pin] = value[src]
            for pin, ast in cell_model(cell).items():
                onet = nl.net_of.get((inst, pin))
                if onet is None:
                    continue
                value[onet] = eval_ast(ast, env)
        if value[cone["root_net"]] == target:
            out.add(bits)
    return out


def hand_table_of(cell):
    """One gate's function from the hand-written HAND tables -- never from
    FAMILY_FUNCS. This is what makes the randomised cross-check able to catch a
    polarity or pin-order error in the cell table itself, rather than only in
    the code that reads it."""
    pins, out_pin, onset = HAND[cell]
    return sorted(pins), out_pin, onset


def hand_force_table(nl, cone, formula, target=1):
    """The cone's on/off-set, evaluated purely from the hand-written gate
    tables. Shares no cell model with the code under test."""
    out = set()
    k = formula.n_inputs()
    for bits in itertools.product((0, 1), repeat=k):
        value = {}
        for net, bit in formula.const_nets.items():
            value[net] = 1 if bit == "1" else 0
        assign = {}
        for i, (key, _label, _leaf) in enumerate(formula.inputs):
            assign[key] = bits[i]
            if key[0] == "net":
                value[key[1]] = bits[i]
        for inst in cone["cells"]:
            cell = nl.inst_cell[inst]
            pins, out_pin, onset = hand_table_of(cell)
            env = {}
            for pin, src in nl.input_nets(inst):
                if isinstance(src, tuple):
                    env[pin] = 1 if src[1] == "1" else 0
                elif src is None:
                    env[pin] = assign[("nc", inst, pin)]
                else:
                    env[pin] = value[src]
            onet = nl.net_of.get((inst, out_pin))
            if onet is None:
                continue
            value[onet] = 1 if tuple(env[p] for p in pins) in onset else 0
        if value[cone["root_net"]] == target:
            out.add(bits)
    return out


def eval_ast(ast, env):
    kind = ast[0]
    if kind == "lit":
        return ast[1]
    if kind == "name":
        return env[ast[1]]
    if kind == "not":
        return 1 - eval_ast(ast[1], env)
    if kind == "and2":
        return eval_ast(ast[1], env) & eval_ast(ast[2], env)
    if kind == "or2":
        return eval_ast(ast[1], env) | eval_ast(ast[2], env)
    if kind == "xor2":
        return eval_ast(ast[1], env) ^ eval_ast(ast[2], env)
    if kind == "mux":
        return eval_ast(ast[2], env) if eval_ast(ast[1], env) else \
            eval_ast(ast[3], env)
    raise AssertionError("unknown ast %r" % (ast,))


# -------------------------------------------------------------- hand-built cases

# cell -> (input pins, output pin, expected on-set as a set of tuples in
# sorted-pin order). Worked out by hand, not read off FAMILY_FUNCS.
HAND = {
    HD + "and2_1":  (["A", "B"], "X", {(1, 1)}),
    HD + "or2_1":   (["A", "B"], "X", {(0, 1), (1, 0), (1, 1)}),
    HD + "xor2_1":  (["A", "B"], "X", {(0, 1), (1, 0)}),
    HD + "xnor2_1": (["A", "B"], "Y", {(0, 0), (1, 1)}),
    HD + "nand2_1": (["A", "B"], "Y", {(0, 0), (0, 1), (1, 0)}),
    HD + "nor2_1":  (["A", "B"], "Y", {(0, 0)}),
    HD + "inv_1":   (["A"], "Y", {(0,)}),
    HD + "buf_1":   (["A"], "X", {(1,)}),
    # mux2: X = S ? A1 : A0, pins sorted as (A0, A1, S)
    HD + "mux2_1":  (["A0", "A1", "S"], "X",
                     {(0, 1, 1), (1, 1, 1), (1, 0, 0), (1, 1, 0)}),
    # o21ai: Y = ~((A1 | A2) & B1), pins sorted (A1, A2, B1)
    HD + "o21ai_1": (["A1", "A2", "B1"], "Y",
                     {(0, 0, 0), (0, 0, 1), (0, 1, 0), (1, 0, 0), (1, 1, 0)}),
    # a21oi: Y = ~((A1 & A2) | B1), pins sorted (A1, A2, B1)
    HD + "a21oi_1": (["A1", "A2", "B1"], "Y",
                     {(0, 0, 0), (0, 1, 0), (1, 0, 0)}),
    # maj3: X = at least two of A, B, C
    HD + "maj3_1":  (["A", "B", "C"], "X",
                     {(0, 1, 1), (1, 0, 1), (1, 1, 0), (1, 1, 1)}),
    # nand3: Y = ~(A & B & C)
    HD + "nand3_1": (["A", "B", "C"], "Y",
                     {b for b in itertools.product((0, 1), repeat=3)
                      if b != (1, 1, 1)}),
    # o22ai: Y = ~((A1 | A2) & (B1 | B2)), pins sorted (A1, A2, B1, B2)
    HD + "o22ai_1": (["A1", "A2", "B1", "B2"], "Y",
                     {b for b in itertools.product((0, 1), repeat=4)
                      if not ((b[0] or b[1]) and (b[2] or b[3]))}),
    # a22oi: Y = ~((A1 & A2) | (B1 & B2))
    HD + "a22oi_1": (["A1", "A2", "B1", "B2"], "Y",
                     {b for b in itertools.product((0, 1), repeat=4)
                      if not ((b[0] and b[1]) or (b[2] and b[3]))}),
    # mux4: X = S1 ? (S0 ? A3 : A2) : (S0 ? A1 : A0)
    # pins sorted (A0, A1, A2, A3, S0, S1)
    HD + "mux4_1":  (["A0", "A1", "A2", "A3", "S0", "S1"], "X",
                     {b for b in itertools.product((0, 1), repeat=6)
                      if b[{(0, 0): 0, (1, 0): 1, (0, 1): 2,
                            (1, 1): 3}[(b[4], b[5])]]}),
}


def check_single_gates_against_hand_tables():
    """Every gate's on-set, compared against a table written out by hand.

    This is the polarity test: it does not consult FAMILY_FUNCS for the expected
    answer, so an inverting cell modelled non-inverting fails here.
    """
    for cell, (pins, out_pin, expected) in sorted(HAND.items()):
        pin_nets = {pin: i for i, pin in enumerate(sorted(pins))}
        instances, nets, ports = gate_cone(cell, pin_nets, out_pin)
        nl, cone, _g, _res = single_output_cone(instances, nets, ports)
        formula, got = z3_table(nl, cone, 1)
        assert formula.input_labels() == ["I%d" % i for i in range(len(pins))], \
            formula.input_labels()
        assert got == expected, (cell, sorted(got), sorted(expected))

        # and the off-set must be the exact complement
        _f, off = z3_table(nl, cone, 0)
        allbits = set(itertools.product((0, 1), repeat=len(pins)))
        assert off == allbits - expected, (cell, sorted(off))
    print("hand tables: OK (%d cells, on-set and off-set)" % len(HAND))


def check_deep_nested_cone():
    """A 5-input cone with a hand-computed function.

    u0: nand2(I0, I1)            -> n10
    u1: nor2(I2, I3)             -> n11
    u2: xor2(n10, n11)           -> n12
    u3: o21ai(A1=n12, A2=I4, B1=I0) -> root

    root = ~(((I0 nand I1) xor (I2 nor I3) | I4) & I0)
    """
    instances = [("u0", HD + "nand2_1"), ("u1", HD + "nor2_1"),
                 ("u2", HD + "xor2_1"), ("u3", HD + "o21ai_1")]
    nets = [
        (0, [("u0", "A", "input"), ("u3", "B1", "input")]),      # I0, reconvergent
        (1, [("u0", "B", "input")]),                             # I1
        (2, [("u1", "A", "input")]),                             # I2
        (3, [("u1", "B", "input")]),                             # I3
        (4, [("u3", "A2", "input")]),                            # I4
        (10, [("u0", "Y", "output"), ("u2", "A", "input")]),
        (11, [("u1", "Y", "output"), ("u2", "B", "input")]),
        (12, [("u2", "X", "output"), ("u3", "A1", "input")]),
        (100, [("u3", "Y", "output")]),
    ]
    ports = [("I%d" % i, "input", i) for i in range(5)]
    nl, cone, _g, _res = single_output_cone(instances, nets, ports)
    formula, got = z3_table(nl, cone, 1)
    assert formula.input_labels() == ["I0", "I1", "I2", "I3", "I4"], \
        formula.input_labels()

    expected = set()
    for i0, i1, i2, i3, i4 in itertools.product((0, 1), repeat=5):
        n10 = 1 - (i0 & i1)
        n11 = 1 - (i2 | i3)
        n12 = n10 ^ n11
        root = 1 - ((n12 | i4) & i0)
        if root:
            expected.add((i0, i1, i2, i3, i4))
    assert got == expected, (sorted(got - expected), sorted(expected - got))
    assert len(cone["cells"]) == 4, cone["cells"]
    print("deep nested cone: OK (4 levels, reconvergent fan-out, %d minterms)"
          % len(expected))


def check_irrelevant_input_is_caught():
    """An input the wiring provides that the function cannot use.

    u0: and2(I0, I1)      -> n10
    u1: inv(I1)           -> n11
    u2: and2(n10, n11)    -> n12    ==  I0 & I1 & ~I1  ==  0 ... so instead:
    u2: or2(n10, n11)     -> root   ==  (I0 & I1) | ~I1

    Neither I0 nor I1 is irrelevant there, so build the classic one instead:
    root = xor2(I1, I1) fed through -- but stage 4 dedupes nets, so use a mux
    whose two data inputs are the same signal: X = S ? A : A == A, and S is
    structurally present but functionally irrelevant.
    """
    instances = [("u0", HD + "mux2_1")]
    nets = [
        (0, [("u0", "A0", "input"), ("u0", "A1", "input")]),   # I0 -> both data
        (1, [("u0", "S", "input")]),                           # I1 -> select
        (100, [("u0", "X", "output")]),
    ]
    ports = [("I0", "input", 0), ("I1", "input", 1)]
    nl, cone, _g, _res = single_output_cone(instances, nets, ports)
    formula = ConeFormula(nl, cone)
    assert formula.input_labels() == ["I0", "I1"], formula.input_labels()

    res = run_support(z3, formula, Opts())
    by_input = {r["input"]: r["relevant"] for r in res["rows"]}
    assert by_input["I0"] is True, res["rows"]
    assert by_input["I1"] is False, res["rows"]
    assert res["n_relevant"] == 1, res

    # The mux select really is structurally present: it is a boundary input and
    # the cone's leaves list it, which is what makes this test worth having.
    assert any(l.get("name") == "I1" for l in cone["leaves"]), cone["leaves"]

    # A second shape: reconvergence that cancels. root = xor2(n10, n10') where
    # both come from I1 through an even/odd inverter pair.
    instances = [("u0", HD + "inv_1"), ("u1", HD + "buf_1"),
                 ("u2", HD + "xnor2_1"), ("u3", HD + "and2_1")]
    nets = [
        (0, [("u0", "A", "input"), ("u1", "A", "input")]),     # I0
        (1, [("u3", "B", "input")]),                           # I1
        (10, [("u0", "Y", "output"), ("u2", "A", "input")]),   # ~I0
        (11, [("u1", "X", "output"), ("u2", "B", "input")]),   # I0
        (12, [("u2", "Y", "output"), ("u3", "A", "input")]),   # ~(~I0 ^ I0) = 0
        (100, [("u3", "X", "output")]),                        # 0 & I1 = 0
    ]
    ports = [("I0", "input", 0), ("I1", "input", 1)]
    nl, cone, _g, _res = single_output_cone(instances, nets, ports)
    formula = ConeFormula(nl, cone)
    res = run_support(z3, formula, Opts())
    assert res["n_relevant"] == 0, res["rows"]
    print("irrelevant inputs: OK (mux select on tied data, cancelling "
          "reconvergence)")


def check_constant_cone_is_unsat():
    """A cone that can never reach the target: UNSAT, not a stack trace."""
    # root = ~(~I0 ^ I0) & I1 -> always 0 (from the case above)
    instances = [("u0", HD + "inv_1"), ("u1", HD + "buf_1"),
                 ("u2", HD + "xnor2_1"), ("u3", HD + "and2_1")]
    nets = [
        (0, [("u0", "A", "input"), ("u1", "A", "input")]),
        (1, [("u3", "B", "input")]),
        (10, [("u0", "Y", "output"), ("u2", "A", "input")]),
        (11, [("u1", "X", "output"), ("u2", "B", "input")]),
        (12, [("u2", "Y", "output"), ("u3", "A", "input")]),
        (100, [("u3", "X", "output")]),
    ]
    ports = [("I0", "input", 0), ("I1", "input", 1)]
    nl, cone, g, res_dec = single_output_cone(instances, nets, ports)
    formula = ConeFormula(nl, cone)

    for mode_fn in (run_implicants, run_models):
        r = mode_fn(z3, formula, 1, Opts())
        assert r["rows"] == [], (mode_fn, r)
    r = run_count(z3, formula, 1, Opts())
    assert (r["count"], r["exact"]) == (0, True), r

    # ... while target 0 is satisfied by everything
    r = run_count(z3, formula, 0, Opts())
    assert r["count"] == 4 and r["exact"], r

    # and the CLI reports it plainly with exit code 2
    code, out, err = run_cli(g, res_dec, [str(cone["id"]), "1"])
    assert code == decomposeCone.EXIT_UNSAT, (code, out, err)
    assert "UNSAT" in out and "constant 0" in out, out
    assert "Traceback" not in err, err

    # a constant-1 cone reports constant 1 when asked for 0
    code, out, _err = run_cli(g, res_dec, [str(cone["id"]), "0"])
    assert code == 0, (code, out)
    print("constant cone: OK (UNSAT both APIs, exit %d, plain message)"
          % decomposeCone.EXIT_UNSAT)


def check_tied_and_unconnected_pins():
    """A tied-off pin folds to a constant; an unconnected pin is its own bit."""
    # and2 with B tied to 1 -> root = I0. B must not be a boundary input.
    g = make_graph(
        instances=[("u0", HD + "and2_1"), ("uff", HD + "dfxtp_1")],
        nets=[(0, [("u0", "A", "input")]),
              (100, [("u0", "X", "output"), ("uff", "D", "input")]),
              (999, [("uff", "CLK", "input")])],
        ports=[("I0", "input", 0), ("CLK", "input", 999)],
        tied_pins=[("u0", "B", "1")])
    nl = Netlist(g, [])
    cone = find_cone(decompose(g, []), cone_id=0)
    formula = ConeFormula(nl, cone)
    assert formula.input_labels() == ["I0"], formula.input_labels()
    r = run_implicants(z3, formula, 1, Opts())
    assert r["rows"] == [[1]], r["rows"]

    # the same cell with B left unconnected: B becomes its own free bit
    g = make_graph(
        instances=[("u0", HD + "and2_1"), ("uff", HD + "dfxtp_1")],
        nets=[(0, [("u0", "A", "input")]),
              (100, [("u0", "X", "output"), ("uff", "D", "input")]),
              (999, [("uff", "CLK", "input")])],
        ports=[("I0", "input", 0), ("CLK", "input", 999)])
    nl = Netlist(g, [])
    cone = find_cone(decompose(g, []), cone_id=0)
    formula = ConeFormula(nl, cone)
    assert formula.n_inputs() == 2, formula.input_labels()
    assert "u0.B" in formula.input_labels()[1], formula.input_labels()
    r = run_count(z3, formula, 1, Opts())
    assert r["count"] == 1, r
    print("tied / unconnected pins: OK (tie folds, dangling pin stays free)")


def check_const_net_folds():
    """A net driven by a conb tie cell is a constant, not an input."""
    g = make_graph(
        instances=[("u0", HD + "and2_1"), ("uff", HD + "dfxtp_1")],
        nets=[(0, [("u0", "A", "input")]),
              (1, [("u0", "B", "input")]),
              (100, [("u0", "X", "output"), ("uff", "D", "input")]),
              (999, [("uff", "CLK", "input")])],
        ports=[("I0", "input", 0), ("CLK", "input", 999)],
        const_nets=[(1, "0")])
    nl = Netlist(g, [])
    cone = find_cone(decompose(g, []), cone_id=0)
    formula = ConeFormula(nl, cone)
    assert formula.input_labels() == ["I0"], formula.input_labels()
    assert formula.const_nets == {1: "0"}, formula.const_nets
    r = run_count(z3, formula, 1, Opts())
    assert r["count"] == 0, r          # A & 0 is never 1
    print("constant nets: OK (tie cell folds, is not a boundary input)")


def check_unmodelled_cell_raises():
    """A cell with no FAMILY_FUNCS entry must stop the tool, not be guessed."""
    CELLS["FICTIONAL_GATE"] = {"inputs": ["A", "B"], "outputs": ["X"]}
    try:
        g = make_graph(
            instances=[("u0", "FICTIONAL_GATE"), ("uff", HD + "dfxtp_1")],
            nets=[(0, [("u0", "A", "input")]),
                  (1, [("u0", "B", "input")]),
                  (100, [("u0", "X", "output"), ("uff", "D", "input")]),
                  (999, [("uff", "CLK", "input")])],
            ports=[("I0", "input", 0), ("I1", "input", 1),
                   ("CLK", "input", 999)])
        # stage 4 calls it opaque, so it becomes a leaf rather than a cone cell;
        # ask decomposeCone about the cell directly, which is the guard that
        # matters when a netlist does put one inside a cone.
        try:
            cell_model("FICTIONAL_GATE")
        except ConeError as e:
            assert "FAMILY_FUNCS" in str(e), e
        else:
            raise AssertionError("an unmodelled cell was silently modelled")
    finally:
        del CELLS["FICTIONAL_GATE"]

    # tri-state cells are not Boolean and must be refused by name
    try:
        cell_model(HD + "einvp_1")
    except ConeError as e:
        assert "Tri-state" in str(e), e
    else:
        raise AssertionError("a tri-state cell was silently modelled")

    # a sequential cell inside a cone is refused as a boundary violation
    try:
        cell_model(HD + "dfxtp_1")
    except ConeError as e:
        assert "sequential" in str(e) and "register boundary" in str(e), e
    else:
        raise AssertionError("a flop was silently modelled as combinational")
    print("unmodelled cells: OK (missing, tri-state and sequential all refused)")


def check_register_boundary():
    """A flop's Q is a boundary input; its D is a cone root. Never crossed."""
    g = make_graph(
        instances=[("u0", HD + "and2_1"), ("ur", HD + "dfxtp_1"),
                   ("uff", HD + "dfxtp_1")],
        nets=[(0, [("u0", "A", "input")]),
              (5, [("ur", "Q", "output"), ("u0", "B", "input")]),
              (6, [("ur", "D", "input")]),
              (100, [("u0", "X", "output"), ("uff", "D", "input")]),
              (999, [("uff", "CLK", "input"), ("ur", "CLK", "input")])],
        ports=[("I0", "input", 0), ("I1", "input", 6), ("CLK", "input", 999)])
    nl = Netlist(g, [])
    res = decompose(g, [])
    cone = [c for c in res["cones"] if c["root_net"] == 100][0]
    formula = ConeFormula(nl, cone)
    assert formula.input_labels() == ["I0", "ur.Q"], formula.input_labels()
    assert cone["cells"] == ["u0"], cone["cells"]     # ur is not inside the cone
    r = run_implicants(z3, formula, 1, Opts())
    assert r["rows"] == [[1, 1]], r["rows"]

    # the register's own D pin is the root of a separate cone
    other = [c for c in res["cones"] if c["root_net"] == 6]
    assert len(other) == 1, [c["root_net"] for c in res["cones"]]
    print("register boundary: OK (Q is an input, D is a root, never crossed)")


# ------------------------------------------------------------- implicant checks

def check_implicants_are_prime_and_cover():
    """Every implicant row is prime, and the rows together cover the on-set."""
    for cell, (pins, out_pin, expected) in sorted(HAND.items()):
        if len(pins) > 4:
            continue
        pin_nets = {pin: i for i, pin in enumerate(sorted(pins))}
        instances, nets, ports = gate_cone(cell, pin_nets, out_pin)
        nl, cone, _g, _res = single_output_cone(instances, nets, ports)
        formula = ConeFormula(nl, cone)
        r = run_implicants(z3, formula, 1, Opts())
        k = len(pins)

        covered = set()
        for cube in r["rows"]:
            pts = cube_points(cube, k)
            # sound: every point of the cube is in the on-set
            assert pts <= expected, (cell, cube, sorted(pts - expected))
            # prime: widening any fixed literal escapes the on-set
            for i, b in enumerate(cube):
                if b is None:
                    continue
                wider = list(cube)
                wider[i] = None
                assert not (cube_points(wider, k) <= expected), \
                    ("not prime", cell, cube, i)
            covered |= pts
        assert covered == expected, (cell, sorted(expected - covered))
    print("implicants: OK (sound, prime, and covering for %d cells)"
          % sum(1 for _c, (p, _o, _e) in HAND.items() if len(p) <= 4))


def cube_points(cube, k):
    free = [i for i, b in enumerate(cube) if b is None]
    out = set()
    for fill in itertools.product((0, 1), repeat=len(free)):
        pt = list(cube)
        for i, v in zip(free, fill):
            pt[i] = v
        out.add(tuple(pt))
    return out


def check_implicant_dont_cares():
    """A cone whose answer genuinely has a don't-care, checked by hand.

    root = or2(I0, I1) -> the on-set cover is {1-, -1}, both prime.
    """
    instances, nets, ports = gate_cone(HD + "or2_1", {"A": 0, "B": 1}, "X")
    nl, cone, _g, _res = single_output_cone(instances, nets, ports)
    formula = ConeFormula(nl, cone)
    r = run_implicants(z3, formula, 1, Opts())
    got = {decomposeCone._cube_str(c) for c in r["rows"]}
    assert got == {"1-", "-1"}, got

    # target 0 of the same cone is the single cube 00
    r = run_implicants(z3, formula, 0, Opts())
    assert [decomposeCone._cube_str(c) for c in r["rows"]] == ["00"], r["rows"]
    print("don't-cares: OK (or2 covers as {1-, -1}, off-set as {00})")


# ------------------------------------------------------------- randomised cross-check

def random_cone(rng, n_inputs, n_gates):
    """A random combinational cone over n_inputs primary inputs."""
    # Drawn from HAND only, so hand_force_table can evaluate whatever comes out.
    choices = [(cell, pins, out_pin)
               for cell, (pins, out_pin, _onset) in sorted(HAND.items())]
    endpoints = {i: [] for i in range(n_inputs)}
    instances = []
    available = list(range(n_inputs))          # net ids that can feed a gate
    for gi in range(n_gates):
        cell, pins, out_pin = rng.choice(choices)
        inst = "u%d" % gi
        instances.append((inst, cell))
        for pin in pins:
            src = rng.choice(available)
            endpoints.setdefault(src, []).append((inst, pin, "input"))
        # the last gate drives the cone root; the others drive internal nets
        out = ROOT_NET if gi == n_gates - 1 else 1000 + gi
        endpoints.setdefault(out, []).append((inst, out_pin, "output"))
        available.append(out)

    nets = [(net, eps) for net, eps in sorted(endpoints.items()) if eps]
    ports = [("I%d" % i, "input", i) for i in range(n_inputs)
             if endpoints.get(i)]
    return instances, nets, ports


def check_count_against_brute_force():
    """--mode count, cross-checked exhaustively for cones of <= 12 inputs.

    Four independent evaluations must agree exactly: the Z3 encoding, stage 5's
    canonical normal form, a dumb per-assignment interpreter, and the hand-written
    gate tables. The middle two share FAMILY_FUNCS with the code under test, so
    they catch encoding mistakes only; the hand tables share nothing with it, so
    a polarity or pin-order error in FAMILY_FUNCS shows up here as well -- over
    compositions of gates, not just single gates.
    """
    rng = random.Random(20260902)
    checked = 0
    gates = 0
    for trial in range(60):
        n_inputs = rng.randint(2, 6)
        n_gates = rng.randint(1, 8)
        instances, nets, ports = random_cone(rng, n_inputs, n_gates)
        try:
            nl, cone, _g, _res = single_output_cone(instances, nets, ports)
        except AssertionError:
            continue                       # generator produced a degenerate cone
        formula = ConeFormula(nl, cone)
        k = formula.n_inputs()
        if k > 12:
            continue

        for target in (0, 1):
            r = run_count(z3, formula, target, Opts(limit=1 << 14))
            assert r["exact"], r
            _f, models = z3_table(nl, cone, target)
            assert len(models) == r["count"], (r, len(models))

            ref = reference_table(nl, cone, formula, target)
            brute = brute_force_table(nl, cone, formula, target)
            hand = hand_force_table(nl, cone, formula, target)
            assert models == ref, ("z3 vs normal form", trial, target,
                                   sorted(models ^ ref))
            assert models == brute, ("z3 vs interpreter", trial, target,
                                     sorted(models ^ brute))
            assert models == hand, ("z3 vs hand tables", trial, target,
                                    sorted(models ^ hand))
            assert r["count"] + len(hand_force_table(nl, cone, formula,
                                                     1 - target)) == (1 << k)
            gates += len(cone["cells"])
        checked += 1
    assert checked >= 40, checked
    print("count vs brute force: OK (%d random cones, %d gate instances, "
          "4 independent evaluators, both targets)" % (checked, gates))


def check_support_against_brute_force():
    """`support` must agree with exhaustive difference-checking."""
    rng = random.Random(4242)
    checked = 0
    for _trial in range(40):
        instances, nets, ports = random_cone(rng, rng.randint(2, 5),
                                             rng.randint(1, 6))
        try:
            nl, cone, _g, _res = single_output_cone(instances, nets, ports)
        except AssertionError:
            continue
        formula = ConeFormula(nl, cone)
        k = formula.n_inputs()
        if k > 10:
            continue
        onset = hand_force_table(nl, cone, formula, 1)
        expected = []
        for i in range(k):
            relevant = False
            for bits in itertools.product((0, 1), repeat=k):
                flipped = list(bits)
                flipped[i] ^= 1
                if (bits in onset) != (tuple(flipped) in onset):
                    relevant = True
                    break
            expected.append(relevant)
        got = [r["relevant"] for r in run_support(z3, formula, Opts())["rows"]]
        assert got == expected, (got, expected)
        checked += 1
    assert checked >= 25, checked
    print("support vs brute force: OK (%d random cones)" % checked)


# ------------------------------------------------- a two-output combinational block

# A 2-bit datapath slice with two outputs, an enable and a dead spare input.
# A = (A1, A0), B = (B1, B0):
#
#   SUM1 = bit 1 of A + B   = A1 ^ B1 ^ (A0 & B0)
#   GE   = EN & (A >= B)    = EN & ( (A1 & ~B1) | ((A1 == B1) & (A0 | ~B0)) )
#
# The two outputs are two cones, and they share the gate computing A1 ^ B1 --
# the adder uses it as a partial sum, the comparator inverts it to test A1 == B1.
# That is the point of the fixture: the cones are not independent, so "drive
# both high" is a different question from "drive each high", and running the
# tool twice cannot answer it. Both are reachable together (A = B = 3 is one
# witness) but only on a strict subset of what either allows alone.
#
# SPARE reaches the SUM1 cone through a path that is always 0 (A0 & ~A0), so it
# is structurally present and functionally irrelevant -- support has to say so.
#
# The reference functions are written from the specification above, not read off
# the gate list, so the gate list has to be right for the test to pass.

def spec_sum1(a1, a0, b1, b0):
    return a1 ^ b1 ^ (a0 & b0)


def spec_ge(en, a1, a0, b1, b0):
    return en & ((a1 & (1 - b1)) | ((a1 == b1) & (a0 | (1 - b0))))


SUM1_NET = 100
GE_NET = 101


def two_output_block():
    """-> (netlist, graph, decomposition, SUM1 cone, GE cone).

    Fifteen gates, deliberately mixing polarities: the comparator's result is
    computed inverted by an a21oi and un-inverted by the nor2 that applies EN,
    so a polarity slip anywhere along that chain changes the answer.
    """
    instances = [
        ("u0", HD + "and2_1"),      # c0   = A0 & B0
        ("u1", HD + "xor2_1"),      # t1   = A1 ^ B1          (shared by both)
        ("u2", HD + "xor2_1"),      # sraw = t1 ^ c0
        ("u3", HD + "inv_1"),       # eq1  = ~t1              (A1 == B1)
        ("u4", HD + "inv_1"),       # nb1  = ~B1
        ("u5", HD + "and2_1"),      # gt1  = A1 & ~B1
        ("u6", HD + "inv_1"),       # nb0  = ~B0
        ("u7", HD + "or2_1"),       # ge0  = A0 | ~B0
        ("u8", HD + "a21oi_1"),     # nGE  = ~((eq1 & ge0) | gt1)
        ("u9", HD + "nor2_1"),      # GE   = ~(nGE | nEN)
        ("u10", HD + "inv_1"),      # nEN  = ~EN
        ("u11", HD + "inv_1"),      # na0  = ~A0
        ("u12", HD + "and2_1"),     # zero = A0 & ~A0         = 0
        ("u13", HD + "and2_1"),     # z    = SPARE & zero     = 0
        ("u14", HD + "or2_1"),      # SUM1 = sraw | z         = sraw
        ("dS", HD + "dfxtp_1"), ("dC", HD + "dfxtp_1"),
    ]
    nets = [
        (0, [("u0", "A", "input"), ("u7", "A", "input"),
             ("u11", "A", "input"), ("u12", "A", "input")]),          # A0
        (1, [("u1", "A", "input"), ("u5", "A", "input")]),            # A1
        (2, [("u0", "B", "input"), ("u6", "A", "input")]),            # B0
        (3, [("u1", "B", "input"), ("u4", "A", "input")]),            # B1
        (4, [("u10", "A", "input")]),                                 # EN
        (5, [("u13", "A", "input")]),                                 # SPARE
        (10, [("u1", "X", "output"), ("u2", "A", "input"),
              ("u3", "A", "input")]),                                 # t1, shared
        (11, [("u0", "X", "output"), ("u2", "B", "input")]),           # c0
        (12, [("u2", "X", "output"), ("u14", "A", "input")]),          # sraw
        (13, [("u3", "Y", "output"), ("u8", "A1", "input")]),          # eq1
        (14, [("u4", "Y", "output"), ("u5", "B", "input")]),           # nb1
        (15, [("u5", "X", "output"), ("u8", "B1", "input")]),          # gt1
        (16, [("u6", "Y", "output"), ("u7", "B", "input")]),           # nb0
        (17, [("u7", "X", "output"), ("u8", "A2", "input")]),          # ge0
        (18, [("u8", "Y", "output"), ("u9", "A", "input")]),           # nGE
        (19, [("u10", "Y", "output"), ("u9", "B", "input")]),          # nEN
        (20, [("u11", "Y", "output"), ("u12", "B", "input")]),         # na0
        (21, [("u12", "X", "output"), ("u13", "B", "input")]),         # zero
        (22, [("u13", "X", "output"), ("u14", "B", "input")]),         # z
        (SUM1_NET, [("u14", "X", "output"), ("dS", "D", "input")]),
        (GE_NET, [("u9", "Y", "output"), ("dC", "D", "input")]),
        (CLK_NET, [("dS", "CLK", "input"), ("dC", "CLK", "input")]),
    ]
    ports = [("A0", "input", 0), ("A1", "input", 1), ("B0", "input", 2),
             ("B1", "input", 3), ("EN", "input", 4), ("SPARE", "input", 5),
             ("CLK", "input", CLK_NET)]
    g = make_graph(instances=instances, nets=nets, ports=ports)
    res = decompose(g, [])
    by_root = {c["root_net"]: c for c in res["cones"]}
    assert set(by_root) == {SUM1_NET, GE_NET}, sorted(by_root)
    return Netlist(g, []), g, res, by_root[SUM1_NET], by_root[GE_NET]


def labelled(formula, rows):
    """Model rows as sorted (label, bit) tuples, so two cones over different
    boundaries can still be compared by signal name. Rows carrying '-' for an
    ignored input are expanded first."""
    labels = formula.input_labels()
    return {tuple(sorted(zip(labels, row))) for row in expand(rows)}


def spec_models(labels, fn):
    """Every assignment over `labels` that `fn` accepts, in `labelled` form."""
    out = set()
    for bits in itertools.product((0, 1), repeat=len(labels)):
        env = dict(zip(labels, bits))
        if fn(env):
            out.add(tuple(sorted(env.items())))
    return out


def check_two_output_block():
    """Each output on its own, then both at once over the shared boundary."""
    nl, g, res, sum_cone, ge_cone = two_output_block()
    assert (sum_cone["id"], ge_cone["id"]) == (0, 1), (sum_cone, ge_cone)
    # the A1 ^ B1 gate really is in both cones -- that is what makes the joint
    # question different from two separate ones
    assert "u1" in sum_cone["cells"] and "u1" in ge_cone["cells"], \
        (sum_cone["cells"], ge_cone["cells"])
    assert len(sum_cone["cells"]) == 7, sum_cone["cells"]
    assert len(ge_cone["cells"]) == 9, ge_cone["cells"]

    f_sum = ConeFormula(nl, sum_cone)
    f_ge = ConeFormula(nl, ge_cone)
    assert sorted(f_sum.input_labels()) == ["A0", "A1", "B0", "B1", "SPARE"], \
        f_sum.input_labels()
    assert sorted(f_ge.input_labels()) == ["A0", "A1", "B0", "B1", "EN"], \
        f_ge.input_labels()

    # --- each output on its own, against the specification -------------------
    sum_alone = labelled(f_sum, z3_table(nl, sum_cone, 1)[1])
    want = spec_models(f_sum.input_labels(),
                       lambda e: spec_sum1(e["A1"], e["A0"], e["B1"], e["B0"]))
    assert sum_alone == want, sorted(sum_alone ^ want)
    assert len(sum_alone) == 16, len(sum_alone)      # 8 of 16 (A,B), SPARE free

    ge_alone = labelled(f_ge, z3_table(nl, ge_cone, 1)[1])
    want = spec_models(f_ge.input_labels(),
                       lambda e: spec_ge(e["EN"], e["A1"], e["A0"],
                                         e["B1"], e["B0"]))
    assert ge_alone == want, sorted(ge_alone ^ want)
    # A >= B holds for 10 of the 16 ordered 2-bit pairs, and EN must be on
    assert len(ge_alone) == 10, len(ge_alone)

    # --- both at once --------------------------------------------------------
    joint = JointFormula(nl, [(sum_cone, 1), (ge_cone, 1)])
    assert sorted(joint.input_labels()) == ["A0", "A1", "B0", "B1", "EN",
                                            "SPARE"], joint.input_labels()
    r = run_models(z3, joint, 1, Opts(limit=1 << 10))
    assert not r["truncated"], r
    got = labelled(joint, r["rows"])
    want = spec_models(joint.input_labels(),
                       lambda e: spec_sum1(e["A1"], e["A0"], e["B1"], e["B0"])
                       and spec_ge(e["EN"], e["A1"], e["A0"], e["B1"], e["B0"]))
    assert got == want, sorted(got ^ want)
    # 5 of the 16 (A, B) pairs satisfy both; EN must be on, SPARE is free
    assert len(got) == 10, len(got)

    # A = B = 3 is the obvious witness: 3 + 3 = 6 = 110 so bit 1 is set, and
    # A >= B holds. It must appear, with EN on and SPARE either way.
    witness = [dict(m) for m in got
               if all(dict(m)[k] == 1 for k in ("A0", "A1", "B0", "B1", "EN"))]
    assert len(witness) == 2, witness

    # --- the joint answer is strictly stronger than two separate runs --------
    def project(model, formula):
        e = dict(model)
        return tuple(sorted((k, e[k]) for k in formula.input_labels()))

    for m in got:
        assert project(m, f_sum) in sum_alone, m
        assert project(m, f_ge) in ge_alone, m

    # A = 0, B = 2 drives SUM1 high but leaves A >= B false, so it satisfies one
    # output and not both: exactly the case two separate runs would miss.
    a0b2 = {"A0": 0, "A1": 0, "B0": 0, "B1": 1, "EN": 1, "SPARE": 0}
    assert spec_sum1(0, 0, 1, 0) == 1 and spec_ge(1, 0, 0, 1, 0) == 0
    assert tuple(sorted((k, a0b2[k]) for k in f_sum.input_labels())) in sum_alone
    assert tuple(sorted(a0b2.items())) not in got

    # --- support over the joint goal ----------------------------------------
    sup = run_support(z3, joint, Opts())
    by_input = {row["input"]: row["relevant"] for row in sup["rows"]}
    assert by_input["SPARE"] is False, sup["rows"]
    assert all(by_input[k] for k in ("A0", "A1", "B0", "B1", "EN")), sup["rows"]
    assert sup["n_relevant"] == 5, sup
    # SPARE is genuinely wired into the SUM1 cone -- it is a leaf of it
    assert any(l.get("name") == "SPARE" for l in sum_cone["leaves"]), \
        sum_cone["leaves"]
    # EN, by contrast, is irrelevant to SUM1 because it is not in that cone
    assert "EN" not in f_sum.input_labels(), f_sum.input_labels()

    # --- implicants of the joint goal are sound, prime and covering ----------
    r = run_implicants(z3, joint, 1, Opts())
    k = joint.n_inputs()
    onset = {tuple(dict(m)[lbl] for lbl in joint.input_labels()) for m in want}
    covered = set()
    for cube in r["rows"]:
        pts = cube_points(cube, k)
        assert pts <= onset, (cube, sorted(pts - onset))
        for i, b in enumerate(cube):
            if b is None:
                continue
            wider = list(cube)
            wider[i] = None
            assert not (cube_points(wider, k) <= onset), ("not prime", cube, i)
        covered |= pts
    assert covered == onset, sorted(onset - covered)
    # every implicant must leave SPARE free, since nothing depends on it
    spare_at = joint.input_labels().index("SPARE")
    assert all(cube[spare_at] is None for cube in r["rows"]), r["rows"]

    # --- contradictory targets are UNSAT, and say why -----------------------
    assert run_count(z3, JointFormula(nl, [(sum_cone, 1), (sum_cone, 0)]),
                     1, Opts())["count"] == 0
    code, out, _err = run_cli(g, res, ["CONE0", "1", "--also", "CONE0=0"])
    assert code == decomposeCone.EXIT_UNSAT, (code, out)
    assert "not simultaneously reachable" in out, out

    # --- and the CLI answers the joint question end to end ------------------
    code, out, _err = run_cli(g, res, ["CONE0", "1", "--also", "CONE1=1"])
    assert code == 0, (code, out)
    assert "cone 0 (CONE0)" in out and "cone 1 (CONE1)" in out, out
    assert "all 2 cones at their target together" in out, out

    code, out, _err = run_cli(g, res, ["CONE0", "1", "--also", "CONE1=1",
                                       "--mode", "count"])
    assert code == 0 and "count 10" in out, out

    code, out, _err = run_cli(g, res, ["CONE0", "1", "--also", "CONE1=1",
                                       "--mode", "support"])
    assert code == 0 and "5 of 6 inputs in functional support" in out, out
    assert "SPARE" in out and "NO (irrelevant)" in out, out

    # A mixed goal: SUM1 high while GE is low. Over the joint 6-input boundary
    # SUM1 alone holds in 32 assignments (its 16 models, times the free EN it
    # does not see); 10 of those also satisfy GE, so 22 remain. A goal-polarity
    # slip on the --also target would not land on 22.
    code, out, _err = run_cli(g, res, ["CONE0", "1", "--also", "CONE1=0",
                                       "--mode", "count"])
    assert code == 0 and "count 22" in out, out

    for bad in ("CONE1", "CONE1=2", "CONE1=yes"):
        code, _out, err = run_cli(g, res, ["CONE0", "1", "--also", bad])
        assert code == decomposeCone.EXIT_ERROR, (bad, code)
        assert "Traceback" not in err, err

    print("two-output block: OK (15 gates, shared A1^B1, %d/%d/%d models for "
          "SUM1 / GE / both, SPARE irrelevant)"
          % (len(sum_alone), len(ge_alone), len(got)))


# ------------------------------------------------------- a full ALU datapath fixture

class Builder(object):
    """Accumulates instances/nets/ports and hands them to make_graph.

    Purely a convenience for writing large fixtures by hand; the graph it
    produces is exactly what make_graph produces, and stage 4 decomposes it
    like any other.
    """

    def __init__(self):
        self.instances = []
        self.eps = {}
        self.ports = []
        self._net = 0
        self._inst = 0

    def net(self):
        self._net += 1
        self.eps.setdefault(self._net, [])
        return self._net

    def port_in(self, name):
        n = self.net()
        self.ports.append((name, "input", n))
        return n

    def port_out(self, name, net):
        self.ports.append((name, "output", net))
        return net

    def gate(self, cell, wiring, out_pin):
        """wiring: {input pin: net}. -> the output net."""
        inst = "u%d" % self._inst
        self._inst += 1
        self.instances.append((inst, cell))
        for pin, n in sorted(wiring.items()):
            self.eps[n].append((inst, pin, "input"))
        out = self.net()
        self.eps[out].append((inst, out_pin, "output"))
        return out

    def flop(self, dnet, clk):
        inst = "r%d" % self._inst
        self._inst += 1
        self.instances.append((inst, HD + "dfxtp_1"))
        self.eps[dnet].append((inst, "D", "input"))
        self.eps[clk].append((inst, "CLK", "input"))
        out = self.net()
        self.eps[out].append((inst, "Q", "output"))
        return out

    def build(self):
        nets = [(n, eps) for n, eps in sorted(self.eps.items()) if eps]
        return make_graph(instances=self.instances, nets=nets, ports=self.ports)


def _INV(b, a):
    return b.gate(HD + "inv_1", {"A": a}, "Y")


def _AND(b, x, y):
    return b.gate(HD + "and2_1", {"A": x, "B": y}, "X")


def _OR(b, x, y):
    return b.gate(HD + "or2_1", {"A": x, "B": y}, "X")


def _XOR(b, x, y):
    return b.gate(HD + "xor2_1", {"A": x, "B": y}, "X")


def _XNOR(b, x, y):
    return b.gate(HD + "xnor2_1", {"A": x, "B": y}, "Y")


def _NOR(b, x, y):
    return b.gate(HD + "nor2_1", {"A": x, "B": y}, "Y")


def _NAND(b, x, y):
    return b.gate(HD + "nand2_1", {"A": x, "B": y}, "Y")


def _MAJ(b, x, y, z):
    return b.gate(HD + "maj3_1", {"A": x, "B": y, "C": z}, "X")


def _MUX4(b, a0, a1, a2, a3, s0, s1):
    return b.gate(HD + "mux4_1", {"A0": a0, "A1": a1, "A2": a2, "A3": a3,
                                  "S0": s0, "S1": s1}, "X")


def _tree(b, op, nets):
    """Balanced reduction, so the cone is deep as well as wide."""
    cur = list(nets)
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur) - 1, 2):
            nxt.append(op(b, cur[i], cur[i + 1]))
        if len(cur) % 2:
            nxt.append(cur[-1])
        cur = nxt
    return cur[0]


def alu_design(width):
    """A `width`-bit ALU datapath: ripple-carry add, bitwise ops, a 4:1 result
    mux, plus zero / parity / carry-out / A>=B flags, all registered.

    Every flag reduces the whole result word, so its cone spans the entire
    datapath: those are the wide, deep, heavily reconvergent cones that make
    this a stress test rather than a toy. SPARE is wired in through an
    always-zero path, exactly as in the two-output block.

    -> (graph, decomposition, {name: cone}, port names)
    """
    b = Builder()
    clk = b.port_in("CLK")
    A = [b.port_in("A%d" % i) for i in range(width)]
    B = [b.port_in("B%d" % i) for i in range(width)]
    cin = b.port_in("CIN")
    s0 = b.port_in("SEL0")
    s1 = b.port_in("SEL1")
    spare = b.port_in("SPARE")

    # ripple-carry adder: the carry chain is what makes these cones deep
    carry = cin
    sums, carries = [], []
    for i in range(width):
        half = _XOR(b, A[i], B[i])
        sums.append(_XOR(b, half, carry))
        carry = _MAJ(b, A[i], B[i], carry)
        carries.append(carry)

    # bitwise ops and the 4:1 result mux
    result = []
    for i in range(width):
        result.append(_MUX4(b, sums[i], _AND(b, A[i], B[i]),
                            _OR(b, A[i], B[i]), _XOR(b, A[i], B[i]), s0, s1))

    # SPARE joins bit 0 through a path that is always 0
    zero_path = _AND(b, A[0], _INV(b, A[0]))
    result[0] = _OR(b, result[0], _AND(b, spare, zero_path))

    # flags, each reducing the whole word
    zero = _INV(b, _tree(b, _OR, result))
    parity = _tree(b, _XOR, result)
    cout = carries[-1]

    # A >= B, built from the top down out of equality and greater-than terms
    ge = None
    for i in range(width):
        gt = _AND(b, A[i], _INV(b, B[i]))
        eq = _XNOR(b, A[i], B[i])
        ge = gt if ge is None else _OR(b, gt, _AND(b, eq, ge))
    ge = _OR(b, ge, _tree(b, _AND, [_XNOR(b, A[i], B[i])
                                    for i in range(width)]))

    named = {}
    for i, net in enumerate(result):
        b.flop(net, clk)
        named["R%d" % i] = net
    for name, net in (("ZERO", zero), ("PARITY", parity), ("COUT", cout),
                      ("GE", ge)):
        b.flop(net, clk)
        named[name] = net

    g = b.build()
    res = decompose(g, [])
    by_root = {c["root_net"]: c for c in res["cones"]}
    cones = {name: by_root[net] for name, net in named.items()}
    return g, res, cones, {"A": A, "B": B}


def alu_spec(width, env, name):
    """The reference value of one flag/result bit, from the specification."""
    a = [env["A%d" % i] for i in range(width)]
    bb = [env["B%d" % i] for i in range(width)]
    sel = env["SEL0"] + 2 * env["SEL1"]
    carry = env["CIN"]
    sums, cout = [], env["CIN"]
    for i in range(width):
        sums.append(a[i] ^ bb[i] ^ carry)
        carry = 1 if (a[i] + bb[i] + carry) >= 2 else 0
        cout = carry
    ops = [sums,
           [a[i] & bb[i] for i in range(width)],
           [a[i] | bb[i] for i in range(width)],
           [a[i] ^ bb[i] for i in range(width)]]
    result = list(ops[sel])
    if name.startswith("R"):
        return result[int(name[1:])]
    if name == "COUT":
        return cout
    if name == "ZERO":
        return 1 - (1 if any(result) else 0)
    if name == "PARITY":
        p = 0
        for v in result:
            p ^= v
        return p
    if name == "GE":
        av = sum(v << i for i, v in enumerate(a))
        bv = sum(v << i for i, v in enumerate(bb))
        return 1 if av >= bv else 0
    raise AssertionError(name)


def check_alu_datapath():
    """Every cone of a real datapath, against the ALU specification.

    For each cone the whole design input space is enumerated, the specification
    evaluated, and the result compared with what the tool says about that cone
    over its *own* boundary. That checks two things at once: the cone computes
    the right function, and its boundary is sufficient to determine it -- a cone
    missing an input it actually needs would show up as the same projection
    demanding two different answers.
    """
    width = 3
    g, res, cones, _p = alu_design(width)
    nl = Netlist(g, [])
    ports = (["A%d" % i for i in range(width)] + ["B%d" % i for i in range(width)]
             + ["CIN", "SEL0", "SEL1", "SPARE"])
    space = list(itertools.product((0, 1), repeat=len(ports)))

    checked = 0
    for name in sorted(cones):
        cone = cones[name]
        f = ConeFormula(nl, cone)
        labels = f.input_labels()
        assert set(labels) <= set(ports), (name, labels)
        _f, got = z3_table(nl, cone, 1)
        for bits in space:
            env = dict(zip(ports, bits))
            expected = alu_spec(width, env, name)
            proj = tuple(env[l] for l in labels)
            assert (proj in got) == bool(expected), (name, env, expected)
        checked += 1
    assert checked == width + 4, checked          # result bits + 4 flags

    # SPARE is wired into bit 0 and therefore into every flag that reduces the
    # word, and is functionally dead in all of them
    for name in ("R0", "ZERO", "PARITY"):
        f = ConeFormula(nl, cones[name])
        assert "SPARE" in f.input_labels(), (name, f.input_labels())
        sup = run_support(z3, f, Opts())
        by_input = {r["input"]: r["relevant"] for r in sup["rows"]}
        assert by_input["SPARE"] is False, (name, sup["rows"])
        assert all(v for k, v in by_input.items() if k != "SPARE"), \
            (name, sup["rows"])

    # the carry chain really is deep: the top carry depends on every input bit
    f = ConeFormula(nl, cones["COUT"])
    assert set(f.input_labels()) == set(["CIN"] + ["A%d" % i for i in range(width)]
                                        + ["B%d" % i for i in range(width)]), \
        f.input_labels()
    assert run_support(z3, f, Opts())["n_relevant"] == 2 * width + 1

    # and an implicant cover of a flag is sound, prime and covering
    for name in ("COUT", "GE"):
        f = ConeFormula(nl, cones[name])
        labels = f.input_labels()
        _f, onset = z3_table(nl, cones[name], 1)
        r = run_implicants(z3, f, 1, Opts(limit=10000))
        assert not r["truncated"], r["stopped"]
        covered = set()
        for cube in r["rows"]:
            pts = cube_points(cube, len(labels))
            assert pts <= onset, (name, cube)
            for i, b in enumerate(cube):
                if b is None:
                    continue
                wider = list(cube)
                wider[i] = None
                assert not (cube_points(wider, len(labels)) <= onset), \
                    ("not prime", name, cube, i)
            covered |= pts
        assert covered == onset, (name, len(onset - covered))

    print("ALU datapath: OK (width %d, %d instances, %d cones, every cone "
          "against the specification over the whole input space)"
          % (width, len(g["instances"]), len(cones)))


# ------------------------------------------------------------------- limits/CLI

def check_limit_and_truncation():
    """--limit caps the output and the truncation is reported, not hidden."""
    instances, nets, ports = gate_cone(HD + "nand3_1",
                                       {"A": 0, "B": 1, "C": 2}, "Y")
    nl, cone, g, res_dec = single_output_cone(instances, nets, ports)
    formula = ConeFormula(nl, cone)

    r = run_models(z3, formula, 1, Opts(limit=3))
    assert len(r["rows"]) == 3 and r["truncated"], r

    r = run_models(z3, formula, 1, Opts(limit=7))      # exactly the whole on-set
    assert len(r["rows"]) == 7 and not r["truncated"], r

    r = run_count(z3, formula, 1, Opts(limit=4))
    assert (r["count"], r["exact"], r["stopped"]) == (4, False, "limit"), r
    r = run_count(z3, formula, 1, Opts(limit=100))
    assert r["count"] == 7 and r["exact"], r

    code, out, _err = run_cli(g, res_dec,
                              [str(cone["id"]), "1", "--mode", "models",
                               "--limit", "2"])
    assert code == 0, (code, out)
    assert "PARTIAL" in out and "--limit 2" in out, out

    # a spent --budget is reported as partial too, and never as "no solutions"
    code, out, _err = run_cli(g, res_dec,
                              [str(cone["id"]), "1", "--mode", "count",
                               "--budget", "0"])
    assert code in (0, decomposeCone.EXIT_UNKNOWN), (code, out)
    assert "UNSAT" not in out, out
    print("limits: OK (capped, partial runs reported with their reason, "
          "exact when it fits)")


def check_cone_id_parsing():
    assert parse_cone_id("CONE7") == 7
    assert parse_cone_id("cone7") == 7
    assert parse_cone_id("7") == 7
    assert parse_cone_id(" #7 ") == 7
    for bad in ("CONE", "seven", "CONE7a", ""):
        try:
            parse_cone_id(bad)
        except ConeError as e:
            assert "integer id" in str(e), e
        else:
            raise AssertionError("accepted %r as a cone id" % bad)

    fake = {"cones": [{"id": 0, "root_net": 40}, {"id": 1, "root_net": 77}]}
    assert find_cone(fake, cone_id=1)["root_net"] == 77
    assert find_cone(fake, root_net=40)["id"] == 0
    for kw in ({"cone_id": 9}, {"root_net": 1234}):
        try:
            find_cone(fake, **kw)
        except ConeError:
            pass
        else:
            raise AssertionError("found a cone that does not exist: %r" % kw)
    print("cone ids: OK (CONE7 == cone7 == 7, --by-root, clear errors)")


def run_cli(graph, cones_data, args):
    """Drive main() end to end on a fixture, capturing stdout/stderr."""
    import io
    import json as _json
    import tempfile

    d = tempfile.mkdtemp()
    gp = os.path.join(d, "g.json")
    cp = os.path.join(d, "c.json")
    with open(gp, "w") as f:
        _json.dump(graph, f)
    with open(cp, "w") as f:
        _json.dump(cones_data, f)

    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = decomposeCone.main(args + ["--graph", gp, "--cones", cp])
    finally:
        sys.stdout, sys.stderr = old
    return code, out.getvalue(), err.getvalue()


def chain_fixture():
    """IN0,IN1 -> nand -> f1 ; f1.Q & IN2 -> and -> f2 ; f2.Q | b0.P0 -> or -> f3.

    A chain of cones with one of every boundary kind that can name a source and
    one of each that cannot, so the origin report is exercised end to end.
    """
    g = make_graph(
        instances=[("u0", HD + "nand2_1"), ("f1", HD + "dfxtp_1"),
                   ("u1", HD + "and2_1"), ("f2", HD + "dfxtp_1"),
                   ("b0", "MYBLACKBOX"), ("u2", HD + "or2_1"),
                   ("f3", HD + "dfxtp_1")],
        nets=[(0, [("u0", "A", "input")]), (1, [("u0", "B", "input")]),
              (2, [("u0", "Y", "output"), ("f1", "D", "input")]),
              (3, [("f1", "CLK", "input"), ("f2", "CLK", "input"),
                   ("f3", "CLK", "input")]),
              (4, [("f1", "Q", "output"), ("u1", "A", "input")]),
              (5, [("u1", "B", "input")]),
              (6, [("u1", "X", "output"), ("f2", "D", "input")]),
              (7, [("f2", "Q", "output"), ("u2", "A", "input")]),
              (8, [("b0", "P0", "output"), ("u2", "B", "input")]),
              (9, [("u2", "X", "output"), ("f3", "D", "input")]),
              (10, [("f3", "Q", "output")])],
        ports=[("IN0", "input", 0), ("IN1", "input", 1), ("clk", "input", 3),
               ("IN2", "input", 5), ("OUT", "output", 10)])
    return g, decompose(g, [])


def check_input_origins():
    """Each boundary input is reported with the cone it comes from.

    A register output names the cone feeding that register's data pin; a primary
    input, a black box and an undriven net say what they are instead, because
    nothing upstream of them is a cone.
    """
    g, cones = chain_fixture()

    def report(cone_id):
        opts = decomposeCone.build_parser().parse_args([str(cone_id), "1"])
        res, _f, _goals = decomposeCone.analyse(g, cones, opts, [])
        return dict(zip(res["inputs"], res["input_origins"])), res

    # cone 1 is fed by cone 0 (through f1) and by a primary input
    origins, res = report(1)
    assert origins == {"f1.Q": "CONE0", "IN2": "primary input IN2"}, origins
    assert res["input_cones"] == [0, None], res["input_cones"]

    # cone 2 is fed by cone 1 (through f2) and by a black box, which is not a cone
    origins, res = report(2)
    assert origins == {"f2.Q": "CONE1", "b0.P0": "black box b0"}, origins
    assert res["input_cones"] == [1, None], res["input_cones"]

    # cone 0 sits at the front: both its inputs are primary
    origins, _res = report(0)
    assert origins == {"IN0": "primary input IN0",
                       "IN1": "primary input IN1"}, origins
    print("input origins: OK (register leaves name their cone; ports and black "
          "boxes say what they are)")


def check_input_origins_survive_a_stale_cones_file():
    """A CONES.json written before `from_cone` existed still reports origins:
    the tag is recomputed with stage 4's own function rather than demanded."""
    import copy
    g, cones = chain_fixture()
    stale = copy.deepcopy(cones)
    for c in stale["cones"]:
        for leaf in c["leaves"]:
            leaf.pop("from_cone")
    assert not any("from_cone" in l for c in stale["cones"] for l in c["leaves"])

    warnings = []
    assert decomposeCone.ensure_leaf_origins(stale, warnings) is True
    assert decomposeCone.ensure_leaf_origins(stale, warnings) is False, \
        "a file that already has the tag must not be rewritten"

    opts = decomposeCone.build_parser().parse_args(["2", "1"])
    res, _f, _goals = decomposeCone.analyse(g, copy.deepcopy(stale), opts, [])
    assert dict(zip(res["inputs"], res["input_origins"])) == {
        "f2.Q": "CONE1", "b0.P0": "black box b0"}, res["input_origins"]
    print("stale CONES.json: OK (origins recomputed, no regeneration needed)")


def check_origins_in_report_and_json():
    """The origin shows in the human header and in --json, and a joint --also
    query reports the merged boundary's origins too."""
    import json as _json
    g, cones = chain_fixture()

    code, out, _err = run_cli(g, cones, ["2", "1"])
    assert code == 0, (code, out)
    assert "f2.Q" in out and "<-  CONE1" in out, out
    assert "b0.P0" in out and "<-  black box b0" in out, out
    header = out.split("implicant")[0]
    assert "CONE1" in header, header          # above the results, not after them

    code, out, _err = run_cli(g, cones, ["2", "1", "--json"])
    assert code == 0, (code, out)
    doc = _json.loads(out)
    assert doc["inputs"] == ["f2.Q", "b0.P0"], doc["inputs"]
    assert doc["input_origins"] == ["CONE1", "black box b0"], doc["input_origins"]
    assert doc["input_cones"] == [1, None], doc["input_cones"]

    # --also merges two boundaries; every input still names its source
    code, out, _err = run_cli(g, cones, ["1", "1", "--also", "CONE2=1", "--json"])
    assert code == 0, (code, out)
    doc = _json.loads(out)
    origins = dict(zip(doc["inputs"], doc["input_origins"]))
    assert origins == {"f1.Q": "CONE0", "IN2": "primary input IN2",
                       "f2.Q": "CONE1", "b0.P0": "black box b0"}, origins
    print("origin reporting: OK (human header, --json, and a joint --also query)")


def check_cli_output_shape():
    """The human report is greppable and the JSON is machine-readable."""
    import json as _json
    instances, nets, ports = gate_cone(HD + "o21ai_1",
                                       {"A1": 0, "A2": 1, "B1": 2}, "Y")
    _nl, cone, g, res_dec = single_output_cone(instances, nets, ports)
    cid = str(cone["id"])

    code, out, _err = run_cli(g, res_dec, [cid, "1"])
    assert code == 0, (code, out)
    assert "cone %s (CONE%s)" % (cid, cid) in out, out
    assert "target 1" in out, out
    for label in ("I0", "I1", "I2"):
        assert "input" in out and label in out, out
    assert out.count("implicant ") >= 1, out
    assert "summary:" in out, out

    code, out, _err = run_cli(g, res_dec, [cid, "1", "--mode", "support"])
    assert code == 0 and "3 of 3 inputs in functional support" in out, out

    code, out, _err = run_cli(g, res_dec, [cid, "1", "--mode", "count"])
    assert code == 0 and "count 5" in out, out

    code, out, _err = run_cli(g, res_dec, ["CONE" + cid, "1", "--json"])
    assert code == 0, (code, out)
    doc = _json.loads(out)
    assert doc["cone"] == cone["id"], doc
    assert doc["module"] == "CONE%d" % cone["id"], doc
    assert doc["inputs"] == ["I0", "I1", "I2"], doc
    assert doc["mode"] == "implicants" and doc["target"] == 1, doc
    assert doc["status"] == "sat" and doc["n_inputs"] == 3, doc

    code, out, err = run_cli(g, res_dec, ["CONE999", "1"])
    assert code == decomposeCone.EXIT_ERROR, (code, out, err)
    assert "no cone with id 999" in err, err
    assert "Traceback" not in err, err
    print("CLI: OK (human report greppable, JSON complete, errors are messages)")


def stress(width, budget):
    """Not part of the suite: the scale run, for when a change might have cost
    something. `python3 tests/testDecomposeCone.py --stress 12 15`"""
    import time
    g, res, cones, _p = alu_design(width)
    nl = Netlist(g, [])
    print("ALU width %d: %d instances, %d nets, %d cones"
          % (width, len(g["instances"]), len(g["nets"]), len(res["cones"])))
    names = ["R0", "R%d" % (width - 1), "COUT", "GE", "PARITY", "ZERO"]
    for name in names:
        f = ConeFormula(nl, cones[name])
        opts = Opts(limit=1000000, timeout=60, budget=budget)
        t = time.time()
        r = run_implicants(z3, f, 1, opts)
        print("  %-7s inputs=%2d cells=%3d  cubes=%7d %-10s avg_lits=%5.2f  "
              "%6.2fs %7d calls"
              % (name, f.n_inputs(), len(cones[name]["cells"]), len(r["rows"]),
                 "(%s)" % (r["stopped"] or "complete"), r["avg_literals"],
                 time.time() - t, r["stats"]["solver_calls"]))
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--stress":
        return stress(int(sys.argv[2]) if len(sys.argv) > 2 else 8,
                      float(sys.argv[3]) if len(sys.argv) > 3 else 15.0)
    check_single_gates_against_hand_tables()
    check_deep_nested_cone()
    check_implicants_are_prime_and_cover()
    check_implicant_dont_cares()
    check_irrelevant_input_is_caught()
    check_constant_cone_is_unsat()
    check_tied_and_unconnected_pins()
    check_const_net_folds()
    check_unmodelled_cell_raises()
    check_register_boundary()
    check_two_output_block()
    check_alu_datapath()
    check_count_against_brute_force()
    check_support_against_brute_force()
    check_limit_and_truncation()
    check_cone_id_parsing()
    check_input_origins()
    check_input_origins_survive_a_stale_cones_file()
    check_origins_in_report_and_json()
    check_cli_output_shape()
    print("\nall decomposeCone tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
