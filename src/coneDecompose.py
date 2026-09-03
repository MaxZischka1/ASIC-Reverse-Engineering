#!/usr/bin/env python3
"""coneDecompose.py — netlist graph -> combinational logic cones.

Stage 4 of the pipeline (see CLAUDE.md). Walks backward from every sequential
element's data inputs and every primary output, collecting the combinational
logic until it reaches the nearest upstream *sequential boundary*: a register
output, a primary input, or a constant. Each such fan-in is one cone.

A cone is keyed by the net it computes (its root). Two registers fed by the same
net share one cone, and every sink that consumes it is recorded, so nothing is
duplicated and nothing is lost.

Sequential vs combinational is not guessed from names: it comes from the same
cell-function tables that generate the simulation models (genCellModels), so the
three sources can never disagree. Clock-like pins are excluded from the traversal
(a flop's CLK, a latch's GATE), which is what makes the boundary sequential
rather than merely structural.

Output (CONES.json):

{
  "cones": [
    {"id": int,
     "root_net": int,
     "sinks": [{"kind": "reg", "inst": "u5", "pin": "D"},
               {"kind": "port", "name": "OUT"}],
     "cells": ["u1", "u2"],            # topologically ordered, inputs first
     "leaves": [{"kind": "port", "name": "I", "from_cone": None},
                {"kind": "reg", "inst": "u9", "pin": "Q", "from_cone": 3},
                {"kind": "const", "value": "1", "from_cone": None},
                {"kind": "opaque", "inst": "b0", "pin": "P0", "from_cone": None},
                {"kind": "undriven", "net": 12, "from_cone": None}],
     "depth": int,                     # levels of combinational logic
     "n_cells": int, "n_leaves": int}
  ],
  "registers": [{"inst": ..., "cell": ..., "clock_pins": [...], "data_pins": [...],
                 "outputs": [...]}],
  "summary": {...},
  "warnings": [...]
}

Cells inside a cone are listed in topological order so a consumer (stage 5) can
evaluate the cone by walking the list once.

`from_cone` on a leaf is the cone that produces it: for a register output, the
cone feeding that register's data pin. It is the one leaf kind that leads
somewhere, so it is what lets a reader walk from a cone to the cones upstream of
it. Every other leaf kind — and a register whose data pin has no cone, or several
ambiguous ones — gets None, and a consumer names the net instead.
"""

import argparse
import json
import sys
from collections import defaultdict

from genCellModels import FAMILY_FUNCS, SEQ_TEMPLATES, family_of


def clock_pins_of(family):
    """Pins that make a sequential cell advance, rather than data it captures."""
    if family in ("dlclkp", "sdlclkp"):
        return {"CLK"}
    if family == "lpflow_inputisolatch":
        return {"SLEEP_B"}
    if family.startswith("dl"):        # transparent latches: level-sensitive gate
        return {"GATE", "GATE_N"}
    return {"CLK", "CLK_N"}            # edge-triggered flops


def classify(cell_name, cell_def):
    """-> "seq" | "comb" | "opaque" for one cell type."""
    if cell_def.get("blackbox"):
        return "opaque"
    fam = family_of(cell_name)
    if fam in SEQ_TEMPLATES:
        return "seq"
    if fam in FAMILY_FUNCS:
        return "comb"
    return "opaque"


class Netlist(object):
    """Indexed view of a netlist graph, for backward traversal."""

    def __init__(self, graph, warnings):
        self.graph = graph
        self.warnings = warnings
        self.cells = graph["cells"]
        self.inst_cell = {i["id"]: i["cell"] for i in graph["instances"]}
        self.kind = {name: classify(name, d) for name, d in self.cells.items()}

        self.net_of = {}                      # (inst, pin) -> net id
        self.driver = {}                      # net id -> ("cell", inst, pin)
        for net in graph["nets"]:
            for inst, pin, direction in net["endpoints"]:
                self.net_of[(inst, pin)] = net["id"]
                if direction == "output":
                    if net["id"] in self.driver:
                        warnings.append("net %d has multiple drivers" % net["id"])
                    else:
                        self.driver[net["id"]] = ("cell", inst, pin)

        self.port_of_net = {}                 # net id -> (name, direction)
        for p in graph["ports"]:
            self.port_of_net.setdefault(p["net"], []).append((p["name"], p["direction"]))

        self.const_of_net = {n: v for n, v in graph.get("const_nets", [])}
        self.tied = {(i, p): v for i, p, v in graph.get("tied_pins", [])}

    def input_nets(self, inst):
        """[(pin, source)] for a cell's signal inputs. source is a net id, or a
        ("const", v) / None when the pin is tied off or unconnected."""
        out = []
        for pin in self.cells[self.inst_cell[inst]]["inputs"]:
            net = self.net_of.get((inst, pin))
            if net is not None:
                out.append((pin, net))
            elif (inst, pin) in self.tied:
                out.append((pin, ("const", self.tied[(inst, pin)])))
            else:
                out.append((pin, None))
        return out

    def source_of(self, net):
        """What drives a net, as a leaf descriptor or a combinational cell.

        -> ("comb", inst) | ("leaf", {...})
        """
        if net in self.const_of_net:
            return ("leaf", {"kind": "const", "value": self.const_of_net[net]})
        for name, direction in self.port_of_net.get(net, []):
            if direction == "input":
                return ("leaf", {"kind": "port", "name": name})
        drv = self.driver.get(net)
        if drv is None:
            return ("leaf", {"kind": "undriven", "net": net})
        _k, inst, pin = drv
        kind = self.kind[self.inst_cell[inst]]
        if kind == "seq":
            return ("leaf", {"kind": "reg", "inst": inst, "pin": pin})
        if kind == "opaque":
            return ("leaf", {"kind": "opaque", "inst": inst, "pin": pin})
        return ("comb", inst)


# Every sequential family in SEQ_TEMPLATES names its data input `D`; the other
# data pins (SCD, SCE, DE, RESET_B, SET_B) are scan and control. Asserted against
# the templates by tests/testConeDecompose.py.
DATA_PIN = "D"


def annotate_leaf_origins(cones, registers, warnings):
    """Tag each cone leaf with the cone that produces it, where there is one.

    A cone's leaves are its boundary: primary inputs, constants, black boxes, and
    register outputs. A register output is the one kind that leads somewhere —
    back to the cone feeding that register's data pin — so tagging it lets a
    reader walk from a cone to the cones upstream of it without re-deriving the
    connectivity. Everything else gets None, and the caller falls back to naming
    the net.
    """
    cone_at = {}                        # (register instance, pin) -> cone id
    for cone in cones:
        for sink in cone["sinks"]:
            if sink["kind"] == "reg":
                cone_at[(sink["inst"], sink["pin"])] = cone["id"]

    data_pins_of = {r["inst"]: r["data_pins"] for r in registers}
    resolved = unresolved = 0
    for cone in cones:
        for leaf in cone["leaves"]:
            leaf["from_cone"] = None
            if leaf["kind"] != "reg":
                continue
            inst = leaf["inst"]
            src = cone_at.get((inst, DATA_PIN))
            if src is None:
                # No cone on D: a tied-off or unconnected data input, or a family
                # that takes its data elsewhere. One candidate is still an answer;
                # several are ambiguous, and the caller names the net instead.
                candidates = sorted({cone_at[(inst, p)]
                                     for p in data_pins_of.get(inst, [])
                                     if (inst, p) in cone_at})
                src = candidates[0] if len(candidates) == 1 else None
            leaf["from_cone"] = src
            if src is None:
                unresolved += 1
            else:
                resolved += 1
    return resolved, unresolved


def leaf_key(leaf):
    if leaf["kind"] == "port":
        return ("port", leaf["name"], "")
    if leaf["kind"] in ("reg", "opaque"):
        return (leaf["kind"], leaf["inst"], leaf["pin"])
    if leaf["kind"] == "const":
        return ("const", leaf["value"], "")
    return ("undriven", str(leaf["net"]), "")


def build_cone(nl, root_net, warnings):
    """Backward fan-in from one net. Returns (cells_topo, leaves, depth)."""
    cells = []                      # topological order, inputs first
    seen_cells = set()
    leaves = {}
    depth_of = {}
    on_stack = set()
    looped = set()

    def visit(net_or_const):
        """Returns the depth of the signal."""
        if isinstance(net_or_const, tuple):        # ("const", v) from a tied pin
            leaf = {"kind": "const", "value": net_or_const[1]}
            leaves[leaf_key(leaf)] = leaf
            return 0
        if net_or_const is None:
            leaf = {"kind": "undriven", "net": -1}
            leaves[leaf_key(leaf)] = leaf
            return 0
        net = net_or_const
        if net in depth_of:
            return depth_of[net]
        if net in on_stack:
            if net not in looped:
                looped.add(net)
                warnings.append("combinational loop through net %d; cut for cone %d"
                                % (net, root_net))
            return 0
        kind, payload = nl.source_of(net)
        if kind == "leaf":
            leaves[leaf_key(payload)] = payload
            depth_of[net] = 0
            return 0
        inst = payload
        on_stack.add(net)
        d = 0
        for _pin, src in nl.input_nets(inst):
            d = max(d, visit(src))
        on_stack.discard(net)
        if inst not in seen_cells:
            seen_cells.add(inst)
            cells.append(inst)                     # appended after its inputs
        depth_of[net] = d + 1
        return d + 1

    depth = visit(root_net)
    ordered_leaves = [leaves[k] for k in sorted(leaves)]
    return cells, ordered_leaves, depth


def decompose(graph, warnings):
    nl = Netlist(graph, warnings)

    registers = []
    roots = {}                          # root net -> [sink descriptors]

    def add_root(net, sink):
        roots.setdefault(net, []).append(sink)

    for inst in graph["instances"]:
        cell = inst["cell"]
        if nl.kind[cell] != "seq":
            continue
        fam = family_of(cell)
        clocks = clock_pins_of(fam)
        defn = nl.cells[cell]
        data_pins = [p for p in defn["inputs"] if p not in clocks]
        registers.append({"inst": inst["id"], "cell": cell,
                          "clock_pins": [p for p in defn["inputs"] if p in clocks],
                          "data_pins": data_pins,
                          "outputs": list(defn["outputs"])})
        for pin in data_pins:
            net = nl.net_of.get((inst["id"], pin))
            if net is None:
                if (inst["id"], pin) not in nl.tied:
                    warnings.append("%s.%s (sequential data input) is unconnected"
                                    % (inst["id"], pin))
                continue
            add_root(net, {"kind": "reg", "inst": inst["id"], "pin": pin})

    for p in graph["ports"]:
        if p["direction"] == "output":
            add_root(p["net"], {"kind": "port", "name": p["name"]})

    cones = []
    for root_net in sorted(roots):
        cells, leaves, depth = build_cone(nl, root_net, warnings)
        cones.append({
            "id": len(cones),
            "root_net": root_net,
            "sinks": roots[root_net],
            "cells": cells,
            "leaves": leaves,
            "depth": depth,
            "n_cells": len(cells),
            "n_leaves": len(leaves),
        })

    origins_resolved, origins_unresolved = annotate_leaf_origins(
        cones, registers, warnings)

    covered = set()
    for c in cones:
        covered.update(c["cells"])

    # Logic that feeds only clock pins is the clock tree, not dead logic: walk
    # back from every clock pin and account for it separately. Collect the clock
    # nets on the way, because the tree also has *sinks* that drive nothing —
    # clock-tree synthesis adds buffers purely as capacitive load to balance skew
    # between branches, and their outputs are intentionally left unconnected.
    clock_tree = set()
    clock_nets = set()
    for reg in registers:
        for pin in reg["clock_pins"]:
            net = nl.net_of.get((reg["inst"], pin))
            if net is None:
                continue
            cells, _leaves, _d = build_cone(nl, net, warnings)
            clock_tree.update(cells)
            clock_nets.add(net)
            for inst in cells:
                for _p, src in nl.input_nets(inst):
                    if isinstance(src, int):
                        clock_nets.add(src)

    # A cell hanging off a clock net that drives nothing is such a load cell.
    clock_loads = set()
    for inst in graph["instances"]:
        if nl.kind[inst["cell"]] != "comb" or inst["id"] in clock_tree:
            continue
        srcs = [s for _p, s in nl.input_nets(inst["id"]) if isinstance(s, int)]
        drives = any(nl.net_of.get((inst["id"], p)) is not None
                     for p in nl.cells[inst["cell"]]["outputs"])
        if srcs and not drives and all(s in clock_nets for s in srcs):
            clock_loads.add(inst["id"])
    clock_tree |= clock_loads

    comb_total = [i["id"] for i in graph["instances"]
                  if nl.kind[i["cell"]] == "comb"]
    clock_only = sorted(clock_tree - covered)
    orphan = sorted(set(comb_total) - covered - clock_tree)
    if orphan:
        warnings.append("%d combinational cells are in no cone and drive no clock "
                        "(they reach neither a register nor an output)" % len(orphan))

    depth_hist = defaultdict(int)
    size_hist = defaultdict(int)
    for c in cones:
        depth_hist[c["depth"]] += 1
        size_hist[min(c["n_cells"], 20)] += 1

    summary = {
        "cones": len(cones),
        "registers": len(registers),
        "combinational_cells": len(comb_total),
        "cells_in_cones": len(covered),
        "clock_tree_cells": len(clock_only),
        "clock_load_cells": len(clock_loads),
        "orphan_cells": len(orphan),
        "max_depth": max([c["depth"] for c in cones], default=0),
        "max_cells": max([c["n_cells"] for c in cones], default=0),
        "max_leaves": max([c["n_leaves"] for c in cones], default=0),
        "leaves_from_cone": origins_resolved,
        "leaves_without_cone": origins_unresolved,
        "depth_histogram": dict(sorted(depth_hist.items())),
    }
    return {"cones": cones, "registers": registers,
            "clock_tree_cells": clock_only, "orphan_cells": orphan,
            "summary": summary, "warnings": warnings}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("graph_json", nargs="?", default="NETLIST_GRAPH.json")
    ap.add_argument("--out", default="CONES.json")
    args = ap.parse_args(argv)

    sys.setrecursionlimit(100000)
    with open(args.graph_json) as f:
        graph = json.load(f)
    warnings = []
    result = decompose(graph, warnings)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)
        f.write("\n")

    s = result["summary"]
    print("%s: %d cones from %d registers + outputs -> %s"
          % (args.graph_json, s["cones"], s["registers"], args.out))
    print("  combinational cells: %d in cones, %d clock tree "
          "(%d skew-balancing loads), %d orphan"
          % (s["cells_in_cones"], s["clock_tree_cells"], s["clock_load_cells"],
             s["orphan_cells"]))
    print("  max depth %d, max cells/cone %d, max leaves/cone %d"
          % (s["max_depth"], s["max_cells"], s["max_leaves"]))
    print("  register leaves traced to a producing cone: %d (%d left as nets)"
          % (s["leaves_from_cone"], s["leaves_without_cone"]))
    print("  depth histogram: %s" % s["depth_histogram"])
    for w in warnings[:10]:
        print("warning:", w)
    if len(warnings) > 10:
        print("... %d more warnings (see JSON)" % (len(warnings) - 10))
    return 0


if __name__ == "__main__":
    sys.exit(main())
