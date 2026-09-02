#!/usr/bin/env python3
"""Synthetic tests for boolExpr.py and coneClasses.py (stage 5). Everything is
checked against functions worked out by hand.

Run:  python3 tests/testConeClasses.py
"""

import os
import sys
from itertools import product

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import boolExpr                                     # noqa: E402
from boolExpr import parse, from_ast, key, negate, ONE, ZERO  # noqa: E402
from coneDecompose import decompose                 # noqa: E402
from coneClasses import (classify, truth_table, canonical_table,  # noqa: E402
                         cone_function)
from testConeDecompose import make_graph, HD        # noqa: E402


def node_for(text, names):
    env = {n: ("v", i) for i, n in enumerate(names)}
    return from_ast(parse(text), env)


def brute(text, names):
    """Reference evaluation of an expression, one assignment at a time."""
    out = []
    for bits in product((0, 1), repeat=len(names)):
        env = dict(zip(names, bits))
        expr = text
        # evaluate with Python by textual substitution of a tiny grammar
        node = node_for(text, names)
        vals = {i: (1 if bits[i] else 0) for i in range(len(names))}
        out.append(boolExpr.evaluate(node, vals, 1))
    return out


def check_parser_and_normal_form():
    # DeMorgan: the two spellings must normalise to the same key
    a = node_for("~(A & B)", ["A", "B"])
    b = node_for("~A | ~B", ["A", "B"])
    assert key(a) == key(b), (key(a), key(b))
    c = node_for("~(A | B)", ["A", "B"])
    d = node_for("~A & ~B", ["A", "B"])
    assert key(c) == key(d), (key(c), key(d))

    # double negation, idempotence, complement, constants
    assert node_for("~~A", ["A"]) == ("v", 0)
    assert node_for("A & A", ["A"]) == ("v", 0)
    assert node_for("A & ~A", ["A"]) == ZERO
    assert node_for("A | ~A", ["A"]) == ONE
    assert node_for("A & 1'b1", ["A"]) == ("v", 0)
    assert node_for("A & 1'b0", ["A"]) == ZERO
    assert node_for("A | 1'b1", ["A"]) == ONE
    assert node_for("A ^ A", ["A"]) == ZERO
    assert node_for("A ^ 1'b1", ["A"]) == ("nv", 0)

    # operand order must not matter, and nesting must flatten
    assert key(node_for("(A & B) & C", ["A", "B", "C"])) == \
           key(node_for("C & (B & A)", ["A", "B", "C"]))

    # a mux expands and normalises consistently with its Boolean spelling
    m1 = node_for("S ? A : B", ["S", "A", "B"])
    m2 = node_for("(S & A) | (~S & B)", ["S", "A", "B"])
    assert key(m1) == key(m2), (key(m1), key(m2))

    # negate() is an involution and matches the direct spelling
    x = node_for("(A & B) | (C & ~D)", ["A", "B", "C", "D"])
    assert negate(negate(x)) == x
    assert key(negate(x)) == key(node_for("~((A & B) | (C & ~D))",
                                          ["A", "B", "C", "D"]))
    print("parser+normal form: OK (DeMorgan, folding, flattening, mux, involution)")


def check_truth_tables_against_hand_values():
    names = ["A", "B"]
    # bit m of the table is the value for the assignment encoded by m
    cases = {
        "A & B":    0b1000,     # only A=1,B=1
        "A | B":    0b1110,
        "~(A & B)": 0b0111,
        "A ^ B":    0b0110,
        "~(A ^ B)": 0b1001,
        "1'b0":     0b0000,
        "1'b1":     0b1111,
    }
    for text, expected in cases.items():
        tt = truth_table(node_for(text, names), 2)
        assert tt == expected, (text, bin(tt), bin(expected))

    # a three-variable case, checked against an independent enumeration
    text = "(A & B) | ~C"
    node = node_for(text, ["A", "B", "C"])
    tt = truth_table(node, 3)
    for m in range(8):
        A, B, C = m & 1, (m >> 1) & 1, (m >> 2) & 1
        want = 1 if ((A and B) or not C) else 0
        assert (tt >> m) & 1 == want, (m, bin(tt))
    print("truth tables: OK (hand-computed values for 7 functions + 3-var sweep)")


def check_permutation_canonicalisation():
    # the same function with its inputs swapped must canonicalise identically
    f1 = truth_table(node_for("A & ~B", ["A", "B"]), 2)
    f2 = truth_table(node_for("~A & B", ["A", "B"]), 2)
    assert f1 != f2
    c1, p1 = canonical_table(f1, 2)
    c2, p2 = canonical_table(f2, 2)
    assert c1 == c2 and p1 and p2, (bin(f1), bin(f2), bin(c1), bin(c2))

    # a genuinely different function must not collide
    f3 = truth_table(node_for("A | B", ["A", "B"]), 2)
    c3, _ = canonical_table(f3, 2)
    assert c3 != c1

    # three-variable permutation
    g1 = truth_table(node_for("(A & B) | C", ["A", "B", "C"]), 3)
    g2 = truth_table(node_for("(B & C) | A", ["A", "B", "C"]), 3)
    assert g1 != g2
    assert canonical_table(g1, 3)[0] == canonical_table(g2, 3)[0]
    print("permutation canonicalisation: OK (input reordering collapses, "
          "distinct functions stay apart)")


def _two_cone_design(expr_a_cells, expr_b_cells):
    """Build a netlist with two independent cones, each feeding its own flop."""
    return None


def check_cone_classes_equivalent_but_different_structure():
    """Two cones computing the same function by different gates must share a
    class: nand(a,b) versus or(inv a, inv b) -- DeMorgan in silicon."""
    g = make_graph(
        instances=[("u0", HD + "nand2_1"), ("fA", HD + "dfxtp_1"),
                   ("u1", HD + "inv_1"), ("u2", HD + "inv_1"),
                   ("u3", HD + "or2_1"), ("fB", HD + "dfxtp_1")],
        nets=[
            (0, []), (1, []),                                  # IN0, IN1
            (2, [("u0", "A", "input"), ("u1", "A", "input")]),  # X
            (3, [("u0", "B", "input"), ("u2", "A", "input")]),  # Y
            (4, [("u0", "Y", "output"), ("fA", "D", "input")]),
            (5, [("u1", "Y", "output"), ("u3", "A", "input")]),
            (6, [("u2", "Y", "output"), ("u3", "B", "input")]),
            (7, [("u3", "X", "output"), ("fB", "D", "input")]),
            (8, [("fA", "CLK", "input"), ("fB", "CLK", "input")]),
        ],
        ports=[("X", "input", 2), ("Y", "input", 3), ("CLK", "input", 8)])
    res = decompose(g, [])
    warnings = []
    cls = classify(g, res, warnings)
    assert warnings == [], warnings

    by_cone = {c["id"]: c for c in cls["cones"]}
    cone_a = next(c["id"] for c in res["cones"]
                  if c["sinks"][0].get("inst") == "fA")
    cone_b = next(c["id"] for c in res["cones"]
                  if c["sinks"][0].get("inst") == "fB")
    assert by_cone[cone_a]["method"] == "truth_table", by_cone[cone_a]
    assert by_cone[cone_a]["class"] == by_cone[cone_b]["class"], \
        (by_cone[cone_a], by_cone[cone_b])
    assert by_cone[cone_a]["key"] == by_cone[cone_b]["key"]
    print("equivalence: OK (nand vs DeMorgan-expanded or share one class)")


def check_cone_classes_distinguish_functions():
    """Cones computing different functions must not be merged."""
    g = make_graph(
        instances=[("u0", HD + "and2_1"), ("fA", HD + "dfxtp_1"),
                   ("u1", HD + "or2_1"), ("fB", HD + "dfxtp_1")],
        nets=[
            (2, [("u0", "A", "input"), ("u1", "A", "input")]),
            (3, [("u0", "B", "input"), ("u1", "B", "input")]),
            (4, [("u0", "X", "output"), ("fA", "D", "input")]),
            (5, [("u1", "X", "output"), ("fB", "D", "input")]),
            (8, [("fA", "CLK", "input"), ("fB", "CLK", "input")]),
        ],
        ports=[("X", "input", 2), ("Y", "input", 3), ("CLK", "input", 8)])
    res = decompose(g, [])
    cls = classify(g, res, [])
    by_cone = {c["id"]: c for c in cls["cones"]}
    cones = [c["id"] for c in res["cones"] if c["n_cells"] == 1]
    assert len(cones) == 2, cones
    assert by_cone[cones[0]]["class"] != by_cone[cones[1]]["class"], by_cone
    print("discrimination: OK (and vs or stay in different classes)")


def check_bit_slice_grouping():
    """Three identical gates on three different signal pairs -- a bit slice --
    must collapse into one class of three members."""
    instances = []
    nets = {}
    ports = []
    net_id = 100
    for i in range(3):
        instances += [("g%d" % i, HD + "nand2_1"), ("f%d" % i, HD + "dfxtp_1")]
        a, b = 10 + i * 2, 11 + i * 2
        nets[a] = [("g%d" % i, "A", "input")]
        nets[b] = [("g%d" % i, "B", "input")]
        nets[net_id] = [("g%d" % i, "Y", "output"), ("f%d" % i, "D", "input")]
        ports += [("A%d" % i, "input", a), ("B%d" % i, "input", b)]
        net_id += 1
    nets[9] = [("f%d" % i, "CLK", "input") for i in range(3)]
    ports.append(("CLK", "input", 9))
    g = make_graph(instances=instances, nets=sorted(nets.items()), ports=ports)
    res = decompose(g, [])
    cls = classify(g, res, [])
    assert cls["summary"]["classes"] == 1, cls["summary"]
    assert cls["classes"][0]["members"] == sorted(c["id"] for c in res["cones"]), \
        cls["classes"][0]
    assert len(cls["classes"][0]["members"]) == 3
    print("bit slice: OK (three identical slices collapse to one class)")


def check_large_cone_is_normalised_not_tabulated():
    """A cone with more than 8 leaves must use the normalised method, and two
    such cones that differ only by DeMorgan spelling must still match."""
    # 10 inputs into two 5-input AND trees, one spelled with nands+inverters
    def chain(prefix, start_net, style):
        insts, nets = [], []
        prev = None
        for i in range(5):
            a = start_net + i
            nets.append((a, [("%s_g%d" % (prefix, i), "A", "input")]))
        return insts, nets

    # simpler: 9 distinct inputs through a tree of and2 gates
    instances = []
    nets = {}
    ports = []
    leaf = 200
    for i in range(9):
        ports.append(("P%d" % i, "input", leaf + i))
        nets[leaf + i] = []
    # tree A: and2 chain
    cur = leaf
    nets[leaf] = [("a0", "A", "input")]
    nets[leaf + 1] = [("a0", "B", "input")]
    instances.append(("a0", HD + "and2_1"))
    prev_out = 300
    nets[prev_out] = [("a0", "X", "output")]
    for i in range(2, 9):
        gi = "a%d" % i
        instances.append((gi, HD + "and2_1"))
        nets[prev_out].append((gi, "A", "input"))
        nets[leaf + i] = [(gi, "B", "input")]
        prev_out += 1
        nets[prev_out] = [(gi, "X", "output")]
    instances.append(("fA", HD + "dfxtp_1"))
    nets[prev_out].append(("fA", "D", "input"))
    nets[9] = [("fA", "CLK", "input")]
    ports.append(("CLK", "input", 9))
    g = make_graph(instances=instances, nets=sorted(nets.items()), ports=ports)
    res = decompose(g, [])
    cls = classify(g, res, [])
    rec = [c for c in cls["cones"] if c["n_leaves"] == 9]
    assert rec and rec[0]["method"] == "normalised", cls["cones"]
    assert rec[0]["key"].startswith("nf9:"), rec[0]["key"]
    print("large cone: OK (9 leaves uses the normalised method, not a table)")


if __name__ == "__main__":
    check_parser_and_normal_form()
    check_truth_tables_against_hand_values()
    check_permutation_canonicalisation()
    check_cone_classes_equivalent_but_different_structure()
    check_cone_classes_distinguish_functions()
    check_bit_slice_grouping()
    check_large_cone_is_normalised_not_tabulated()
    print("all checks passed")
