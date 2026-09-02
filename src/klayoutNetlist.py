#!/usr/bin/env python3
"""klayoutNetlist.py — extract the gate-level netlist with KLayout's own engine
(pipeline stages 1-3: layout -> instances + connectivity + ports).

Instead of intersecting polygons ourselves, this hands the layout to KLayout's
connectivity extractor (LayoutToNetlist) and reads the result back. KLayout
walks the routing stack hierarchically, so the standard cells stay as
subcircuits rather than being flattened into transistors, and the pin *labels*
the sky130 cells carry in their own GDS (li1 text on 67/5, and the metal text
layers) give every pin and every primary port its real name — `CLK`, `RESET_B`,
`A`, `B`, `S` — rather than a name inferred from geometry.

No PDK LVS deck is needed, and none is used. Device recognition is what an LVS
deck is for, and a gate-level netlist does not want it: the goal is to keep the
standard cells intact, not to dissolve them into transistors. All that is
declared here is which GDS layers conduct and which vias join them.

Output schema: {top, cells, instances, ports, nets, tied_pins, const_nets,
warnings, units, extractor} — consumed directly by coneDecompose (stage 4).

Requires the `klayout` Python module. Validated against the authors' own
post-place-and-route DEF by tests/testKlayoutNetlist.py.
"""

import argparse
import json
import sys

from lefLib import is_filler, is_power_pin, parse_lef, signal_pins

DEFAULT_LEF = "lib/sky130_fd_sc_hd_merged.lef"

# The sky130 routing stack: conductors bottom to top, with the cut layer that
# joins each pair.
CONDUCTORS = [("li1", 67, 20), ("mcon", 67, 44), ("met1", 68, 20),
              ("via", 68, 44), ("met2", 69, 20), ("via2", 69, 44),
              ("met3", 70, 20), ("via3", 70, 44), ("met4", 71, 20),
              ("via4", 71, 44), ("met5", 72, 20)]

# Text layers carrying pin and port names, per conductor.
LABELS = {"li1": (67, 5), "met1": (68, 5), "met2": (69, 5), "met3": (70, 5),
          "met4": (71, 5), "met5": (72, 5)}

POWER_NETS = {"VPWR", "VGND", "VPB", "VNB", "VDD", "VSS", "VCC", "GND", "KAPWR"}

ROT = {0: (1, 0, 0, 1), 90: (0, -1, 1, 0), 180: (-1, 0, 0, -1),
       270: (0, 1, -1, 0)}


def transform_of(subcircuit, db_per_um):
    """KLayout placement -> the (a, b, c, d, dx, dy) matrix stage 3 emits."""
    trans = subcircuit.trans
    angle = int(round(trans.angle)) % 360
    if angle not in ROT:
        raise ValueError("unsupported rotation %r" % trans.angle)
    r00, r01, r10, r11 = ROT[angle]
    if trans.is_mirror():                  # mirror about x, then rotate
        r01, r11 = -r01, -r11
    disp = trans.disp
    return [r00, r01, r10, r11,
            int(round(disp.x * db_per_um)), int(round(disp.y * db_per_um))]


def build_extractor(layout, top):
    """A LayoutToNetlist wired for the sky130 routing stack, labels included."""
    import klayout.db as kdb

    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, top, []))
    present = {(layout.get_info(i).layer, layout.get_info(i).datatype)
               for i in layout.layer_indexes()}

    layers, order = {}, []
    for name, num, datatype in CONDUCTORS:
        layers[name] = l2n.make_layer(layout.layer(num, datatype), name)
        order.append(name)
    for name in order:
        l2n.connect(layers[name])                       # each layer to itself
    for lower, upper in zip(order, order[1:]):
        l2n.connect(layers[lower], layers[upper])       # metal <-> cut <-> metal
    for name, (num, datatype) in sorted(LABELS.items()):
        if (num, datatype) in present:
            text = l2n.make_text_layer(layout.layer(num, datatype),
                                       name + "_label")
            l2n.connect(layers[name], text)             # names attach to nets
    l2n.extract_netlist()
    return l2n


def extract(gds_path, macros, warnings, top_name=None):
    """-> the stage-3 netlist graph dict."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(gds_path)
    tops = [layout.cell(i).name for i in layout.each_top_cell()]
    if top_name and top_name not in tops:
        raise SystemExit("no top cell named %r (have %s)" % (top_name, tops))
    top = layout.cell(top_name) if top_name else layout.top_cell()
    if len(tops) > 1:
        warnings.append("%d top cells; used %s" % (len(tops), top.name))

    db_per_um = 1.0 / layout.dbu
    l2n = build_extractor(layout, top)
    circuit = l2n.netlist().circuit_by_name(top.name)
    if circuit is None:
        raise SystemExit("KLayout extracted no circuit for %s" % top.name)

    # --- instances: the subcircuits that are real library cells ---------------
    instances, inst_of, skipped = [], {}, {}
    for sub in circuit.each_subcircuit():
        name = sub.circuit_ref().name
        macro = macros.get(name)
        if macro is None or is_filler(macro):
            skipped[name] = skipped.get(name, 0) + 1
            continue
        inst_id = "u%d" % len(instances)
        inst_of[sub.id()] = inst_id
        instances.append({"id": inst_id, "cell": name,
                          "transform": transform_of(sub, db_per_um)})

    used = sorted({i["cell"] for i in instances})
    cells, direction_of = {}, {}
    for name in used:
        pins = signal_pins(macros[name])
        cells[name] = {
            "inputs": sorted(p.name for p in pins.values()
                             if p.direction != "OUTPUT"),
            "outputs": sorted(p.name for p in pins.values()
                              if p.direction == "OUTPUT")}
        for pin in pins.values():
            direction_of[(name, pin.name)] = (
                "output" if pin.direction == "OUTPUT" else "input")

    # --- nets: endpoints are cell pins, names come from the layout's labels ---
    nets, ports = [], []
    for net in circuit.each_net():
        label = net.expanded_name()
        named = not label.startswith("$")
        if named and label.upper() in POWER_NETS:
            continue                                    # supplies carry no signal
        endpoints = []
        for ref in net.each_subcircuit_pin():
            inst_id = inst_of.get(ref.subcircuit().id())
            if inst_id is None:
                continue                                # a via cell or a filler
            pin = ref.pin().name()
            cell = ref.subcircuit().circuit_ref().name
            if not pin or (cell, pin) not in direction_of:
                continue                                # a power pin
            endpoints.append([inst_id, pin, direction_of[(cell, pin)]])
        if not endpoints:
            continue
        net_id = len(nets)
        labels = [label] if named else []
        nets.append({"id": net_id, "endpoints": endpoints,
                     "ports": labels, "labels": labels})
        if named:
            drivers = [e for e in endpoints if e[2] == "output"]
            ports.append({"name": label,
                          "direction": "output" if drivers else "input",
                          "net": net_id})
        if len([e for e in endpoints if e[2] == "output"]) > 1:
            warnings.append("net %s has %d drivers"
                            % (label, len([e for e in endpoints
                                           if e[2] == "output"])))

    for name, count in sorted(skipped.items()):
        if not name.startswith("VIA"):
            warnings.append("skipped %d instance(s) of %s (no library macro, "
                            "or a filler)" % (count, name))

    return {
        "top": top.name,
        "units": {"meters_per_db_unit": layout.dbu * 1e-6},
        "cells": cells,
        "instances": instances,
        "ports": sorted(ports, key=lambda p: p["name"]),
        "nets": nets,
        "tied_pins": [],
        "const_nets": [],
        "pin_health": {},
        "unconnected_report": [],
        "dropped": {"skipped_structures": sum(skipped.values())},
        "unmatched_structures": [],
        "routing_structures": sorted(n for n in skipped
                                     if n.startswith("VIA")),
        "marker_structures": [],
        "extractor": "klayout",
        "warnings": warnings,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gds")
    ap.add_argument("--lef", action="append", default=None,
                    help="cell library LEF (default: %s)" % DEFAULT_LEF)
    ap.add_argument("--top", help="top cell name, if the layout has several")
    ap.add_argument("--out", default="NETLIST_GRAPH.json")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        import klayout.db                                       # noqa: F401
    except ImportError:
        raise SystemExit("this stage needs the klayout Python module: "
                         "pip install klayout")

    macros = {}
    for path in (args.lef or [DEFAULT_LEF]):
        macros.update(parse_lef(path))

    warnings = []
    graph = extract(args.gds, macros, warnings, args.top)
    with open(args.out, "w") as handle:
        json.dump(graph, handle, indent=1)

    if not args.quiet:
        print("%s: top=%s, %d instances (%d cell types), %d nets, %d ports -> %s"
              % (args.gds, graph["top"], len(graph["instances"]),
                 len(graph["cells"]), len(graph["nets"]),
                 len(graph["ports"]), args.out))
        for warning in warnings:
            print("warning: %s" % warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
