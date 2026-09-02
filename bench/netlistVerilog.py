#!/usr/bin/env python3
"""netlistVerilog.py — netlist graph JSON -> clean structural Verilog.

Reads a NETLIST_GRAPH.json (from src/klayoutNetlist.py) and writes a flat
structural netlist. By default the netlist is *cleaned*: buffer-like and
inverter-like cells are removed and their nets merged, because they carry no
logical information — only polarity, which is preserved exactly.

Cleaning is a signed (parity) union-find over nets:
- A cell is spliceable iff it has exactly one signal input and one signal output
  pin. Cells whose name contains "inv" invert (parity 1); every other 1-in/1-out
  cell is a plain or clock/delay buffer (parity 0).
- Splicing unions the cell's input net with its output net at that parity. A
  union that would force a net to equal its own inverse (an inverter ring) is
  refused: the cell stays and a warning is emitted.
- After merging, each net class gets one wire, its polarity anchored at the class
  driver. Sinks needing the opposite polarity use a single `assign` bar wire.

Constant generators (0 signal inputs, >=1 signal output) are removed too; each
connected output's value is inferred from the pin name (HI/HIGH/ONE/TIE1 -> 1,
LO/LOW/ZERO/TIE0 -> 0) and consumers get the literal at the correct polarity.

This lives in bench/, not src/: it is a simulation aid, not part of stages 1-5.
"""

import argparse
import json
import re
import sys
from collections import defaultdict


class SignedDSU(object):
    """Union-find where each edge carries a parity (0 same, 1 inverted)."""

    def __init__(self, n):
        self.parent = list(range(n))
        self.parity = [0] * n

    def find(self, x):
        if self.parent[x] == x:
            return x, 0
        root, par = self.find(self.parent[x])
        self.parent[x] = root
        self.parity[x] ^= par
        return root, self.parity[x]

    def union(self, a, b, rel):
        """Declare b = a XOR rel. Returns False on parity conflict (ring)."""
        ra, pa = self.find(a)
        rb, pb = self.find(b)
        if ra == rb:
            return (pa ^ pb) == rel
        if rb < ra:
            ra, rb = rb, ra
            pa, pb = pb, pa
        self.parent[rb] = ra
        self.parity[rb] = pa ^ pb ^ rel
        return True


BUS_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]$")


def _const_polarity(pin_name):
    """1, 0, or None from a tie-cell output pin name (sky130 conb convention)."""
    up = pin_name.upper()
    if up in ("HIGH", "H", "ONE", "TIE1", "VPWR", "VDD") or "HI" in up:
        return 1
    if up in ("LOW", "L", "ZERO", "TIE0", "VGND", "VSS", "GND") or "LO" in up:
        return 0
    return None


def clean_netlist(graph, do_clean, warnings):
    """Returns (kept_instances, net_signal_names, wires, assigns, splice_counts,
    tied). net_signal_names maps net id -> verilog expression name."""
    cells = graph["cells"]
    nets = graph["nets"]
    n_nets = len(nets)

    pin_net = defaultdict(dict)
    for net in nets:
        for inst, pin, _d in net["endpoints"]:
            pin_net[inst][pin] = net["id"]
    port_by_net = defaultdict(list)
    for p in graph["ports"]:
        port_by_net[p["net"]].append(p)

    dsu = SignedDSU(n_nets)
    removed = set()
    splice_counts = defaultdict(int)
    const_net_val = {n: int(v) for n, v in graph.get("const_nets", [])}
    tied = {(i, p): int(v) for i, p, v in graph.get("tied_pins", [])}

    if do_clean:
        for inst in graph["instances"]:
            cell = cells[inst["cell"]]
            if cell["inputs"] or not cell["outputs"]:
                continue
            pins = pin_net[inst["id"]]
            vals = {}
            ok = True
            for pin in cell["outputs"]:
                net_id = pins.get(pin)
                if net_id is None:
                    continue
                v = _const_polarity(pin)
                if v is None:
                    warnings.append(
                        "%s (%s): cannot infer constant polarity of output pin %s; "
                        "instance kept" % (inst["id"], inst["cell"], pin))
                    ok = False
                    break
                vals[net_id] = v
            if ok:
                removed.add(inst["id"])
                splice_counts[inst["cell"] + " (constant)"] += 1
                const_net_val.update(vals)

    if do_clean:
        for inst in graph["instances"]:
            cell = cells[inst["cell"]]
            if len(cell["inputs"]) != 1 or len(cell["outputs"]) != 1:
                continue
            invert = 1 if "inv" in inst["cell"].lower() else 0
            pins = pin_net[inst["id"]]
            in_net = pins.get(cell["inputs"][0])
            out_net = pins.get(cell["outputs"][0])
            if out_net is None:
                removed.add(inst["id"])
                splice_counts[inst["cell"] + " (dead)"] += 1
                continue
            if in_net is None:
                tied_val = tied.get((inst["id"], cell["inputs"][0]))
                if tied_val is not None:
                    removed.add(inst["id"])
                    splice_counts[inst["cell"] + " (tied input)"] += 1
                    const_net_val[out_net] = tied_val ^ invert
                    continue
                removed.add(inst["id"])
                splice_counts[inst["cell"] + " (floating input)"] += 1
                warnings.append("%s (%s): input floating; removed — its output net "
                                "is left undriven" % (inst["id"], inst["cell"]))
                continue
            if not dsu.union(in_net, out_net, invert):
                warnings.append("%s (%s) closes an inversion ring; kept"
                                % (inst["id"], inst["cell"]))
                continue
            removed.add(inst["id"])
            splice_counts[inst["cell"]] += 1

    kept = [i for i in graph["instances"] if i["id"] not in removed]

    classes = defaultdict(list)
    for net in nets:
        root, par = dsu.find(net["id"])
        classes[root].append((net["id"], par))

    kept_ids = {i["id"] for i in kept}
    referenced = set()
    for inst in kept:
        cell = cells[inst["cell"]]
        for pin in cell["inputs"] + cell["outputs"]:
            net_id = pin_net[inst["id"]].get(pin)
            if net_id is not None:
                referenced.add(net_id)
    net_name = {}
    wires = []
    assigns = []
    bar_needed = {}

    for root, members in sorted(classes.items()):
        const_members = [(n, p) for n, p in members if n in const_net_val]
        if const_members:
            n0, p0 = const_members[0]
            v0 = const_net_val[n0]
            for n, p in const_members[1:]:
                if (const_net_val[n] ^ p) != (v0 ^ p0):
                    warnings.append("conflicting constant drivers merged on one net "
                                    "class (n%d)" % root)
            for net_id, par in members:
                rel = par ^ p0
                net_name[net_id] = "1'b%d" % (v0 ^ rel)
                for p in port_by_net.get(net_id, []):
                    if p["direction"] == "output":
                        assigns.append("assign %s = 1'b%d;"
                                       % (_ref(p["name"]), v0 ^ rel))
                    else:
                        warnings.append("input port %s is tied to a constant"
                                        % p["name"])
            continue

        driver_par = None
        for net_id, par in members:
            if any(p["direction"] == "input" for p in port_by_net.get(net_id, [])):
                driver_par = par
                break
        if driver_par is None:
            for net_id, par in members:
                net = nets[net_id]
                if any(d == "output" and inst in kept_ids
                       for inst, _pin, d in net["endpoints"]):
                    driver_par = par
                    break
        if driver_par is None:
            driver_par = members[0][1]
            warnings.append("net class of n%d has no driver" % root)

        base = None
        for net_id, par in members:
            if par == driver_par and port_by_net.get(net_id):
                base = port_by_net[net_id][0]["name"]
                break
        is_port_name = base is not None
        if base is None:
            base = "n%d" % min(m[0] for m in members)
            if any(m[0] in referenced for m in members):
                wires.append(base)

        for net_id, par in members:
            rel = par ^ driver_par
            if rel == 0:
                net_name[net_id] = base
            elif net_id in referenced:
                bar = _bar_name(base)
                if bar not in bar_needed:
                    bar_needed[bar] = base
                net_name[net_id] = bar
            for p in port_by_net.get(net_id, []):
                if p["name"] == base and rel == 0:
                    continue
                if p["direction"] == "output":
                    expr = base if rel == 0 else "~" + base
                    assigns.append("assign %s = %s;" % (_ref(p["name"]), expr))
                elif not (is_port_name and rel == 0):
                    warnings.append("input port %s aliases net %s (parity %d)"
                                    % (p["name"], base, rel))
                    assigns.append("assign %s = %s;"
                                   % (base, (_ref(p["name"]) if rel == 0
                                             else "~" + _ref(p["name"]))))

    for bar, base in sorted(bar_needed.items()):
        wires.append(bar)
        assigns.append("assign %s = ~%s;" % (bar, _ref(base)))

    return kept, net_name, wires, assigns, splice_counts, tied


def _bar_name(base):
    m = BUS_RE.match(base)
    if m:
        return "%s_%s_bar" % (m.group(1), m.group(2))
    return base + "_bar"


def _ref(name):
    return name


def port_declarations(ports, warnings):
    """Group name[idx] labels into vectors. Returns [(direction, decl, name)]."""
    scalars = []
    buses = defaultdict(dict)
    for p in ports:
        m = BUS_RE.match(p["name"])
        if m:
            buses[m.group(1)][int(m.group(2))] = p["direction"]
        else:
            scalars.append((p["direction"], p["name"]))
    decls = []
    for name in sorted(buses):
        idxs = sorted(buses[name])
        dirs = set(buses[name].values())
        if len(dirs) > 1:
            warnings.append("bus %s mixes directions %r" % (name, dirs))
        if idxs != list(range(idxs[0], idxs[-1] + 1)):
            warnings.append("bus %s has gaps: %r" % (name, idxs))
        decls.append((sorted(dirs)[0], "[%d:%d] %s" % (idxs[-1], idxs[0], name), name))
    for direction, name in sorted(scalars, key=lambda s: s[1]):
        decls.append((direction, name, name))
    return decls


def emit_verilog(graph, module_name, do_clean, warnings):
    kept, net_name, wires, assigns, splice_counts, tied = clean_netlist(
        graph, do_clean, warnings)

    decls = port_declarations(graph["ports"], warnings)
    port_names = [d[2] for d in decls]
    bus_or_port_names = set(port_names)
    for p in graph["ports"]:
        bus_or_port_names.add(p["name"])

    lines = []
    lines.append("// generated by bench/netlistVerilog.py — flat cell-level netlist"
                 + (" (buffers/inverters cleaned)" if do_clean else ""))
    lines.append("module %s (" % module_name)
    lines.append("    " + ",\n    ".join(port_names))
    lines.append(");")
    for direction, decl, _name in decls:
        lines.append("  %s %s;" % (direction, decl))
    lines.append("")
    for w in sorted(set(wires) - bus_or_port_names):
        lines.append("  wire %s;" % w)
    lines.append("")
    for a in sorted(set(assigns)):
        lines.append("  " + a)
    if assigns:
        lines.append("")

    pin_net = defaultdict(dict)
    for net in graph["nets"]:
        for inst, pin, _d in net["endpoints"]:
            pin_net[inst][pin] = net["id"]

    for inst in kept:
        cell = graph["cells"][inst["cell"]]
        conns = []
        for pin in cell["inputs"] + cell["outputs"]:
            net_id = pin_net[inst["id"]].get(pin)
            if net_id is not None:
                conns.append(".%s(%s)" % (pin, net_name[net_id]))
            elif (inst["id"], pin) in tied:
                conns.append(".%s(1'b%d)" % (pin, tied[(inst["id"], pin)]))
            else:
                warnings.append("%s.%s unconnected" % (inst["id"], pin))
                conns.append(".%s()" % pin)
        lines.append("  %s %s ( %s );" % (inst["cell"], inst["id"], ", ".join(conns)))
    lines.append("endmodule")
    return "\n".join(lines) + "\n", kept, splice_counts


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Emit clean structural Verilog from a netlist graph JSON.")
    ap.add_argument("graph_json", nargs="?", default="out/NETLIST_GRAPH.json")
    ap.add_argument("--out", default="bench/netlistVerilog.v")
    ap.add_argument("--module-name", default="netlistVerilog")
    ap.add_argument("--no-clean", action="store_true",
                    help="keep buffers and inverters as instances")
    args = ap.parse_args(argv)

    with open(args.graph_json) as f:
        graph = json.load(f)

    warnings = []
    text, kept, splice_counts = emit_verilog(graph, args.module_name,
                                             not args.no_clean, warnings)
    with open(args.out, "w") as f:
        f.write(text)

    total_spliced = sum(splice_counts.values())
    print("%s: %d instances kept, %d spliced -> %s"
          % (args.graph_json, len(kept), total_spliced, args.out))
    for cell in sorted(splice_counts):
        print("  spliced %4d x %s" % (splice_counts[cell], cell))
    for w in warnings[:20]:
        print("warning:", w)
    if len(warnings) > 20:
        print("... %d more warnings" % (len(warnings) - 20))
    return 0


if __name__ == "__main__":
    sys.exit(main())
