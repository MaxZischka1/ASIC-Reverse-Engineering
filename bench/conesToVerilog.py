#!/usr/bin/env python3
"""conesToVerilog.py — cone decomposition JSON -> structural Verilog.

Reads out/CONES.json (from src/coneDecompose.py) and the out/NETLIST_GRAPH.json
it was built from, and writes:

  * one module per cone, named after the cone id (cone 0 -> ``CONE0`` ...;
    ``--prefix`` / ``--start`` change the mapping), and
  * one top module (default ``conesTop``, ``--top-module`` / ``--no-top``) that
    instantiates every cone module plus the design's registers and clock tree and
    wires the whole design back together.

A cone's ``cells`` list is emitted verbatim as cell instances, wired through
their real nets from the netlist graph:

- the cone's ``root_net`` becomes the single ``output`` port (default ``Y``),
- a net driven by another cell of the same cone becomes an internal ``wire n<id>``,
- every other net a cone cell reads is a cone *leaf* -> an ``input`` port:
    port leaf   -> the port's name (bus ``I[3]`` -> ``I_3_``)
    reg leaf    -> ``<inst>_<pin>``   (e.g. a flop output ``u9_Q``)
    opaque leaf -> ``<inst>_<pin>``
    undriven    -> ``undriven_<net>``
- a constant leaf / tied pin is wired straight to ``1'b0`` / ``1'b1`` at the pin.

A depth-0 cone (root driven directly by a register or a port) emits
``assign Y = <leaf>;`` with no instances, and is not instantiated in the top
(whatever drives its root is already there).

Above each cone module is a comment naming every input and where it comes from —
the upstream cone for a register output (stage 4's ``from_cone``), the port name
for a primary input, and the net itself when neither applies. That is what makes
a single module readable on its own: an input called ``u9_Q`` is some other
cone's output, and the comment says which one. ``--no-origins`` omits it.

This lives in bench/, not src/: it is a simulation aid, not part of stages 1-5.
"""

import argparse
import json
import re
import sys
from collections import defaultdict

from netlistVerilog import port_declarations


def sanitize(name):
    """A valid Verilog identifier for a port/net label. ``I[3]`` -> ``I_3_``."""
    s = re.sub(r"[^A-Za-z0-9_$]", "_", name)
    if not s or not re.match(r"[A-Za-z_]", s[0]):
        s = "_" + s
    return s


class Uniquifier(object):
    """Hands out names, appending _2, _3, ... on collision."""

    def __init__(self, reserved=()):
        self.used = set(reserved)

    def take(self, base):
        base = base or "n"
        if base not in self.used:
            self.used.add(base)
            return base
        i = 2
        while "%s_%d" % (base, i) in self.used:
            i += 1
        name = "%s_%d" % (base, i)
        self.used.add(name)
        return name


class GraphView(object):
    """Pin/net/driver lookups over a NETLIST_GRAPH.json dict."""

    def __init__(self, graph):
        self.cells = graph["cells"]
        self.inst_cell = {i["id"]: i["cell"] for i in graph["instances"]}
        self.net_of = {}
        self.driver = {}
        for net in graph["nets"]:
            for inst, pin, direction in net["endpoints"]:
                self.net_of[(inst, pin)] = net["id"]
                if direction == "output":
                    self.driver.setdefault(net["id"], (inst, pin))
        self.in_port_of_net = {}
        self.out_ports_of_net = defaultdict(list)
        for p in graph["ports"]:
            if p["direction"] == "input":
                self.in_port_of_net.setdefault(p["net"], p["name"])
            else:
                self.out_ports_of_net[p["net"]].append(p["name"])
        self.const_of_net = {int(n): str(v) for n, v in graph.get("const_nets", [])}
        self.tied = {(i, p): str(v) for i, p, v in graph.get("tied_pins", [])}

    def pins(self, inst):
        cell = self.cells[self.inst_cell[inst]]
        return cell["inputs"], cell["outputs"]

    def trace_clock_to_port(self, net):
        """Follow a net backward through 1-in/1-out cells to an input port name."""
        seen = set()
        cur = net
        while cur is not None and cur not in seen:
            if cur in self.in_port_of_net:
                return sanitize(self.in_port_of_net[cur])
            seen.add(cur)
            drv = self.driver.get(cur)
            if drv is None:
                return None
            ins, _outs = self.pins(drv[0])
            if len(ins) != 1:
                return None
            cur = self.net_of.get((drv[0], ins[0]))
        return None


def leaf_source_name(gv, net):
    """Verilog name for a net that is a cone leaf. Returns (name, is_literal).
    Mirrors coneDecompose.Netlist.source_of."""
    if net in gv.const_of_net:
        return ("1'b%s" % gv.const_of_net[net], True)
    if net in gv.in_port_of_net:
        return (sanitize(gv.in_port_of_net[net]), False)
    drv = gv.driver.get(net)
    if drv is None:
        return ("undriven_%d" % net, False)
    inst, pin = drv
    return ("%s_%s" % (sanitize(inst), sanitize(pin)), False)


def input_origins(cone, gv, inputs, cone_name, warnings):
    """Comment lines naming where each of a cone module's inputs comes from.

    Stage 4 tags a register-output leaf with the cone feeding that register's
    data pin (`from_cone`), which is what makes a cone module navigable: an input
    port named `u9_Q` is the output of some other cone, and this says which. When
    stage 4 could not resolve one — a tied-off data pin, an ambiguous scan flop, a
    black box, a cut combinational loop — the net is named instead, which is
    always true even when it is not informative.
    """
    from_cone = {}
    for leaf in cone.get("leaves", []):
        if leaf.get("from_cone") is not None:
            from_cone[(leaf["inst"], leaf["pin"])] = leaf["from_cone"]

    lines = []
    width = max([len(p) for p, _n in inputs] or [0])
    for port, net in inputs:
        if net in gv.in_port_of_net:
            origin = "primary input %s" % gv.in_port_of_net[net]
        else:
            drv = gv.driver.get(net)
            if drv is None:
                origin = "net %d, undriven" % net
            elif (drv[0], drv[1]) in from_cone:
                origin = "%s  (register %s)" % (
                    cone_name(from_cone[(drv[0], drv[1])]), drv[0])
            else:
                origin = "net %d  (driven by %s.%s)" % (net, drv[0], drv[1])
        lines.append("//   %-*s  <-  %s" % (width, port, origin))
    return lines


def build_module(cone, gv, module_name, out_port, warnings, boundary_insts=frozenset(),
                 cone_name=None):
    """Return (text, port_info) for one cone.

    port_info = {"name", "out_port", "root_net", "inputs": [(port_name, net_id)]}

    boundary_insts: instance ids that are legitimate sequential/opaque leaves, so
    a net they drive is a boundary, not a cut combinational loop.
    """
    cone_cells = list(cone["cells"])
    cell_set = set(cone_cells)
    root = cone["root_net"]

    referenced = []
    seen = set()
    pin_net = {}
    for inst in cone_cells:
        ins, outs = gv.pins(inst)
        for pin in ins + outs:
            net = gv.net_of.get((inst, pin))
            pin_net[(inst, pin)] = net
            if net is not None and net not in seen:
                seen.add(net)
                referenced.append(net)

    names = Uniquifier(reserved={out_port})
    net_name = {}
    inputs = []
    wires = []
    assigns = []

    root_driver = gv.driver.get(root)
    root_internal = root_driver is not None and root_driver[0] in cell_set

    for net in referenced:
        if net == root:
            net_name[net] = out_port
            continue
        drv = gv.driver.get(net)
        if drv is not None and drv[0] in cell_set:
            w = names.take("n%d" % net)
            net_name[net] = w
            wires.append(w)
            continue
        if drv is not None and drv[0] not in cell_set \
                and drv[0] not in boundary_insts \
                and net not in gv.in_port_of_net \
                and net not in gv.const_of_net:
            warnings.append("%s: net %d is driven by comb cell %s outside the "
                            "cone (cut loop); exposed as an input"
                            % (module_name, net, drv[0]))
        src, is_lit = leaf_source_name(gv, net)
        if is_lit:
            net_name[net] = src
            continue
        port = names.take(src)
        net_name[net] = port
        inputs.append((port, net))

    if not root_internal:
        if root_driver is None and root not in gv.const_of_net \
                and root not in gv.in_port_of_net:
            warnings.append("%s: root net %d has no driver" % (module_name, root))
            assigns.append("assign %s = 1'bx;" % out_port)
        else:
            src, is_lit = leaf_source_name(gv, root)
            if not is_lit:
                src = names.take(src)
                inputs.append((src, root))
            assigns.append("assign %s = %s;" % (out_port, src))

    lines = []
    if cone_name is not None:
        lines.append("// %s — inputs and the cone each comes from:" % module_name)
        if inputs:
            lines.extend(input_origins(cone, gv, inputs, cone_name, warnings))
        else:
            lines.append("//   (none: this cone reads only constants)")
    lines.append("module %s (" % module_name)
    lines.append("    " + ",\n    ".join([out_port] + [p for p, _n in inputs]))
    lines.append(");")
    lines.append("  output %s;" % out_port)
    for p, _n in inputs:
        lines.append("  input %s;" % p)
    if wires:
        lines.append("")
        for w in wires:
            lines.append("  wire %s;" % w)
    if assigns:
        lines.append("")
        for a in assigns:
            lines.append("  " + a)
    lines.append("")

    for inst in cone_cells:
        cell = gv.inst_cell[inst]
        ins, outs = gv.pins(inst)
        conns = []
        for pin in ins + outs:
            net = pin_net[(inst, pin)]
            if net is not None:
                conns.append(".%s(%s)" % (pin, net_name[net]))
            elif (inst, pin) in gv.tied:
                conns.append(".%s(1'b%s)" % (pin, gv.tied[(inst, pin)]))
            else:
                warnings.append("%s: %s.%s is unconnected" % (module_name, inst, pin))
                conns.append(".%s()" % pin)
        lines.append("  %s %s ( %s );" % (cell, sanitize(inst), ", ".join(conns)))

    lines.append("endmodule")
    return "\n".join(lines), {"name": module_name, "out_port": out_port,
                              "root_net": root, "inputs": inputs}


def build_top(cones_data, graph, gv, port_infos, prefix, start, top_name, warnings):
    """Instantiate every cone module + the registers + the clock tree, and wire
    the whole design back together. Returns the module text."""
    in_label = {}
    out_label = {}
    for p in graph["ports"]:
        if p["direction"] == "input":
            in_label.setdefault(p["net"], p["name"])
        else:
            out_label.setdefault(p["net"], p["name"])

    def wname(nid):
        if nid in in_label:
            return in_label[nid]
        if nid in out_label:
            return out_label[nid]
        return "n%d" % nid

    referenced = set()
    driven = set(in_label) | set(gv.const_of_net)
    body = []

    reg_insts = [r["inst"] for r in cones_data.get("registers", [])]
    clk_insts = list(cones_data.get("clock_tree_cells", []))
    opaque_insts = sorted({l["inst"] for c in cones_data["cones"]
                           for l in c["leaves"] if l["kind"] == "opaque"})
    direct_insts = reg_insts + clk_insts + opaque_insts

    for inst in direct_insts:
        cell = gv.inst_cell[inst]
        ins, outs = gv.pins(inst)
        conns = []
        for pin in ins + outs:
            net = gv.net_of.get((inst, pin))
            if net is None:
                if (inst, pin) in gv.tied:
                    conns.append(".%s(1'b%s)" % (pin, gv.tied[(inst, pin)]))
                else:
                    conns.append(".%s()" % pin)
            elif net in gv.const_of_net:
                conns.append(".%s(1'b%s)" % (pin, gv.const_of_net[net]))
            else:
                conns.append(".%s(%s)" % (pin, wname(net)))
                referenced.add(net)
        for pin in outs:
            net = gv.net_of.get((inst, pin))
            if net is not None and net not in gv.const_of_net:
                driven.add(net)
        body.append("  %s %s ( %s );" % (cell, sanitize(inst), ", ".join(conns)))

    for cone, pi in zip(cones_data["cones"], port_infos):
        root = pi["root_net"]
        if not cone["cells"]:
            for s in cone["sinks"]:
                if s["kind"] != "port":
                    continue
                if root in in_label and s["name"] != in_label[root]:
                    body.append("  assign %s = %s;" % (s["name"], in_label[root]))
                elif root in gv.const_of_net:
                    body.append("  assign %s = 1'b%s;"
                                % (s["name"], gv.const_of_net[root]))
            continue
        mod = "%s%d" % (prefix, cone["id"] + start)
        conns = [".%s(%s)" % (pi["out_port"], wname(root))]
        referenced.add(root)
        driven.add(root)
        for pname, nid in pi["inputs"]:
            conns.append(".%s(%s)" % (pname, wname(nid)))
            referenced.add(nid)
        body.append("  %s cone_%d ( %s );" % (mod, cone["id"] + start, ", ".join(conns)))

    for r in cones_data.get("registers", []):
        for pin in r.get("clock_pins", []):
            net = gv.net_of.get((r["inst"], pin))
            if net is None or net in driven or net in in_label:
                continue
            src = gv.trace_clock_to_port(net)
            if src is not None:
                body.append("  assign %s = %s;" % (wname(net), src))
                driven.add(net)
            else:
                warnings.append("top: clock net %d (%s.%s) has no driver"
                                % (net, r["inst"], pin))

    decls = port_declarations(graph["ports"], warnings)
    port_names = [d[2] for d in decls]
    port_or_bus = set(port_names) | set(in_label.values()) | set(out_label.values())

    wires = sorted(n for n in referenced
                   if n not in in_label and n not in out_label
                   and n not in gv.const_of_net)

    lines = ["module %s (" % top_name,
             "    " + ",\n    ".join(port_names), ");"]
    for direction, decl, _name in decls:
        lines.append("  %s %s;" % (direction, decl))
    lines.append("")
    for n in wires:
        w = wname(n)
        if w not in port_or_bus:
            lines.append("  wire %s;" % w)
    lines.append("")
    lines.extend(body)
    lines.append("endmodule")
    return "\n".join(lines)


def emit(cones_data, graph, prefix, start, out_port, warnings,
         top_module="conesTop", no_top=False, no_origins=False):
    gv = GraphView(graph)
    boundary_insts = {r["inst"] for r in cones_data.get("registers", [])}
    boundary_insts |= {l["inst"] for c in cones_data["cones"]
                       for l in c["leaves"] if l["kind"] in ("reg", "opaque")}
    def cone_name(cone_id):
        return "%s%d" % (prefix, cone_id + start)

    blocks = []
    port_infos = []
    for cone in cones_data["cones"]:
        name = cone_name(cone["id"])
        text, pi = build_module(cone, gv, name, out_port, warnings, boundary_insts,
                                cone_name=None if no_origins else cone_name)
        blocks.append(text)
        port_infos.append(pi)
    if not no_top:
        blocks.append(build_top(cones_data, graph, gv, port_infos,
                                prefix, start, top_module, warnings))
    header = ("// generated by bench/conesToVerilog.py — one structural module per\n"
              "// cone, plus a top module wiring cones + registers + clock tree into\n"
              "// the whole design. module <name> == cone id.\n")
    return header + "\n\n".join(blocks) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cones_json", nargs="?", default="out/CONES.json")
    ap.add_argument("graph_json", nargs="?", default="out/NETLIST_GRAPH.json")
    ap.add_argument("--out", default="bench/ConesVerilog.v")
    ap.add_argument("--prefix", default="CONE", help="module name prefix (default CONE)")
    ap.add_argument("--start", type=int, default=0,
                    help="add this to each cone id for the module name (1 -> CONE1..)")
    ap.add_argument("--out-port", default="Y", help="name of the cone output port")
    ap.add_argument("--top-module", default="conesTop",
                    help="name of the assembled top module (default conesTop)")
    ap.add_argument("--no-top", action="store_true",
                    help="emit only the per-cone modules, no assembled top")
    ap.add_argument("--no-origins", action="store_true",
                    help="omit the comment above each cone naming where its "
                         "inputs come from")
    args = ap.parse_args(argv)

    with open(args.cones_json) as f:
        cones_data = json.load(f)
    with open(args.graph_json) as f:
        graph = json.load(f)

    warnings = []
    text = emit(cones_data, graph, args.prefix, args.start, args.out_port,
                warnings, top_module=args.top_module, no_top=args.no_top,
                no_origins=args.no_origins)
    with open(args.out, "w") as f:
        f.write(text)

    n = len(cones_data["cones"])
    print("%s + %s -> %s: %d cone module(s) %s%d..%s%d%s"
          % (args.cones_json, args.graph_json, args.out, n,
             args.prefix, args.start, args.prefix,
             (n - 1 + args.start) if n else args.start,
             "" if args.no_top else " + top module %s" % args.top_module))
    for w in warnings[:20]:
        print("warning:", w)
    if len(warnings) > 20:
        print("... %d more warnings" % (len(warnings) - 20))
    return 0


if __name__ == "__main__":
    sys.exit(main())
