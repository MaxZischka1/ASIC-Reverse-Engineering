"""
moduleGraph.py -- nodes=modules, edges=wires.

Builds a literal circuit graph from OUT.json's layout geometry: one node
per real standard-cell instance (+ one synthetic node per external
circuit port), and exactly ONE edge per signal net, each edge listing
every (instance, pin) endpoint actually on that wire. Power/ground nets
(found via VPWR/VGND/VPB/VNB TEXT labels, not a geometry heuristic) and
non-logic connective tissue (vias, TOP-level routing wire) are dropped
entirely -- vias/TOP are real physical objects but not logic, so they're
used only to reconstruct which two logic cells share a net root, then
excluded from the node set. Also dropped: nets that touch exactly one
logic instance and no external port -- these aren't wires to anything,
just intra-cell routing (e.g. the internal node between a gate's series
transistors) that the geometry extraction picks up on a signal layer
but the GDS never gave a pin label, since only real pins get one.

Deliberately NOT one edge per (cell, cell) pair sharing a net: a net
with N logic-cell fanout is ONE edge here (with an N-long `pins` list),
not itertools.combinations(N, 2) pairwise edges. Keeping the wire as a
single object (rather than exploding it into a clique) is what makes
this useful for spotting repeated bus-width structures -- shift
registers, adders, muxes banked 8-wide, etc. -- by looking at net
fanout/pin patterns directly, without first having to re-derive "which
edges actually belong to the same wire."

Reuses netGraph.py's pipeline as-is (build_shapes -> build_labels ->
union_shapes -> build_report -> match_labels_to_nets); this script only
supplies a different final assembly step over the same intermediates.

Each pin in an edge also carries a "direction": "driver" if that pin
puts the signal onto the net, "load" if it receives the signal from
the net. For standard-cell pins this comes straight from the cell's
port declaration in sky130_prims.v (the behavioral Verilog is the
source of truth, so it's parsed rather than duplicated into a table
here). PORT pins have no fixed direction of their own -- a circuit
port is a "driver" when it's the net's only source (a primary input)
and a "load" when something else on the net already drives it (a
primary output); resolved by elimination once every pin's direction
is known. A net with more than one driver pin is a labeling error
upstream, not something to silently guess at, so its PORT pins are
left with direction=None.

Usage:
    python3 moduleGraph.py [OUT.json] [--out MODULE_GRAPH.json]
"""

import argparse
import itertools
import json
import os
import re

from netGraph import (
    POWER_NAMES,
    build_labels,
    build_report,
    build_shapes,
    is_logic_cell,
    match_labels_to_nets,
    union_shapes,
)


def cell_class(cell_type):
    """Drop the drive-strength suffix: sky130_fd_sc_hd__clkbuf_4,
    _8 and _16 are one cell as far as pin directions go. Keying on the full
    name instead silently loses every pin of a variant the Verilog happens
    not to spell out, which reads downstream as an undriven net rather than
    as a missing model."""
    return re.sub(r"_\d+$", "", cell_type)


def parse_pin_directions(verilog_path):
    """Read sky130_prims.v's port declarations, e.g. `input A,` / `output
    reg Q`, into {cell_class: {pin_name: "driver"|"load"}}."""
    with open(verilog_path) as f:
        text = f.read()

    directions = {}
    for cell_type, port_list in re.findall(r"module\s+(\w+)\s*\((.*?)\);", text, re.DOTALL):
        pins = {
            pin: ("load" if kind == "input" else "driver")
            for kind, pin in re.findall(r"\b(input|output)\s+(?:reg\s+)?(\w+)", port_list)
        }
        cls = cell_class(cell_type)
        if directions.setdefault(cls, pins) != pins:
            raise ValueError(f"{cell_type}: drive-strength variants of {cls} declare "
                             f"different pins ({directions[cls]} vs {pins})")
    return directions


def build_module_graph(shapes, dsu, net_labels, net_top_labels, instance_pin_labels, pin_directions):
    """Walk every net root; for each one that survives the power/ground
    and logic-cell filtering, emit one hyperedge (a "wire" object listing
    every pin it touches) rather than a clique of pairwise edges."""
    from collections import defaultdict

    members_by_root = defaultdict(list)
    for i in range(len(shapes)):
        members_by_root[dsu.find(i)].append(i)

    nodes = {}
    edges = []
    power_nets_dropped = 0
    internal_nets_dropped = 0
    signal_nets = 0

    for root, members in members_by_root.items():
        texts = net_labels.get(root, set())
        if texts & POWER_NAMES:
            power_nets_dropped += 1
            continue

        instance_ids = sorted({shapes[i]["instance_id"] for i in members})
        logic_ids = [i for i in instance_ids if is_logic_cell(i)]
        top_port_names = sorted(net_top_labels.get(root, set()))

        if len(logic_ids) < 1:
            continue
        if len(logic_ids) == 1 and not top_port_names:
            # Touches exactly one instance and no external port -- not a
            # wire to anything else, just intra-cell routing (e.g. the
            # internal node between a gate's series transistors) that
            # geometry extraction captured but the GDS never labeled,
            # since only real pins get an A/B/X/Y-style text label.
            internal_nets_dropped += 1
            continue
        signal_nets += 1

        for inst in logic_ids:
            nodes.setdefault(inst, {"id": inst, "cell": inst.split(":", 1)[1], "port": False})

        pins = []
        for inst in logic_ids:
            cell_type = nodes[inst]["cell"]
            cell_directions = pin_directions.get(cell_class(cell_type), {})
            # instance_pin_labels[inst][root] is the set of this
            # instance's own pin names confirmed to sit on this net
            # (see netGraph.py's match_labels_to_nets) -- usually exactly
            # one name, but a cell can tie two of its own pins together.
            pin_names = sorted(instance_pin_labels.get(inst, {}).get(root, set()))
            if pin_names:
                for pin in pin_names:
                    pins.append({"instance": inst, "pin": pin, "direction": cell_directions.get(pin)})
            else:
                # Geometry confirms this instance is on the net, but no
                # label matched a specific pin -- keep the endpoint, just
                # without a pin name, rather than silently dropping it.
                pins.append({"instance": inst, "pin": None, "direction": None})

        port_pins = []
        for name in top_port_names:
            port_id = f"PORT:{name}"
            nodes.setdefault(port_id, {"id": port_id, "cell": None, "port": True})
            port_pin = {"instance": port_id, "pin": name, "direction": None}
            pins.append(port_pin)
            port_pins.append(port_pin)

        # A port has no direction of its own -- it's a "driver" only if
        # it's the net's sole source, a "load" if something else already
        # drives the net. Multiple driver pins on one net means the
        # labeling upstream is wrong; leave the port direction unresolved
        # rather than guess which driver is real.
        if port_pins:
            drivers = [p for p in pins if p["direction"] == "driver"]
            if not drivers:
                for p in port_pins:
                    p["direction"] = "driver"
            elif len(drivers) == 1:
                for p in port_pins:
                    p["direction"] = "load"

        edges.append({
            "net": str(root),
            "port": top_port_names[0] if top_port_names else None,
            "fanout": len(pins),
            "pins": pins,
        })

    return {
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "edges": edges,
        "stats": {
            "num_nodes": len(nodes),
            "num_edges": len(edges),
            "signal_nets": signal_nets,
            "power_nets_dropped": power_nets_dropped,
            "internal_nets_dropped": internal_nets_dropped,
        },
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_in = os.path.join(here, "puzzleNetlist.json")
    default_out = os.path.join(here, "MODULE_GRAPH.json")
    default_verilog = os.path.join(here, "sky130_prims.v")

    parser = argparse.ArgumentParser(
        description="Build a nodes=modules / edges=wires (one edge per net, "
                     "full pin fanout preserved) graph from an OUT.json "
                     "produced by gdsParser.py."
    )
    parser.add_argument("in_json", nargs="?", default=default_in,
                         help=f"OUT.json produced by gdsParser.py (default: {default_in})")
    parser.add_argument("--out", default=default_out,
                         help=f"where to write the module graph JSON (default: {default_out})")
    parser.add_argument("--grid", type=int, default=2000,
                         help="spatial bucket size in database units (default: 2000)")
    parser.add_argument("--verilog", default=default_verilog,
                         help=f"behavioral Verilog defining each cell's pin directions "
                              f"(default: {default_verilog})")
    args = parser.parse_args()

    with open(args.in_json) as f:
        data = json.load(f)

    shapes = build_shapes(data)
    labels = build_labels(data)
    dsu = union_shapes(shapes, grid=args.grid, structures=data["structures"])
    parent_map, nets, cell_graph = build_report(shapes, dsu)
    net_labels, net_top_labels, instance_pin_labels, unmatched_labels = match_labels_to_nets(
        labels, shapes, dsu
    )
    pin_directions = parse_pin_directions(args.verilog)

    graph = build_module_graph(
        shapes, dsu, net_labels, net_top_labels, instance_pin_labels, pin_directions
    )

    print(f"Module graph: {graph['stats']['num_nodes']} nodes "
          f"({sum(1 for n in graph['nodes'] if n['port'])} ports), "
          f"{graph['stats']['num_edges']} wires "
          f"({graph['stats']['power_nets_dropped']} power/ground nets dropped, "
          f"{graph['stats']['internal_nets_dropped']} internal-cell nets dropped)")

    fanouts = sorted((e["fanout"] for e in graph["edges"]), reverse=True)
    if fanouts:
        print(f"Fanout: max={fanouts[0]}, top 5={fanouts[:5]}")

    with open(args.out, "w") as f:
        json.dump(graph, f, indent=2)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
