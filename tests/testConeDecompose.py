#!/usr/bin/env python3
"""Synthetic tests for coneDecompose.py.

Netlist graphs are built here by hand in the stage-3 JSON schema, with the cones
worked out on paper, so the traversal is checked against known answers rather
than against itself. Covers: a plain fan-in, sharing between two registers, a
register boundary, primary inputs, constants (net-level and tied-pin), a scan
flop's several data roots, a latch whose GATE is a clock, an unconnected sink,
an opaque black box, a combinational loop, and topological ordering.

Run:  python3 tests/testConeDecompose.py
"""

import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from coneDecompose import decompose, clock_pins_of  # noqa: E402

HD = "sky130_fd_sc_hd__"

# Pin definitions for the cell types used below (as stage 3 emits them).
CELLS = {
    HD + "inv_1":    {"inputs": ["A"], "outputs": ["Y"]},
    HD + "buf_1":    {"inputs": ["A"], "outputs": ["X"]},
    HD + "nand2_1":  {"inputs": ["A", "B"], "outputs": ["Y"]},
    HD + "and2_1":   {"inputs": ["A", "B"], "outputs": ["X"]},
    HD + "or2_1":    {"inputs": ["A", "B"], "outputs": ["X"]},
    HD + "dfxtp_1":  {"inputs": ["CLK", "D"], "outputs": ["Q"]},
    HD + "sdfrtp_1": {"inputs": ["CLK", "D", "RESET_B", "SCD", "SCE"],
                      "outputs": ["Q"]},
    HD + "dlxtp_1":  {"inputs": ["D", "GATE"], "outputs": ["Q"]},
    "MYBLACKBOX":    {"inputs": [], "outputs": ["P0"], "blackbox": True},
}


def make_graph(instances, nets, ports, const_nets=(), tied_pins=()):
    """instances: [(id, cell)];  nets: [(id, [(inst, pin, dir) | ("port", name)])]"""
    net_list = []
    for nid, endpoints in nets:
        eps = [list(e) for e in endpoints]
        net_list.append({"id": nid, "endpoints": eps, "ports": [], "labels": []})
    used = {c for _i, c in instances}
    return {
        "top": "t",
        "cells": {c: CELLS[c] for c in used},
        "instances": [{"id": i, "cell": c} for i, c in instances],
        "nets": net_list,
        "ports": [{"name": n, "direction": d, "net": x} for n, d, x in ports],
        "const_nets": [list(c) for c in const_nets],
        "tied_pins": [list(t) for t in tied_pins],
    }


def cone_by_root(res, net):
    for c in res["cones"]:
        if c["root_net"] == net:
            return c
    raise AssertionError("no cone rooted at net %d (roots: %s)"
                         % (net, [c["root_net"] for c in res["cones"]]))


def leaves_of(cone):
    out = set()
    for l in cone["leaves"]:
        if l["kind"] == "port":
            out.add(("port", l["name"]))
        elif l["kind"] in ("reg", "opaque"):
            out.add((l["kind"], l["inst"], l["pin"]))
        elif l["kind"] == "const":
            out.add(("const", l["value"]))
        else:
            out.add(("undriven",))
    return out


def check_basic():
    """IN0,IN1 -> nand -> inv -> flop.D ; flop.Q -> or2(with IN2) -> OUT.

    Cone at the flop:  cells [nand, inv] (that order), leaves both ports, depth 2.
    Cone at OUT:       cells [or2], leaf = the register output, depth 1.
    """
    g = make_graph(
        instances=[("u0", HD + "nand2_1"), ("u1", HD + "inv_1"),
                   ("u2", HD + "dfxtp_1"), ("u3", HD + "or2_1")],
        nets=[
            (0, [("u0", "A", "input")]),                      # IN0
            (1, [("u0", "B", "input")]),                      # IN1
            (2, [("u0", "Y", "output"), ("u1", "A", "input")]),
            (3, [("u1", "Y", "output"), ("u2", "D", "input")]),
            (4, [("u2", "CLK", "input")]),                    # CLK
            (5, [("u2", "Q", "output"), ("u3", "A", "input")]),
            (6, [("u3", "B", "input")]),                      # IN2
            (7, [("u3", "X", "output")]),                     # OUT
        ],
        ports=[("IN0", "input", 0), ("IN1", "input", 1), ("CLK", "input", 4),
               ("IN2", "input", 6), ("OUT", "output", 7)])
    warnings = []
    res = decompose(g, warnings)

    assert res["summary"]["registers"] == 1, res["summary"]
    assert len(res["cones"]) == 2, [c["root_net"] for c in res["cones"]]

    c = cone_by_root(res, 3)
    assert c["cells"] == ["u0", "u1"], c["cells"]        # topological: nand then inv
    assert leaves_of(c) == {("port", "IN0"), ("port", "IN1")}, leaves_of(c)
    assert c["depth"] == 2, c["depth"]
    assert c["sinks"] == [{"kind": "reg", "inst": "u2", "pin": "D"}], c["sinks"]

    c = cone_by_root(res, 7)
    assert c["cells"] == ["u3"], c["cells"]
    assert leaves_of(c) == {("reg", "u2", "Q"), ("port", "IN2")}, leaves_of(c)
    assert c["depth"] == 1, c["depth"]
    assert c["sinks"] == [{"kind": "port", "name": "OUT"}], c["sinks"]

    # the clock net is not a cone root: traversal stops at the sequential boundary
    assert all(c["root_net"] != 4 for c in res["cones"]), res["cones"]
    assert warnings == [], warnings
    assert res["summary"]["orphan_cells"] == 0, res["summary"]
    print("basic: OK (fan-in, register boundary, clock excluded, topo order)")


def check_sharing_and_constants():
    """One and2 feeds two flops; its B input is a constant net and one flop's
    data pin is tied off. Shared logic must appear in both cones."""
    g = make_graph(
        instances=[("u0", HD + "and2_1"), ("u1", HD + "buf_1"),
                   ("u2", HD + "dfxtp_1"), ("u3", HD + "dfxtp_1")],
        nets=[
            (0, [("u0", "A", "input")]),                       # IN
            (1, [("u0", "B", "input")]),                       # constant net
            (2, [("u0", "X", "output"), ("u2", "D", "input"), ("u1", "A", "input")]),
            (3, [("u1", "X", "output"), ("u3", "D", "input")]),
            (4, [("u2", "CLK", "input"), ("u3", "CLK", "input")]),
            (5, [("u2", "Q", "output")]),
        ],
        ports=[("IN", "input", 0), ("CLK", "input", 4), ("Q0", "output", 5)],
        const_nets=[(1, "1")])
    warnings = []
    res = decompose(g, warnings)

    shared = cone_by_root(res, 2)
    assert shared["cells"] == ["u0"], shared["cells"]
    assert leaves_of(shared) == {("port", "IN"), ("const", "1")}, leaves_of(shared)
    # the same net feeds a register and (through the buffer) another cone
    assert shared["sinks"] == [{"kind": "reg", "inst": "u2", "pin": "D"}], shared["sinks"]

    downstream = cone_by_root(res, 3)
    assert downstream["cells"] == ["u0", "u1"], downstream["cells"]
    assert leaves_of(downstream) == {("port", "IN"), ("const", "1")}, \
        leaves_of(downstream)
    assert downstream["depth"] == 2, downstream["depth"]

    # Q0 is an output port driven directly by a register: a cone with no logic
    q0 = cone_by_root(res, 5)
    assert q0["cells"] == [] and q0["depth"] == 0, q0
    assert leaves_of(q0) == {("reg", "u2", "Q")}, leaves_of(q0)
    assert warnings == [], warnings
    print("sharing+constants: OK (shared cell in both cones, const leaf, "
          "zero-depth cone)")


def check_scan_latch_tied_opaque():
    """A scan flop contributes one cone per data pin (not for CLK); a latch's
    GATE counts as its clock; a tied pin becomes a constant leaf; a black box
    output ends a cone as an opaque boundary."""
    g = make_graph(
        instances=[("u0", HD + "sdfrtp_1"), ("u1", HD + "dlxtp_1"),
                   ("u2", HD + "inv_1"), ("b0", "MYBLACKBOX")],
        nets=[
            (0, [("u0", "D", "input")]),                       # IN
            (1, [("u0", "CLK", "input")]),                     # CLK
            (2, [("u0", "SCD", "input")]),                     # SCAN_IN
            (3, [("u0", "RESET_B", "input")]),                 # RST
            (4, [("u0", "Q", "output"), ("u1", "D", "input")]),
            (5, [("u1", "GATE", "input"), ("u2", "Y", "output")]),
            (6, [("u2", "A", "input"), ("b0", "P0", "output")]),
        ],
        ports=[("IN", "input", 0), ("CLK", "input", 1), ("SCAN_IN", "input", 2),
               ("RST", "input", 3)],
        tied_pins=[("u0", "SCE", "0")])
    warnings = []
    res = decompose(g, warnings)

    roots = sorted(c["root_net"] for c in res["cones"])
    # scan flop: D(0), SCD(2), RESET_B(3) are roots; CLK(1) is not.
    # latch: D(4) is a root; GATE(5) is its clock, so not a root.
    assert roots == [0, 2, 3, 4], roots
    assert leaves_of(cone_by_root(res, 4)) == {("reg", "u0", "Q")}, \
        leaves_of(cone_by_root(res, 4))

    reg = {r["inst"]: r for r in res["registers"]}
    assert reg["u0"]["clock_pins"] == ["CLK"], reg["u0"]
    assert sorted(reg["u0"]["data_pins"]) == ["D", "RESET_B", "SCD", "SCE"], reg["u0"]
    assert reg["u1"]["clock_pins"] == ["GATE"], reg["u1"]
    assert reg["u1"]["data_pins"] == ["D"], reg["u1"]

    # SCE is tied, so it is not reported unconnected and forms no cone
    assert warnings == [] or all("SCE" not in w for w in warnings), warnings

    # the inverter driving the latch gate feeds only a clock pin: it belongs to
    # the clock tree, and must not be reported as dead logic
    assert res["summary"]["orphan_cells"] == 0, res["summary"]
    assert res["clock_tree_cells"] == ["u2"], res["clock_tree_cells"]
    assert res["summary"]["clock_tree_cells"] == 1, res["summary"]
    print("scan/latch/tied/opaque: OK (per-data-pin roots, GATE is a clock, "
          "clock-tree cell classified not orphaned)")


def check_opaque_and_loop():
    """A black box output terminates a cone as 'opaque'; a combinational loop is
    cut with a warning instead of hanging."""
    g = make_graph(
        instances=[("b0", "MYBLACKBOX"), ("u0", HD + "buf_1"),
                   ("u1", HD + "dfxtp_1")],
        nets=[
            (0, [("b0", "P0", "output"), ("u0", "A", "input")]),
            (1, [("u0", "X", "output"), ("u1", "D", "input")]),
            (2, [("u1", "CLK", "input")]),
        ],
        ports=[("CLK", "input", 2)])
    res = decompose(g, [])
    c = cone_by_root(res, 1)
    assert leaves_of(c) == {("opaque", "b0", "P0")}, leaves_of(c)
    assert c["cells"] == ["u0"], c["cells"]

    # loop: nand output feeds its own input through an inverter
    g2 = make_graph(
        instances=[("u0", HD + "nand2_1"), ("u1", HD + "inv_1"),
                   ("u2", HD + "dfxtp_1")],
        nets=[
            (0, [("u0", "A", "input")]),
            (1, [("u0", "Y", "output"), ("u1", "A", "input")]),
            (2, [("u1", "Y", "output"), ("u0", "B", "input"),
                 ("u2", "D", "input")]),
            (3, [("u2", "CLK", "input")]),
        ],
        ports=[("IN", "input", 0), ("CLK", "input", 3)])
    warnings = []
    res2 = decompose(g2, warnings)
    assert any("combinational loop" in w for w in warnings), warnings
    c2 = cone_by_root(res2, 2)
    assert set(c2["cells"]) == {"u0", "u1"}, c2["cells"]
    print("opaque+loop: OK (black box ends a cone; loop cut with a warning)")


def check_every_cell_reachable():
    """Sanity property that should hold for any well-formed netlist: every
    combinational cell that eventually reaches a register or output appears in
    at least one cone, and every cone's cells are ordered after their inputs."""
    g = make_graph(
        instances=[("u0", HD + "inv_1"), ("u1", HD + "inv_1"), ("u2", HD + "inv_1"),
                   ("u3", HD + "nand2_1"), ("u4", HD + "dfxtp_1")],
        nets=[
            (0, [("u0", "A", "input")]),
            (1, [("u0", "Y", "output"), ("u1", "A", "input")]),
            (2, [("u1", "Y", "output"), ("u2", "A", "input")]),
            (3, [("u2", "Y", "output"), ("u3", "A", "input")]),
            (4, [("u3", "B", "input")]),
            (5, [("u3", "Y", "output"), ("u4", "D", "input")]),
            (6, [("u4", "CLK", "input")]),
        ],
        ports=[("IN", "input", 0), ("IN2", "input", 4), ("CLK", "input", 6)])
    res = decompose(g, [])
    c = cone_by_root(res, 5)
    assert c["cells"] == ["u0", "u1", "u2", "u3"], c["cells"]
    assert c["depth"] == 4, c["depth"]
    order = {inst: i for i, inst in enumerate(c["cells"])}
    assert order["u0"] < order["u1"] < order["u2"] < order["u3"]
    assert res["summary"]["orphan_cells"] == 0, res["summary"]
    print("coverage: OK (chain ordered inputs-first, depth counted, none orphan)")


def check_dead_logic_still_reported():
    """A cell that reaches neither a register, an output, nor a clock is genuinely
    dead and must still be reported — the clock-tree rule must not hide it."""
    g = make_graph(
        instances=[("u0", HD + "inv_1"), ("u1", HD + "inv_1"),
                   ("u2", HD + "dfxtp_1")],
        nets=[
            (0, [("u0", "A", "input")]),                 # IN
            (1, [("u0", "Y", "output"), ("u1", "A", "input")]),
            (2, [("u1", "Y", "output")]),                # goes nowhere
            (3, [("u2", "D", "input")]),                 # IN2
            (4, [("u2", "CLK", "input")]),               # CLK
        ],
        ports=[("IN", "input", 0), ("IN2", "input", 3), ("CLK", "input", 4)])
    warnings = []
    res = decompose(g, warnings)
    assert sorted(res["orphan_cells"]) == ["u0", "u1"], res["orphan_cells"]
    assert res["clock_tree_cells"] == [], res["clock_tree_cells"]
    assert any("reach neither" in w for w in warnings), warnings
    print("dead logic: OK (cells reaching nothing are still reported)")


def check_clock_load_buffer():
    """Clock-tree synthesis hangs buffers off a clock net purely as capacitive
    load to balance skew; their outputs are intentionally unconnected. They must
    be recognised as clock-tree cells, not reported as dead logic."""
    g = make_graph(
        instances=[("u0", HD + "buf_1"),      # clock tree driver
                   ("u1", HD + "dfxtp_1"), ("u2", HD + "dfxtp_1"),
                   ("u3", HD + "buf_1"),      # skew-balancing load: output unused
                   ("u4", HD + "inv_1")],     # genuinely dead: input is data
        nets=[
            (0, [("u0", "A", "input")]),                        # CLK port
            (1, [("u0", "X", "output"), ("u1", "CLK", "input"),
                 ("u2", "CLK", "input"), ("u3", "A", "input")]),
            (2, [("u1", "D", "input"), ("u2", "D", "input")]),  # DIN
            (3, [("u1", "Q", "output")]),                       # Q0
            (4, [("u4", "A", "input")]),                        # DIN2 -> dead inv
        ],
        ports=[("CLK", "input", 0), ("DIN", "input", 2), ("Q0", "output", 3),
               ("DIN2", "input", 4)])
    warnings = []
    res = decompose(g, warnings)
    assert "u3" in res["clock_tree_cells"], res["clock_tree_cells"]
    assert res["summary"]["clock_load_cells"] == 1, res["summary"]
    # the inverter fed by a data port drives nothing and is NOT a clock load
    assert res["orphan_cells"] == ["u4"], res["orphan_cells"]
    print("clock loads: OK (skew-balancing buffer classified as clock tree; "
          "dead data-side logic still reported)")


def check_clock_pin_table():
    """The clock-pin rule must cover every sequential family the model generator
    knows, and never call a data pin a clock."""
    from genCellModels import SEQ_TEMPLATES
    for fam in SEQ_TEMPLATES:
        pins = clock_pins_of(fam)
        assert pins, fam
        if fam in ("dlclkp", "sdlclkp"):
            assert pins == {"CLK"}, (fam, pins)
        elif fam == "lpflow_inputisolatch":
            assert pins == {"SLEEP_B"}, (fam, pins)
        elif fam.startswith("dl"):
            assert pins == {"GATE", "GATE_N"}, (fam, pins)
        else:
            assert pins == {"CLK", "CLK_N"}, (fam, pins)
        assert "D" not in pins and "SCD" not in pins, (fam, pins)
    print("clock pins: OK (all %d sequential families classified)" % len(SEQ_TEMPLATES))


if __name__ == "__main__":
    check_basic()
    check_sharing_and_constants()
    check_scan_latch_tied_opaque()
    check_opaque_and_loop()
    check_every_cell_reachable()
    check_dead_logic_still_reported()
    check_clock_load_buffer()
    check_clock_pin_table()
    print("all checks passed")
