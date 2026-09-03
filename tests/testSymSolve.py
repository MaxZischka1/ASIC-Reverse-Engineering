#!/usr/bin/env python3
"""Synthetic tests for symSolve.py (stage 6).

Every netlist here is built by hand in the stage-3 JSON schema and every expected
answer is worked out on paper, so the unroller is checked against known results
rather than against itself. Covers:

  * the SEQ_SPECS table against genCellModels.SEQ_TEMPLATES, family by family;
  * a shift register with one unlocking sequence -- the solver must return that
    sequence, at that cycle, and prove no other input works;
  * the same design unrolled too shallow (UNSAT) and deep enough (SAT), so the
    incremental deepening is shown to be the thing that finds it;
  * a success condition that is unreachable at any depth;
  * an enable flop and a gated clock, where the answer bit has to land on the one
    cycle the register actually loads;
  * asynchronous reset applied to the state a register *shows* during a cycle,
    not only at the clock edge;
  * the robustness check catching a solution that only works from one power-on
    state;
  * clock inversion, transparent latches, and unconnected pins, which are
    accepted, rejected, or defaulted deliberately rather than guessed at;
  * an LFSR whose returned bits are replayed through a hand-written simulator.

Run:  python3 tests/testSymSolve.py
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import symSolve                                              # noqa: E402
from symSolve import (SEQ_SPECS, ModelError, Unroller,       # noqa: E402
                      answer_bits, check_robust, check_unique, solve)
from coneDecompose import decompose, clock_pins_of           # noqa: E402
from genCellModels import SEQ_TEMPLATES, used_idents         # noqa: E402

HD = "sky130_fd_sc_hd__"

CELLS = {
    HD + "inv_1":     {"inputs": ["A"], "outputs": ["Y"]},
    HD + "buf_1":     {"inputs": ["A"], "outputs": ["X"]},
    HD + "and2_1":    {"inputs": ["A", "B"], "outputs": ["X"]},
    HD + "and2b_1":   {"inputs": ["A_N", "B"], "outputs": ["X"]},
    HD + "and3_1":    {"inputs": ["A", "B", "C"], "outputs": ["X"]},
    HD + "and4b_1":   {"inputs": ["A_N", "B", "C", "D"], "outputs": ["X"]},
    HD + "xor2_1":    {"inputs": ["A", "B"], "outputs": ["X"]},
    HD + "dfxtp_1":   {"inputs": ["CLK", "D"], "outputs": ["Q"]},
    HD + "dfrtp_1":   {"inputs": ["CLK", "D", "RESET_B"], "outputs": ["Q"]},
    HD + "dfrtn_1":   {"inputs": ["CLK_N", "D", "RESET_B"], "outputs": ["Q"]},
    HD + "edfxtp_1":  {"inputs": ["CLK", "D", "DE"], "outputs": ["Q"]},
    HD + "sdfxtp_1":  {"inputs": ["CLK", "D", "SCD", "SCE"], "outputs": ["Q"]},
    HD + "dlxtp_1":   {"inputs": ["D", "GATE"], "outputs": ["Q"]},
    HD + "dlclkp_1":  {"inputs": ["CLK", "GATE"], "outputs": ["GCLK"]},
}


def make_graph(instances, nets, ports, const_nets=(), tied_pins=()):
    """instances: [(id, cell)];  nets: [(id, [(inst, pin, dir)])];
    ports: [(name, direction, net)]."""
    used = {c for _i, c in instances}
    return {
        "top": "t",
        "cells": {c: CELLS[c] for c in used},
        "instances": [{"id": i, "cell": c} for i, c in instances],
        "nets": [{"id": nid, "endpoints": [list(e) for e in eps],
                  "ports": [], "labels": []} for nid, eps in nets],
        "ports": [{"name": n, "direction": d, "net": x} for n, d, x in ports],
        "const_nets": [list(c) for c in const_nets],
        "tied_pins": [list(t) for t in tied_pins],
    }


def make_stim(**signals):
    """Keyword lists of per-cycle 0/1 -> the bench/stimulus.json shape."""
    names = sorted(signals)
    cycles = max(len(v) for v in signals.values())
    return {
        "signals": names,
        "cycles": cycles,
        "value": [[signals[s][c] if c < len(signals[s]) else 0 for s in names]
                  for c in range(cycles)],
        "known": [[1 if c < len(signals[s]) else 0 for s in names]
                  for c in range(cycles)],
        "index": {n: i for i, n in enumerate(names)},
    }


def build(graph, argv, stimulus=None):
    """Stage 4 on the fixture, then an Unroller over exactly the CLI defaults."""
    warnings = []
    cones = decompose(graph, warnings)
    opts = symSolve.parse_options(argv)
    if stimulus is not None:
        opts.stimulus = stimulus
    return Unroller(graph, cones, opts, warnings), opts


def run(graph, argv, stimulus=None):
    u, opts = build(graph, argv, stimulus)
    return u, opts, solve(u, opts)


def bit_at(u, model, port, cycle):
    import z3
    return 1 if z3.is_true(model.eval(u.sym_bits[(port, cycle)],
                                      model_completion=True)) else 0


# ------------------------------------------------------------------- fixtures

def shift_register(detector):
    """4-bit shift register I -> q0 -> q1 -> q2 -> q3, with `detector` on top.

    `detector` is (instance id, cell, {pin: net}) for the cell driving `success`.
    Nets: 2/3/4/5 are q0/q1/q2/q3, 6 is success.
    """
    inst, cell, pinmap = detector
    endpoints = {2: [("u0", "Q", "output"), ("u1", "D", "input")],
                 3: [("u1", "Q", "output"), ("u2", "D", "input")],
                 4: [("u2", "Q", "output"), ("u3", "D", "input")],
                 5: [("u3", "Q", "output")]}
    for pin, net in pinmap.items():
        endpoints[net].append((inst, pin, "input"))
    return make_graph(
        instances=[("u0", HD + "dfxtp_1"), ("u1", HD + "dfxtp_1"),
                   ("u2", HD + "dfxtp_1"), ("u3", HD + "dfxtp_1"),
                   (inst, cell)],
        nets=[(0, [("u0", "D", "input")]),
              (1, [("u%d" % i, "CLK", "input") for i in range(4)]),
              (2, endpoints[2]), (3, endpoints[3]),
              (4, endpoints[4]), (5, endpoints[5]),
              (6, [(inst, list(CELLS[cell]["outputs"])[0], "output")])],
        ports=[("I", "input", 0), ("clk", "input", 1), ("success", "output", 6)])


# ---------------------------------------------------------------------- checks

def check_seq_spec_table():
    """SEQ_SPECS must describe exactly the families SEQ_TEMPLATES models, using
    exactly the pins those templates use, and must agree with stage 4 about
    which pin is the clock."""
    missing = set(SEQ_TEMPLATES) - set(SEQ_SPECS)
    extra = set(SEQ_SPECS) - set(SEQ_TEMPLATES)
    assert not missing, "no SeqSpec for %s" % sorted(missing)
    assert not extra, "SeqSpec for a family with no template: %s" % sorted(extra)

    for fam, spec in sorted(SEQ_SPECS.items()):
        idents = used_idents(SEQ_TEMPLATES[fam])
        assert spec.pins() == idents, (fam, sorted(spec.pins()), sorted(idents))

        clocks = clock_pins_of(fam)
        if spec.kind == "flop":
            assert clocks == {"CLK", "CLK_N"}, (fam, clocks)
            assert spec.clock in clocks, (fam, spec.clock)
            assert spec.inputs().isdisjoint(clocks), (fam, sorted(spec.inputs()))
            assert spec.data == "D", fam
        elif spec.kind == "latch":
            # stage 4 calls a latch's level-sensitive gate the clock pin
            assert spec.gate in clocks, (fam, spec.gate, clocks)
            assert spec.clock is None, fam
        else:
            assert spec.kind == "clkgate", fam
            assert spec.clock in clocks, (fam, spec.clock, clocks)
            assert spec.gate not in clocks, (fam, spec.gate)

        # Q_N is only its own state bit where the template gives it one
        assert spec.split_qn == ("qn_r" in SEQ_TEMPLATES[fam]), fam
    print("SEQ_SPECS: OK (%d families match SEQ_TEMPLATES pin for pin)"
          % len(SEQ_SPECS))


def check_shift_register_password():
    """success = ~q2 & q3 & q1 & q0 four cycles after I, so the one input that
    works is 1011 -- MSB first, because q3 holds the *earliest* bit."""
    g = shift_register(("u4", HD + "and4b_1",
                        {"A_N": 4, "B": 5, "C": 3, "D": 2}))
    stim = make_stim(enable=[1, 1, 1, 1] + [0] * 8, I=[0] * 12)
    u, opts, res = run(g, ["--goal", "success", "--init", "zero",
                           "--symbolic", "I", "--symbolic-when", "enable",
                           "--max-cycles", "10", "--check-unique"], stim)

    assert res["status"] == "sat", res["status"]
    assert res["cycle"] == 4, res["cycle"]
    bits, cycles = answer_bits(u, res["model"], "I")
    assert bits == "1011", bits
    assert cycles == [0, 1, 2, 3], cycles
    assert check_unique(u, res["model"], res["reach"]) is True
    print("shift register: OK (found 1011 at cycle 4, and it is the only input)")


def check_deepening_finds_it():
    """Too few cycles and the sequence is genuinely unreachable; one more and it
    is found. The bound is what separates the two, not the encoding."""
    g = shift_register(("u4", HD + "and4b_1",
                        {"A_N": 4, "B": 5, "C": 3, "D": 2}))
    stim = make_stim(enable=[1, 1, 1, 1] + [0] * 8, I=[0] * 12)
    base = ["--goal", "success", "--init", "zero",
            "--symbolic", "I", "--symbolic-when", "enable"]

    _u, _o, shallow = run(g, base + ["--max-cycles", "4"], stim)
    assert shallow["status"] == "unsat", shallow["status"]
    assert shallow["cycles_unrolled"] == 4, shallow["cycles_unrolled"]

    _u, _o, deep = run(g, base + ["--max-cycles", "5"], stim)
    assert deep["status"] == "sat", deep["status"]
    assert deep["cycle"] == 4, deep["cycle"]
    print("deepening: OK (unsat at 4 cycles, sat at 5, earliest cycle reported)")


def check_unreachable_goal():
    """success = ~q3 & q3 is false for every state, so no depth can reach it."""
    g = shift_register(("u4", HD + "and2b_1", {"A_N": 5, "B": 5}))
    stim = make_stim(enable=[1] * 12, I=[0] * 12)
    _u, _o, res = run(g, ["--goal", "success", "--init", "zero",
                          "--symbolic", "I", "--symbolic-when", "enable",
                          "--max-cycles", "12"], stim)
    assert res["status"] == "unsat", res["status"]
    assert res["cycles_unrolled"] == 12, res["cycles_unrolled"]
    print("unreachable goal: OK (unsat after the full bound, no false positive)")


def check_enable_flop():
    """An edfxtp only loads on a cycle its DE is high, so the answer bit has to
    be on that cycle and nowhere else."""
    g = make_graph(
        instances=[("u0", HD + "edfxtp_1")],
        nets=[(0, [("u0", "D", "input")]),
              (1, [("u0", "CLK", "input")]),
              (2, [("u0", "DE", "input")]),
              (3, [("u0", "Q", "output")])],
        ports=[("I", "input", 0), ("clk", "input", 1), ("EN", "input", 2),
               ("success", "output", 3)])
    stim = make_stim(EN=[0, 0, 1, 0, 0, 0], I=[0] * 6)
    u, _o, res = run(g, ["--goal", "success", "--init", "zero",
                         "--symbolic", "I", "--max-cycles", "6"], stim)
    assert res["status"] == "sat", res["status"]
    assert res["cycle"] == 3, res["cycle"]
    assert bit_at(u, res["model"], "I", 2) == 1, "the loaded cycle must carry it"
    print("enable flop: OK (success at cycle 3, carried by the bit on cycle 2)")


def check_gated_clock():
    """The same behaviour through a dlclkp instead of a flop enable: the clock
    tree has to be walked to find out when the register moves at all."""
    g = make_graph(
        instances=[("ucg", HD + "dlclkp_1"), ("u0", HD + "dfxtp_1")],
        nets=[(0, [("ucg", "CLK", "input")]),
              (1, [("ucg", "GATE", "input")]),
              (2, [("ucg", "GCLK", "output"), ("u0", "CLK", "input")]),
              (3, [("u0", "D", "input")]),
              (4, [("u0", "Q", "output")])],
        ports=[("clk", "input", 0), ("EN", "input", 1), ("I", "input", 3),
               ("success", "output", 4)])
    stim = make_stim(EN=[0, 0, 1, 0, 0, 0], I=[0] * 6)
    u, _o, res = run(g, ["--goal", "success", "--init", "zero",
                         "--symbolic", "I", "--max-cycles", "6"], stim)

    assert u.counts["gated_flops"] == 1, dict(u.counts)
    assert u.clock_of["u0"][0] == ("port", "clk"), u.clock_of["u0"]
    assert u.clock_of["u0"][1] == ["ucg"], u.clock_of["u0"]
    assert res["status"] == "sat", res["status"]
    assert res["cycle"] == 3, res["cycle"]
    assert bit_at(u, res["model"], "I", 2) == 1
    print("gated clock: OK (dlclkp resolved through the clock tree, gated flop "
          "loads only on the enabled cycle)")


def check_async_reset_is_visible():
    """RESET_B is a primary input, so an asserted reset must show in the state
    the design *reads* that cycle, not only in the state after the edge.

    The flop's D is tied high, so with reset released at cycle 1 the earliest
    success is cycle 2. An edge-only reset model would let the free initial
    state satisfy the goal at cycle 0 instead.
    """
    g = make_graph(
        instances=[("u0", HD + "dfrtp_1")],
        nets=[(0, [("u0", "CLK", "input")]),
              (1, [("u0", "RESET_B", "input")]),
              (2, [("u0", "D", "input")]),
              (3, [("u0", "Q", "output")])],
        ports=[("clk", "input", 0), ("rst_n", "input", 1),
               ("success", "output", 3)],
        const_nets=[(2, "1")])
    stim = make_stim(rst_n=[0, 1, 1, 1, 1, 1])
    u, _o, res = run(g, ["--goal", "success", "--init", "free",
                         "--max-cycles", "6"], stim)
    assert res["status"] == "sat", res["status"]
    assert res["cycle"] == 2, res["cycle"]
    assert u.counts["edge_only_async_regs"] == 0, dict(u.counts)
    assert check_robust(u, res["model"], res["reach"]) is True
    print("async reset: OK (visible during the reset cycle, so success lands at "
          "cycle 2 from any initial state)")


def check_robustness_catches_init_dependence():
    """A flop with no reset holding its own output: with a free initial state
    the goal is 'reachable' at cycle 0, but only for one power-on state. The
    robustness check is what tells those apart."""
    g = make_graph(
        instances=[("u0", HD + "dfxtp_1")],
        nets=[(0, [("u0", "CLK", "input")]),
              (1, [("u0", "Q", "output"), ("u0", "D", "input")])],
        ports=[("clk", "input", 0), ("success", "output", 1)])

    u, _o, res = run(g, ["--goal", "success", "--init", "free",
                         "--max-cycles", "6"])
    assert res["status"] == "sat", res["status"]
    assert res["cycle"] == 0, res["cycle"]
    assert u.counts["unreset_regs"] == 1, dict(u.counts)
    assert check_robust(u, res["model"], res["reach"]) is False

    _u, _o, zero = run(g, ["--goal", "success", "--init", "zero",
                           "--max-cycles", "6"])
    assert zero["status"] == "unsat", zero["status"]
    print("robustness: OK (init-dependent solution reported not robust; "
          "unsat from a zero initial state)")


def check_clock_inversion():
    """A negedge flop on an inverted clock is a posedge flop of the root clock
    and is modelled; a flop that really moves on the other edge is refused."""
    def graph(cell, clock_pin):
        return make_graph(
            instances=[("uinv", HD + "inv_1"), ("u0", cell)],
            nets=[(0, [("uinv", "A", "input")]),
                  (1, [("uinv", "Y", "output"), ("u0", clock_pin, "input")]),
                  (2, [("u0", "D", "input")]),
                  (3, [("u0", "Q", "output")]),
                  (4, [("u0", "RESET_B", "input")] if clock_pin == "CLK_N" else
                      [("uinv", "A", "input")])],
            ports=([("clk", "input", 0), ("I", "input", 2),
                    ("success", "output", 3)] +
                   ([("rst_n", "input", 4)] if clock_pin == "CLK_N" else [])))

    # dfrtn on ~clk: negedge of an inverted clock == posedge of clk
    g = graph(HD + "dfrtn_1", "CLK_N")
    stim = make_stim(rst_n=[1] * 6, I=[0] * 6)
    u, _o, res = run(g, ["--goal", "success", "--init", "zero",
                         "--symbolic", "I", "--max-cycles", "6"], stim)
    assert u.counts["half_cycle_flops"] == 0, dict(u.counts)
    assert res["status"] == "sat" and res["cycle"] == 1, res
    assert bit_at(u, res["model"], "I", 0) == 1

    # a posedge flop on ~clk really does move half a cycle later
    g = graph(HD + "dfxtp_1", "CLK")
    try:
        build(g, ["--goal", "success", "--init", "zero", "--max-cycles", "4"])
        raise AssertionError("an opposite-edge flop should be refused")
    except ModelError as e:
        assert "--allow-half-cycle" in str(e), str(e)
    u, _o = build(g, ["--goal", "success", "--init", "zero", "--max-cycles", "4",
                      "--allow-half-cycle"])
    assert u.counts["half_cycle_flops"] == 1, dict(u.counts)
    print("clock inversion: OK (inverted negedge flop accepted, opposite-edge "
          "flop refused until asked for explicitly)")


def check_latches_refused():
    """A transparent latch has no cycle-accurate model here, so it is a hard
    error until the approximation is asked for by name."""
    g = make_graph(
        instances=[("u0", HD + "dlxtp_1")],
        nets=[(0, [("u0", "GATE", "input")]),
              (1, [("u0", "D", "input")]),
              (2, [("u0", "Q", "output")])],
        ports=[("clk", "input", 0), ("I", "input", 1), ("success", "output", 2)])
    try:
        build(g, ["--goal", "success", "--max-cycles", "4"])
        raise AssertionError("a transparent latch should be refused")
    except ModelError as e:
        assert "--latch-as-flop" in str(e), str(e)

    u, _o = build(g, ["--goal", "success", "--max-cycles", "4", "--latch-as-flop"])
    assert len(u.latches) == 1 and not u.flops, (u.latches, u.flops)
    print("latches: OK (refused by default, approximated only on request)")


def check_unconnected_pins():
    """Scan pins with an unambiguous inactive value are defaulted and counted;
    a data pin with no such value is an error until it is tied."""
    scan = make_graph(
        instances=[("u0", HD + "sdfxtp_1")],
        nets=[(0, [("u0", "CLK", "input")]),
              (1, [("u0", "D", "input")]),
              (2, [("u0", "Q", "output")])],
        ports=[("clk", "input", 0), ("I", "input", 1), ("success", "output", 2)])
    stim = make_stim(I=[0] * 6)
    u, _o, res = run(scan, ["--goal", "success", "--init", "zero",
                            "--symbolic", "I", "--max-cycles", "6"], stim)
    assert u.counts["deasserted_pins"] > 0, dict(u.counts)
    assert res["status"] == "sat" and res["cycle"] == 1, res
    assert bit_at(u, res["model"], "I", 0) == 1, "SCE defaulted low: D loads"

    dangling = make_graph(
        instances=[("u0", HD + "dfxtp_1")],
        nets=[(0, [("u0", "CLK", "input")]),
              (1, [("u0", "Q", "output")])],
        ports=[("clk", "input", 0), ("success", "output", 1)])
    try:
        _u, _o, _r = run(dangling, ["--goal", "success", "--init", "zero",
                                    "--max-cycles", "4"])
        raise AssertionError("an unconnected D should be an error")
    except ModelError as e:
        assert "--tie" in str(e) and "u0.D" in str(e), str(e)

    _u, _o, res = run(dangling, ["--goal", "success", "--init", "zero",
                                 "--max-cycles", "4", "--tie", "D=0"])
    assert res["status"] == "unsat", res["status"]
    print("unconnected pins: OK (scan pins defaulted and counted, a dangling D "
          "refused until tied)")


def check_lfsr_answer_replays():
    """q0 <- I ^ q2, q1 <- q0, q2 <- q1; success when all three are high.

    The solver's bits are replayed through a simulator written here from the
    schematic, which is the same cross-check the bench harness performs against
    Verilator: an answer nothing else confirms is not an answer.
    """
    g = make_graph(
        instances=[("ux", HD + "xor2_1"), ("u0", HD + "dfxtp_1"),
                   ("u1", HD + "dfxtp_1"), ("u2", HD + "dfxtp_1"),
                   ("ua", HD + "and3_1")],
        nets=[(0, [("ux", "A", "input")]),                       # I
              (1, [("u%d" % i, "CLK", "input") for i in range(3)]),
              (2, [("ux", "X", "output"), ("u0", "D", "input")]),
              (3, [("u0", "Q", "output"), ("u1", "D", "input"),
                   ("ua", "A", "input")]),                       # q0
              (4, [("u1", "Q", "output"), ("u2", "D", "input"),
                   ("ua", "B", "input")]),                       # q1
              (5, [("u2", "Q", "output"), ("ux", "B", "input"),
                   ("ua", "C", "input")]),                       # q2
              (6, [("ua", "X", "output")])],
        ports=[("I", "input", 0), ("clk", "input", 1),
               ("success", "output", 6)])
    stim = make_stim(I=[0] * 10)
    u, _o, res = run(g, ["--goal", "success", "--init", "zero",
                         "--symbolic", "I", "--max-cycles", "10"], stim)
    assert res["status"] == "sat", res["status"]
    bits, _cycles = answer_bits(u, res["model"], "I")

    # reference simulator, written from the schematic above
    q0 = q1 = q2 = 0
    hit = None
    for c, b in enumerate(bits):
        if q0 and q1 and q2:
            hit = c
            break
        q0, q1, q2 = int(b) ^ q2, q0, q1
    assert hit == res["cycle"], (hit, res["cycle"], bits)
    assert bits[:3] == "111", bits
    print("LFSR: OK (solver's %d bits replay to success at cycle %d)"
          % (len(bits), hit))


def check_vacuous_runs_are_refused():
    """A SAT result that no chosen bit caused is not an answer.

    Both shapes are reported as vacuous rather than sat: nothing made symbolic
    (so the free inputs and initial state did all the work), and a goal already
    reached before the first symbolic bit could propagate.
    """
    g = make_graph(
        instances=[("u0", HD + "dfxtp_1")],
        nets=[(0, [("u0", "CLK", "input")]),
              (1, [("u0", "D", "input")]),
              (2, [("u0", "Q", "output")])],
        ports=[("clk", "input", 0), ("I", "input", 1), ("success", "output", 2)])

    # nothing symbolic: a free initial state satisfies the goal at cycle 0
    u, _o, res = run(g, ["--goal", "success", "--init", "free", "--max-cycles", "4"])
    assert res["status"] == "sat", res["status"]
    why = symSolve.vacuity_reason(u, res["cycle"])
    assert why and "nothing to solve for" in why, why

    # symbolic, but the goal is met at cycle 0 -- before that bit can matter
    stim = make_stim(I=[0] * 6)
    u, _o, res = run(g, ["--goal", "success", "--init", "free", "--symbolic", "I",
                         "--max-cycles", "4"], stim)
    assert res["status"] == "sat" and res["cycle"] == 0, res
    why = symSolve.vacuity_reason(u, res["cycle"])
    assert why and "--init zero" in why, why

    # the same design from a known initial state is a real, caused answer
    u, _o, res = run(g, ["--goal", "success", "--init", "zero", "--symbolic", "I",
                         "--max-cycles", "4"], stim)
    assert res["status"] == "sat" and res["cycle"] == 1, res
    assert symSolve.vacuity_reason(u, res["cycle"]) is None
    print("vacuity: OK (uncaused SAT refused with and without symbolic bits, "
          "caused answer accepted)")


def check_cli_refuses_bad_configuration():
    """The command line rejects a run that cannot produce an answer, before it
    spends any time solving."""
    tmp = tempfile.mkdtemp(prefix="symsolve-cfg-")
    try:
        g = make_graph(
            instances=[("u0", HD + "dfxtp_1")],
            nets=[(0, [("u0", "CLK", "input")]),
                  (1, [("u0", "D", "input")]),
                  (2, [("u0", "Q", "output")])],
            ports=[("clk", "input", 0), ("I", "input", 1),
                   ("success", "output", 2)])
        gp = os.path.join(tmp, "G.json")
        cp = os.path.join(tmp, "C.json")
        json.dump(g, open(gp, "w"))
        json.dump(decompose(g, []), open(cp, "w"))
        base = [gp, cp, "--out", os.path.join(tmp, "S.json"), "--max-cycles", "4"]

        for extra, want in [([], "--symbolic PORT"),
                            (["--symbolic", "nosuch"], "is not an input port"),
                            (["--symbolic", "I", "--symbolic-when", "enable"],
                             "needs a --stimulus")]:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = symSolve.main(base + extra)
            assert rc == 1, (extra, rc, out.getvalue())
            assert want in err.getvalue(), (extra, err.getvalue())

        # --preflight still works without a symbolic port: it solves nothing
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = symSolve.main(base + ["--preflight"])
        assert rc == 0, out.getvalue()
        assert "preflight" in json.load(open(os.path.join(tmp, "S.json")))

        # and a vacuous solve exits nonzero with status "vacuous" on disk
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = symSolve.main(base + ["--symbolic", "I", "--init", "free"])
        assert rc == 1, out.getvalue()
        sol = json.load(open(os.path.join(tmp, "S.json")))
        assert sol["status"] == "vacuous", sol
        assert "nothing to solve for" not in sol["vacuous"], sol["vacuous"]
        assert "answer" not in sol, sol
        print("CLI configuration: OK (no --symbolic, a misspelt port, and "
              "--symbolic-when with no stimulus all refused; vacuous solve "
              "exits nonzero)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_bus_port_reads_recorded_bus():
    """The layout labels a bus bit `O[3]`; the recording reassembles it as `O`.
    A bit-blasted port must read the right bit, not the truthiness of the word."""
    g = make_graph(
        instances=[("u0", HD + "dfxtp_1")],
        nets=[(0, [("u0", "CLK", "input")]),
              (1, [("u0", "D", "input")]),
              (2, [("u0", "Q", "output")])],
        ports=[("clk", "input", 0), ("B[2]", "input", 1),
               ("success", "output", 2)])
    stim = make_stim(B=[0b100, 0b011, 0b100, 0, 0, 0])
    u, _o, res = run(g, ["--goal", "success", "--init", "zero",
                         "--max-cycles", "5"], stim)
    # bit 2 of B is 1 on cycles 0 and 2, so Q is high on cycles 1 and 3
    assert res["status"] == "sat" and res["cycle"] == 1, res
    assert [u.stim_value("B[2]", c) for c in range(4)] == [1, 0, 1, 0]
    assert [u.stim_value("B[0]", c) for c in range(4)] == [0, 1, 0, 0]
    print("bus ports: OK (B[2] reads bit 2 of the recorded bus B)")


def check_undriven_nets_reported_and_tieable():
    """A net that is read but driven by nothing is a free bit every cycle. It has
    to be counted before solving, and pinnable, or the solver wins through it."""
    g = make_graph(
        instances=[("u0", HD + "and2_1"), ("u1", HD + "dfxtp_1")],
        nets=[(0, [("u0", "A", "input")]),          # I
              (1, [("u0", "B", "input")]),          # driven by nothing
              (2, [("u0", "X", "output"), ("u1", "D", "input")]),
              (3, [("u1", "CLK", "input")]),
              (4, [("u1", "Q", "output")])],
        ports=[("I", "input", 0), ("clk", "input", 3), ("success", "output", 4)])
    stim = make_stim(I=[0] * 6)
    base = ["--goal", "success", "--init", "zero", "--symbolic", "I",
            "--max-cycles", "5"]

    u, _o, res = run(g, base, stim)
    assert u.undriven_nets == {1}, u.undriven_nets
    assert symSolve.preflight(u)["undriven_data_nets"] == 1
    assert any("driven by nothing" in w for w in u.warnings), u.warnings
    assert res["status"] == "sat" and res["cycle"] == 1, res

    # pinned low, the AND can never pass I, so the goal becomes unreachable
    u, _o, res = run(g, base + ["--tie-undriven", "0"], stim)
    assert res["status"] == "unsat", res["status"]
    assert not any("driven by nothing" in w for w in u.warnings), u.warnings

    u, _o, res = run(g, base + ["--tie-undriven", "1"], stim)
    assert res["status"] == "sat" and res["cycle"] == 1, res
    assert bit_at(u, res["model"], "I", 0) == 1
    print("undriven nets: OK (counted in preflight, warned about, and pinnable "
          "with --tie-undriven)")


def read_dimacs(path):
    """-> (clauses, {variable index: name}) from a CNF with `c <var> <name>`."""
    clauses, names = [], {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("c "):
                parts = line.split(None, 2)
                if len(parts) == 3 and parts[1].isdigit():
                    names[int(parts[1])] = parts[2]
            elif line and not line.startswith("p "):
                lits = [int(x) for x in line.split()]
                assert lits and lits[-1] == 0, line
                clauses.append(lits[:-1])
    return clauses, names


def check_cli_end_to_end():
    """The command line, the JSON it writes, and the CNF it exports.

    The CNF is solved on its own and its model read back through the `c <var>
    <name>` map, which is the whole point of the export: an external SAT solver
    has to be able to hand the answer bits back.
    """
    tmp = tempfile.mkdtemp(prefix="symsolve-")
    try:
        g = shift_register(("u4", HD + "and4b_1",
                            {"A_N": 4, "B": 5, "C": 3, "D": 2}))
        warnings = []
        stim = make_stim(enable=[1, 1, 1, 1] + [0] * 8, I=[0] * 12)
        stim.pop("index")                       # as vcdToStimulus.py writes it
        paths = {n: os.path.join(tmp, n) for n in
                 ("GRAPH.json", "CONES.json", "stim.json", "SOLUTION.json",
                  "sol.bits", "f.cnf")}
        json.dump(g, open(paths["GRAPH.json"], "w"))
        json.dump(decompose(g, warnings), open(paths["CONES.json"], "w"))
        json.dump(stim, open(paths["stim.json"], "w"))

        argv = [paths["GRAPH.json"], paths["CONES.json"],
                "--stimulus", paths["stim.json"], "--symbolic", "I",
                "--symbolic-when", "enable", "--goal", "success",
                "--init", "zero", "--max-cycles", "10",
                "--check-unique", "--check-robust",
                "--out", paths["SOLUTION.json"], "--bits-out", paths["sol.bits"]]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = symSolve.main(argv)
        assert rc == 0, (rc, buf.getvalue())

        sol = json.load(open(paths["SOLUTION.json"]))
        assert sol["status"] == "sat", sol
        assert sol["goal"] == {"port": "success", "value": 1, "cycle": 4}, sol["goal"]
        assert sol["answer"]["I"]["bits"] == "1011", sol["answer"]
        assert sol["unique"] is True and sol["robust"] is True, sol
        assert sol["preflight"]["flops"] == 4, sol["preflight"]
        assert open(paths["sol.bits"]).read().strip() == "1011"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = symSolve.main(argv[:argv.index("--check-unique")] +
                               ["--dimacs", paths["f.cnf"],
                                "--out", os.path.join(tmp, "D.json")])
        assert rc == 0, buf.getvalue()

        clauses, names = read_dimacs(paths["f.cnf"])
        assert clauses, "no CNF written"
        import z3
        var = {i: z3.Bool("v%d" % i) for i in range(1, max(map(abs, sum(clauses, []))) + 1)}
        s = z3.Solver()
        for cl in clauses:
            s.add(z3.Or([var[abs(l)] if l > 0 else z3.Not(var[abs(l)]) for l in cl]))
        assert s.check() == z3.sat, "the exported CNF is unsatisfiable"
        m = s.model()
        by_name = {n: i for i, n in names.items()}
        bits = "".join(
            "1" if z3.is_true(m.eval(var[by_name["in.I@%d" % c]],
                                     model_completion=True)) else "0"
            for c in range(4))
        assert bits == "1011", bits
        print("CLI end to end: OK (SOLUTION.json, bits file, and a CNF export "
              "whose own model reads back as 1011)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    check_seq_spec_table()
    check_shift_register_password()
    check_deepening_finds_it()
    check_unreachable_goal()
    check_enable_flop()
    check_gated_clock()
    check_async_reset_is_visible()
    check_robustness_catches_init_dependence()
    check_clock_inversion()
    check_latches_refused()
    check_unconnected_pins()
    check_lfsr_answer_replays()
    check_vacuous_runs_are_refused()
    check_bus_port_reads_recorded_bus()
    check_undriven_nets_reported_and_tieable()
    check_cli_end_to_end()
    check_cli_refuses_bad_configuration()
    print("all checks passed")
