"""
genBlockVerilog.py -- turn BLOCK_MATCH.json (+ MODULE_GRAPH.json for the
gate-level wiring) into blockMatch.v: a *hierarchical* structural Verilog
netlist drawn at the granularity blockMatch.py discovered, with one module
per identified structure --

    shift_reg_A / shift_reg_B -- one enable-gated shift register each: 8
                     mux2 + 8 dfrtp_2, serial input and en in, the eight
                     registered bits out.
    adder_compare -- the datapath cloud: 8 adder bit slices, the carry
                     merges above them, the sum bits and the equality tree
                     that collapses them to S.
    clk_tree      -- the 3 clkbuf_16 cells, clk in, one fanned-out clock
                     net per group of 8 flip-flops.
    blockMatch    -- the top module wiring those four together.

Same gates as adder_demo.v (genVerilog.py's flat, one-instantiation-per-gate
dump), zero added or removed -- only regrouped, and, more usefully, renamed:
where the flat netlist calls everything net_<id>, this file uses the names
blockMatch.py recovered, so the netlist reads as what it is.

WIRE NAMING
============
Every wire name comes from BLOCK_MATCH.json, in this order of precedence:

  1. a net that reaches a real chip port keeps that port's name (A, B, S,
     clk, en, rst_n);
  2. a flip-flop in a shift chain names its own two nets after its position
     in that chain -- a_reg3 for the Q of the flop holding A[3], a_nxt3 for
     the D feeding it. This is the naming that makes the datapath legible:
     it is what turns "net_2039" into "the third bit of A";
  3. a gate inside a matched block is named after its role in that block,
     carrying the bit index where the block has one: g3/p3/p3_dual for a
     slice's generate and propagate terms, xsum3 for its half-sum, then
     carry<k>, sumbit<k>, eq<k> and clknet<k> numbered in topological order
     for the carry chain, the sum bits, the equality tree and the clock
     tree;
  4. anything left keeps genVerilog.py's net_<id> fallback (nothing in this
     design does).

Names are asserted unique before emission -- two nets colliding on one
identifier would silently short them in the output.

MODULE BOUNDARIES
==================
A module's ports are exactly the nets crossing its group's gate set:
driven inside and read outside (output), or vice versa (input). Since every
net has one global name, every instantiation is a straight .name(name) map.

Usage:
    python3 genBlockVerilog.py [--out blockMatch.v]
"""

import argparse
import json
import os
import re
from collections import defaultdict

from blockMatch import CELLS, Netlist
from genVerilog import derive_port_directions

TOP_MODULE = "blockMatch"


def group_module_name(group):
    """Name a module after what blockMatch.py says the group is."""
    chains = group.get("shift_chains")
    if chains:
        serial = re.sub(r"\W", "_", chains[0]["serial_in"])
        return f"shift_reg_{serial}"
    if group.get("operand_bits"):
        return "adder_compare" if group["kind_counts"].get("EQ_REDUCE") else "adder"
    if group["kind_counts"].get("CLOCK_BUFFER"):
        return "clk_tree"
    return f"block{group['cloud']}"


def shift_bit_map(match):
    """flip-flop instance -> (register name prefix, bit index), from the
    shift chains blockMatch.py ordered."""
    bits = {}
    for group in match["groups"]:
        for chain in group.get("shift_chains", []):
            prefix = re.sub(r"\W", "_", chain["serial_in"]).lower()
            for stage in chain["stages"]:
                bits[stage["flop"]] = (prefix, stage["bit"])
    return bits


def block_bit(block, flop_bits, nl):
    """Which operand bit a block sits on: its own flop's bit if it holds
    one, else the bit of the register feeding its operand. None for blocks
    with no bit of their own (carry merges, the reduce tree)."""
    for gate in block["gates"]:
        if gate["instance"] in flop_bits:
            return flop_bits[gate["instance"]][1]
    for sym in ("a", "b"):
        net = block["signals"].get(sym, {}).get("net")
        if net is None:
            continue
        driver = nl.driver_gate(net)
        if driver is not None and driver.id in flop_bits:
            return flop_bits[driver.id][1]
    return None


def natural_key(name):
    """Sort a_reg10 after a_reg9, not after a_reg1."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def wire_names(match, nl):
    """net id -> Verilog identifier. See the module docstring for the order
    of precedence; the point of this function is that no wire in the output
    is named after a net number if blockMatch.py knew something better."""
    names = {}

    # (1) real chip ports.
    for net, port in nl.net_port.items():
        names[net] = port

    # (2) shift-register bits: the flop's Q and the D feeding it.
    flop_bits = shift_bit_map(match)
    for flop, (prefix, bit) in flop_bits.items():
        gate = nl.gates[flop]
        names.setdefault(gate.out_net, f"{prefix}_reg{bit}")
        names.setdefault(gate.ins["D"], f"{prefix}_nxt{bit}")

    # (3) roles inside matched blocks. Bit slices carry their operand's bit
    # index; the rest are numbered in topological order, which for a carry
    # chain and a reduce tree means numbered the way the signal flows.
    levels = {}
    for cloud in match["clouds"]:
        levels.update(cloud["levels"])

    role_names = {"g": "g", "p": "p", "p2": "p{bit}_dual", "x": "xsum", "xs": "xsum",
                  "sum": "xsum", "carry": "g"}
    counters = defaultdict(int)
    serial = {"CARRY_MERGE": "carry", "SUM_BIT": "sumbit",
              "EQ_REDUCE": "eq", "CLOCK_BUFFER": "clknet"}

    def block_order(block):
        return (min(levels.get(g["instance"], 99) for g in block["gates"]),
                sorted(g["instance"] for g in block["gates"]))

    for block in sorted(match["blocks"], key=block_order):
        bit = block_bit(block, flop_bits, nl)
        for gate in block["gates"]:
            net = gate["out"]
            if net is None or net in names:
                continue
            if block["kind"] in serial:
                stem = serial[block["kind"]]
                names[net] = f"{stem}{counters[stem]}"
                counters[stem] += 1
            elif gate["role"] in role_names and bit is not None:
                stem = role_names[gate["role"]]
                names[net] = stem.format(bit=bit) if "{" in stem else f"{stem}{bit}"
            else:
                names[net] = f"net_{net}"

    for net in nl.net_driver:
        names.setdefault(net, f"net_{net}")

    collisions = defaultdict(list)
    for net, name in names.items():
        collisions[name].append(net)
    dupes = {n: v for n, v in collisions.items() if len(v) > 1}
    assert not dupes, f"wire names collide (would short nets together): {dupes}"
    return names


def group_gates(match):
    """group index -> its gate instances, and the reverse map."""
    per_group, owner = {}, {}
    for gi, group in enumerate(match["groups"]):
        gates = [g["instance"] for bi in group["blocks"]
                 for g in match["blocks"][bi]["gates"]]
        per_group[gi] = gates
        for inst in gates:
            owner[inst] = gi
    return per_group, owner


def group_ports(nl, gates):
    """The nets crossing this group's boundary, split by direction. A net
    driven inside and read outside is an output (as is one reaching a chip
    port); a net driven elsewhere and read inside is an input."""
    inside = set(gates)
    inputs, outputs = set(), set()
    for inst in gates:
        gate = nl.gates[inst]
        for net in gate.ins.values():
            owner = nl.net_driver.get(net, (None, None))[0]
            if owner not in inside:
                inputs.add(net)
        if gate.out_net is None:
            continue
        outside = [o for o, _ in nl.net_loads.get(gate.out_net, []) if o not in inside]
        if outside or gate.out_net in nl.net_port:
            outputs.add(gate.out_net)
    return sorted(inputs), sorted(outputs)


def emit_instance(nl, inst, names, comment):
    """One cell instantiation, pins in the order sky130_prims.v declares
    them, annotated with the block role it plays."""
    gate = nl.gates[inst]
    spec = CELLS[gate.cls]
    conns = [f"    .{pin}({names[gate.ins[pin]]})" for pin in spec.ins if pin in gate.ins]
    if gate.out_net is not None:
        conns.append(f"    .{gate.out_pin}({names[gate.out_net]})")
    return (f"{gate.cell} u{inst.split(':', 1)[0]} (   // {comment}\n"
            + ",\n".join(conns) + "\n);")


def emit_group_module(nl, match, group, gates, names):
    """One module per discovered structure, its instances laid out block by
    block with the block's own description above them."""
    module = group_module_name(group)
    inputs, outputs = group_ports(nl, gates)
    boundary = set(inputs) | set(outputs)
    by_name = lambda nets: sorted(nets, key=lambda n: natural_key(names[n]))

    lines = [f"// {group['label']}", f"module {module} ("]
    lines.append(",\n".join([f"    input  {names[n]}" for n in by_name(inputs)]
                            + [f"    output {names[n]}" for n in by_name(outputs)]))
    lines.append(");")
    lines.append("")

    internal = by_name({nl.gates[i].out_net for i in gates
                        if nl.gates[i].out_net is not None} - boundary)
    for net in internal:
        lines.append(f"wire {names[net]};")
    if internal:
        lines.append("")

    levels = {}
    for cloud in match["clouds"]:
        levels.update(cloud["levels"])
    flop_bits = shift_bit_map(match)
    blocks = [match["blocks"][bi] for bi in group["blocks"]]

    def order(block):
        bit = block_bit(block, flop_bits, nl)
        return (bit if bit is not None else 99,
                min(levels.get(g["instance"], 99) for g in block["gates"]),
                sorted(g["instance"] for g in block["gates"]))

    # The per-kind description is worth stating once per module, not once
    # per bit -- eight identical paragraphs bury the netlist they annotate.
    explained = set()
    for block in sorted(blocks, key=order):
        bit = block_bit(block, flop_bits, nl)
        title = block["kind"] + (f" [bit {bit}]" if bit is not None else "")
        if block["kind"] in explained:
            lines.append(f"// ---- {title}")
        else:
            lines.append(f"// ---- {title} ({block['check']}): {block['note']}")
            explained.add(block["kind"])
        for gate in block["gates"]:
            lines.append(emit_instance(nl, gate["instance"], names,
                                       f"{block['kind']}.{gate['role']}"))
        lines.append("")

    lines.append(f"endmodule // {module}")
    return module, inputs, outputs, "\n".join(lines)


def emit_top(nl, names, modules, port_direction):
    """The top module: chip ports in, one instantiation per group module.
    Every boundary net already has one global name, so each connection is a
    plain .name(name).

    `port_direction` is derived from the module graph by genVerilog rather
    than hardcoded here: a fixed table would emit the warmup adder's ports
    on any design handed to it, which is exactly the bug genVerilog.py's
    docstring warns against reintroducing."""
    inputs = [p for p, d in port_direction.items() if d == "input"]
    outputs = [p for p, d in port_direction.items() if d == "output"]

    lines = [f"module {TOP_MODULE} ("]
    lines.append(",\n".join([f"    input  {p}" for p in inputs]
                            + [f"    output {p}" for p in outputs]))
    lines.append(");")
    lines.append("")

    crossing = sorted({names[n] for _, ins, outs, _ in modules for n in ins + outs}
                      - set(port_direction), key=natural_key)
    for wire in crossing:
        lines.append(f"wire {wire};")
    lines.append("")

    for module, ins, outs, _ in modules:
        ports = sorted(ins, key=lambda n: natural_key(names[n])) \
            + sorted(outs, key=lambda n: natural_key(names[n]))
        lines.append(f"{module} u_{module} (")
        lines.append(",\n".join(f"    .{names[n]}({names[n]})" for n in ports))
        lines.append(");")
        lines.append("")

    lines.append(f"endmodule // {TOP_MODULE}")
    return "\n".join(lines)


def generate(module_graph, match):
    nl = Netlist(module_graph)
    names = wire_names(match, nl)
    per_group, _ = group_gates(match)

    covered = {inst for gates in per_group.values() for inst in gates}
    missing = sorted(set(nl.gates) - covered)
    assert not missing, (f"{len(missing)} cell(s) are in no group and would be "
                         f"dropped from the netlist: {missing}")

    modules = []
    for gi, group in enumerate(match["groups"]):
        modules.append(emit_group_module(nl, match, group, per_group[gi], names))

    header = (
        "// Auto-generated by genBlockVerilog.py from BLOCK_MATCH.json\n"
        "// (+ MODULE_GRAPH.json for the gate-level wiring).\n"
        "// Same gates as adder_demo.v, regrouped into one module per structure\n"
        "// blockMatch.py identified, and rewired with the names it recovered.\n"
    )
    bodies = [body for _, _, _, body in modules]
    port_direction = derive_port_directions(module_graph)
    return "\n\n".join([header.rstrip()] + bodies
                       + [emit_top(nl, names, modules, port_direction)]) + "\n"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module-graph", default=os.path.join(here, "MODULE_GRAPH.json"))
    parser.add_argument("--block-match", default=os.path.join(here, "BLOCK_MATCH.json"))
    parser.add_argument("--out", default=os.path.join(here, "blockMatch.v"))
    args = parser.parse_args()

    with open(args.module_graph) as f:
        module_graph = json.load(f)
    with open(args.block_match) as f:
        match = json.load(f)

    verilog = generate(module_graph, match)
    with open(args.out, "w") as f:
        f.write(verilog)
    print(f"Wrote {args.out}: {len(match['groups'])} module(s) + {TOP_MODULE}, "
          f"{sum(len(b['gates']) for b in match['blocks'])} cells")


if __name__ == "__main__":
    main()
