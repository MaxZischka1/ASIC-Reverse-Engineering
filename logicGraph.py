"""
logicGraph.py -- buffer/inverter removal, then partition into combinational
blocks separated by flip-flops.

Takes MODULE_GRAPH.json (nodes=cell instances, edges=nets, one driver per
net) and produces LOGIC_GRAPH.json: the same circuit with the
physical-design noise stripped out and the remaining logic cut at every
sequential element, so each piece is a pure boolean function of primary
inputs, flip-flop outputs and constants.

STAGE 1 -- BUFFER AND INVERTER REMOVAL
======================================
Buffers (buf, clkbuf_*) compute X = A. They carry no logic information --
they exist because the physical flow needed drive strength -- but they
split one signal into two nets, add a fake node to every path through
them, and make otherwise-identical bit-slices compare unequal depending
on where the timing closure happened to need one. Removing a buffer is
splicing its output net back onto its input net, and is exactly lossless.

Inverters are NOT deleted, because ~A is real logic. They are *absorbed
into the edge*: every load pin records (source_net, inverted). This is
equally lossless -- the parity is preserved on the consumer, so the
original function is fully reconstructible -- but it means inv->inv
chains cancel to a plain wire, and a structural matcher can see through
polarity (nand2 vs and2, a21oi vs a21o) instead of treating a bubble as
a distinct node.

So every remaining pin connection in this file is a pair (net, inv), and
`resolve_source` is the one place that walks a chain of buffers and
inverters back to the first cell that actually computes something,
accumulating parity on the way.

Antenna diodes are dropped outright: the cell has an input pin and no
output, so it is a load on a net and nothing more.

STAGE 2 -- CONES (the primary structure)
========================================
Flip-flops are the boundary. After stage 1, the graph's signal sources
are primary inputs, flip-flop Q outputs and constant cells (conb); its
signal sinks are flip-flop data/set/reset pins and primary outputs.
Everything in between is combinational.

The unit of decomposition is the *cone*: one per data sink -- every flop
D pin and every primary output -- holding the transitive fan-in of gates
that feeds that sink alone, stopping at flops, constants and input
ports. A cone is "the boolean function that lands in this one register
bit", which is what word-grouping and functional classification work on.

Cones overlap, on purpose: a gate fanning out to five flops appears in
all five cones because it genuinely participates in all five functions.
Nothing here deduplicates that. Gate detail lives once in the top-level
`cells` map; cones carry instance ids and join against it.

`gate_to_cones` is that same overlap read backwards -- instance id to the
cone ids it feeds -- so "what else depends on this gate" is a lookup
rather than a scan. Note that summing cone sizes therefore exceeds the
gate count (a gate in 22 cones contributes 22); that is shared logic
being counted per consumer, never duplicated cells.

`blocks` is a connected-component partition of the combinational cells
(two cells joined if one drives the other) and is kept for DIAGNOSTICS
ONLY -- nothing downstream consumes it. It is a true partition, which is
exactly why it does not decompose anything useful here: a single shared
net pulls most of the datapath into one component. That is the expected
behaviour of a partition, not a defect, and it is not worth "fixing" --
the overlapping cones are the answer instead.

The clock network is kept out of all of this. CLK pins are not treated as
block inputs (the clock is not data), so the buffer tree collapsing onto
a single clk net does not merge unrelated logic. If a clock pin turns out
to be driven by a gate rather than a port or buffer tree -- a gated clock
-- that is reported rather than silently folded into a block.

Usage:
    python3 logicGraph.py [MODULE_GRAPH.json] [--out LOGIC_GRAPH.json]
                          [--verilog puzzleReduced.v]
"""

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict


# --------------------------------------------------------------------------
# Cell classification
# --------------------------------------------------------------------------

def cell_class(cell_type):
    """Drop the drive-strength suffix (same convention as moduleGraph.py):
    clkbuf_4/_8/_16 are one cell as far as function goes."""
    return re.sub(r"_\d+$", "", cell_type)


def classify_cells(verilog_path):
    """Classify every cell in sky130_prims.v by what its behavioural body
    actually does, rather than by matching cell names against a hand-kept
    list -- a name list silently misclassifies any cell nobody thought of,
    and a misclassified buffer is a wrong netlist, not a warning.

    Returns {cell_class: {"kind", "inputs", "outputs", "expr"}} where kind
    is one of buf / inv / seq / const / sink / comb.
    """
    with open(verilog_path) as f:
        text = f.read()

    info = {}
    for cell_type, port_list, body in re.findall(
        r"module\s+(\w+)\s*\((.*?)\);(.*?)endmodule", text, re.DOTALL
    ):
        inputs, outputs = [], []
        for kind, pin in re.findall(r"\b(input|output)\s+(?:reg\s+)?(\w+)", port_list):
            (inputs if kind == "input" else outputs).append(pin)

        body = body.strip()
        if "always" in body:
            kind = "seq"
        elif not outputs:
            kind = "sink"          # antenna diode: a load and nothing else
        elif not inputs:
            kind = "const"         # conb: ties for logic 0 / logic 1
        elif len(inputs) == 1 and len(outputs) == 1:
            one = re.fullmatch(
                r"assign\s+(\w+)\s*=\s*(~?)\s*(\w+)\s*;", body
            )
            if one and one.group(1) == outputs[0] and one.group(3) == inputs[0]:
                kind = "inv" if one.group(2) else "buf"
            else:
                kind = "comb"
        else:
            kind = "comb"

        cls = cell_class(cell_type)
        entry = {"kind": kind, "inputs": inputs, "outputs": outputs}
        if info.setdefault(cls, entry) != entry:
            raise ValueError(
                f"{cell_type}: drive-strength variants of {cls} disagree "
                f"({info[cls]} vs {entry})"
            )
    return info


# --------------------------------------------------------------------------
# Netlist view of MODULE_GRAPH.json
# --------------------------------------------------------------------------

class Netlist:
    """Flat, driver-indexed view of the module graph: what drives each net,
    what loads it, and which net each instance pin sits on."""

    def __init__(self, module_graph, cell_info):
        self.cell_info = cell_info
        self.cell_of = {}       # instance id -> cell type ("" for ports)
        self.is_port = {}
        for n in module_graph["nodes"]:
            self.cell_of[n["id"]] = n["cell"]
            self.is_port[n["id"]] = n["port"]

        self.driver = {}                       # net -> (instance, pin)
        self.loads = defaultdict(list)         # net -> [(instance, pin)]
        self.pin_net = defaultdict(dict)       # instance -> {pin: net}
        self.net_port = {}                     # net -> top-level port name
        for e in module_graph["edges"]:
            net = e["net"]
            if e["port"]:
                self.net_port[net] = e["port"]
            for p in e["pins"]:
                key = (p["instance"], p["pin"])
                if p["direction"] == "driver":
                    if net in self.driver:
                        raise ValueError(f"net {net}: two drivers")
                    self.driver[net] = key
                else:
                    self.loads[net].append(key)
                # A cell may tie two of its own pins to one net, so this is a
                # pin->net map, never a net->pin one.
                self.pin_net[p["instance"]][p["pin"]] = net

    def kind(self, inst):
        """buf / inv / seq / const / sink / comb / port."""
        if self.is_port[inst]:
            return "port"
        return self.cell_info[cell_class(self.cell_of[inst])]["kind"]

    def inputs_of(self, inst):
        if self.is_port[inst]:
            return []
        return self.cell_info[cell_class(self.cell_of[inst])]["inputs"]


# --------------------------------------------------------------------------
# Stage 1: remove buffers, absorb inverters
# --------------------------------------------------------------------------

class Reducer:
    """Walks nets back through buffers and inverters to the first net whose
    driver actually computes something, carrying inversion parity."""

    def __init__(self, netlist):
        self.nl = netlist
        self._memo = {}
        self.buffers_removed = set()
        self.inverters_removed = set()

    def resolve(self, net):
        """net -> (root_net, inverted). root_net's driver is a gate, flop,
        constant or primary input -- never a buffer or inverter."""
        if net in self._memo:
            return self._memo[net]

        chain, inv, cur = [], False, net
        seen = set()
        while True:
            if cur in seen:
                raise ValueError(
                    f"combinational loop through buffers/inverters at net {cur}"
                )
            seen.add(cur)
            if cur in self._memo:                 # spliced onto a known root
                root, extra = self._memo[cur]
                inv ^= extra
                cur = root
                break
            drv = self.nl.driver.get(cur)
            if drv is None:
                break                             # undriven; leave as a root
            inst = drv[0]
            k = self.nl.kind(inst)
            if k not in ("buf", "inv"):
                break
            chain.append(cur)
            if k == "inv":
                inv ^= True
                self.inverters_removed.add(inst)
            else:
                self.buffers_removed.add(inst)
            cur = self.nl.pin_net[inst][self.nl.inputs_of(inst)[0]]

        # Memoise every net on the way down, with the parity from *that*
        # net onward rather than the total -- a shared buffer tree is walked
        # once no matter how many pins hang off it.
        result = (cur, inv)
        self._memo[net] = result
        running = inv
        for hop in chain:
            self._memo[hop] = (cur, running)
            drv_inst = self.nl.driver[hop][0]
            if self.nl.kind(drv_inst) == "inv":
                running ^= True
        return result


# --------------------------------------------------------------------------
# Stage 2: combinational blocks
# --------------------------------------------------------------------------

class DSU:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# Flip-flop pin roles, read off the sky130 sequential cells: everything that
# is not the clock or the data input is an async control input.
FF_DATA_PIN = "D"
FF_CLOCK_PIN = "CLK"


def build_logic_graph(netlist, reducer):
    nl, rd = netlist, reducer

    live = [i for i in nl.cell_of
            if not nl.is_port[i] and nl.kind(i) in ("comb", "seq", "const")]
    comb = [i for i in live if nl.kind(i) == "comb"]
    flops = [i for i in live if nl.kind(i) == "seq"]
    consts = [i for i in live if nl.kind(i) == "const"]
    diodes = [i for i in nl.cell_of
              if not nl.is_port[i] and nl.kind(i) == "sink"]

    # ---- resolved input connections for every live instance ----------------
    # conns[inst][pin] = {"net": root, "inv": bool}
    conns = defaultdict(dict)
    for inst in live:
        for pin in nl.inputs_of(inst):
            net = nl.pin_net[inst].get(pin)
            if net is None:
                continue
            root, inv = rd.resolve(net)
            conns[inst][pin] = {"net": root, "inv": inv}

    # ---- who drives each surviving root net --------------------------------
    root_driver = {}                    # root net -> (instance, pin)
    for net, (inst, pin) in nl.driver.items():
        if nl.kind(inst) in ("buf", "inv"):
            continue
        root_driver[net] = (inst, pin)

    def source_of(root):
        """Describe what feeds a root net, in block-boundary terms."""
        drv = root_driver.get(root)
        if drv is None:
            port = nl.net_port.get(root)
            return {"kind": "undriven", "net": root, "port": port}
        inst, pin = drv
        if nl.is_port[inst]:
            return {"kind": "input_port", "net": root,
                    "port": inst.split(":", 1)[1]}
        k = nl.kind(inst)
        if k == "seq":
            return {"kind": "flop", "net": root, "instance": inst, "pin": pin}
        if k == "const":
            return {"kind": "const", "net": root, "instance": inst, "pin": pin,
                    "value": 1 if pin == "HI" else 0}
        return {"kind": "gate", "net": root, "instance": inst, "pin": pin}

    # ---- clock network -----------------------------------------------------
    clock_sources = defaultdict(list)
    for ff in flops:
        net = nl.pin_net[ff].get(FF_CLOCK_PIN)
        root, inv = rd.resolve(net)
        clock_sources[(root, inv)].append(ff)
    gated_clocks = [
        {"net": root, "inverted": inv, "source": source_of(root), "flops": ffs}
        for (root, inv), ffs in clock_sources.items()
        if source_of(root)["kind"] == "gate"
    ]

    # ---- sinks: where combinational results land ---------------------------
    # sinks_by_net covers every consumer of a combinational result, including
    # flop control pins; data_sinks is the subset that roots a cone -- flop D
    # pins and primary outputs, i.e. the places a combinational function is
    # actually delivered.
    sinks_by_net = defaultdict(list)
    data_sinks = []
    for ff in sorted(flops):
        for pin, c in conns[ff].items():
            if pin == FF_CLOCK_PIN:
                continue
            rec = {
                "kind": "flop_data" if pin == FF_DATA_PIN else "flop_control",
                "instance": ff, "pin": pin, "inv": c["inv"],
            }
            sinks_by_net[c["net"]].append(rec)
            if pin == FF_DATA_PIN:
                data_sinks.append((c["net"], rec))
    out_ports = {}
    for net, port in sorted(nl.net_port.items()):
        drv = nl.driver.get(net)
        if drv and not nl.is_port[drv[0]]:
            root, inv = rd.resolve(net)
            out_ports[port] = {"net": root, "inv": inv}
            rec = {"kind": "output_port", "port": port, "inv": inv}
            sinks_by_net[root].append(rec)
            data_sinks.append((root, rec))

    # ---- cones: the primary structure --------------------------------------
    cones = build_cones(nl, conns, root_driver, data_sinks, source_of)

    # ---- gate -> cones: the same relation read the other way ---------------
    # Inverted from `cones` right here rather than accumulated separately, so
    # the two views cannot disagree. A gate listing N cones is one physical
    # cell feeding N sinks, not N copies of anything. Every combinational
    # cell gets an entry, empty list included, so lookups never KeyError.
    gate_to_cones = {g: [] for g in sorted(comb)}
    for c in cones:
        for g in c["gates"]:
            gate_to_cones[g].append(c["id"])
    gate_to_cones = {g: sorted(ids) for g, ids in gate_to_cones.items()}

    # ---- blocks: diagnostics only ------------------------------------------
    # Two gates are in the same block iff one drives the other. Ports, flops,
    # constants and clock pins are boundaries and never merge blocks. This is
    # a true partition, which is exactly why it is not the decomposition:
    # one shared net pulls the whole datapath into a single component. Kept
    # for reporting connectivity, consumed by nothing downstream.
    dsu = DSU()
    for g in comb:
        dsu.find(g)
    for g in comb:
        for pin, c in conns[g].items():
            drv = root_driver.get(c["net"])
            if drv and nl.kind(drv[0]) == "comb":
                dsu.union(drv[0], g)

    members = defaultdict(list)
    for g in comb:
        members[dsu.find(g)].append(g)

    block_of = {}
    blocks = []
    for bi, root in enumerate(sorted(members, key=lambda r: -len(members[r]))):
        for g in members[root]:
            block_of[g] = bi

    for bi, root in enumerate(sorted(members, key=lambda r: -len(members[r]))):
        gates = sorted(members[root])
        gateset = set(gates)

        # inputs: every root net entering the block from outside it
        inputs = defaultdict(list)
        for g in gates:
            for pin, c in conns[g].items():
                drv = root_driver.get(c["net"])
                if drv and drv[0] in gateset:
                    continue                      # internal wire
                inputs[c["net"]].append({"instance": g, "pin": pin,
                                         "inv": c["inv"]})

        # outputs: root nets driven inside the block that leave it
        outputs = []
        internal = []
        for g in gates:
            for pin in nl.cell_info[cell_class(nl.cell_of[g])]["outputs"]:
                net = nl.pin_net[g].get(pin)
                if net is None:
                    continue
                external = list(sinks_by_net.get(net, []))
                fed_gates = [ld for ld in nl.loads.get(net, [])]
                if external:
                    outputs.append({"net": net, "driver": g, "pin": pin,
                                    "destinations": external})
                elif fed_gates:
                    internal.append(net)
                else:
                    outputs.append({"net": net, "driver": g, "pin": pin,
                                    "destinations": []})   # dangling

        blocks.append({
            "id": f"B{bi}",
            "num_gates": len(gates),
            "gates": gates,                  # join against "cells" for detail
            "inputs": [{"source": source_of(net), "loads": lds}
                       for net, lds in sorted(inputs.items())],
            "outputs": outputs,
            "internal_nets": sorted(set(internal)),
            "depth": block_depth(gates, conns, root_driver, gateset),
        })

    # ---- flip-flops --------------------------------------------------------
    cone_by_flop = {c["sink"]["instance"]: c for c in cones
                    if c["sink"]["kind"] == "flop_data"}
    flop_records = []
    for ff in sorted(flops):
        q_net = nl.pin_net[ff].get("Q")
        cone = cone_by_flop.get(ff)
        flop_records.append({
            "instance": ff, "cell": nl.cell_of[ff], "q_net": q_net,
            "pins": conns[ff],
            "d_cone": cone["id"] if cone else None,
            "block": block_for_net(conns[ff].get(FF_DATA_PIN, {}).get("net"),
                                   root_driver, block_of),
        })

    # ---- flop -> flop connectivity, one hop of combinational logic ---------
    # Taken from the cones, not the blocks: this is per-flop, so it says which
    # flops actually feed this one rather than which flops touch the same
    # component.
    seq_edges = [
        {
            "cone": c["id"],
            "to": c["sink"].get("instance") or f"PORT:{c['sink']['port']}",
            "from_flops": c["source_flops"],
            "from_ports": c["source_ports"],
        }
        for c in cones
    ]

    return {
        "reduction": {
            "buffers_removed": sorted(rd.buffers_removed),
            "inverters_removed": sorted(rd.inverters_removed),
            "diodes_dropped": sorted(diodes),
            "num_buffers_removed": len(rd.buffers_removed),
            "num_inverters_removed": len(rd.inverters_removed),
            "num_diodes_dropped": len(diodes),
            "inverted_connections": sum(
                1 for inst in conns for c in conns[inst].values() if c["inv"]
            ),
        },
        "ports": {
            "inputs": sorted(
                i.split(":", 1)[1] for i in nl.cell_of
                if nl.is_port[i] and any(
                    nl.driver.get(n, (None, None))[0] == i
                    for n in nl.pin_net[i].values())
            ),
            "outputs": out_ports,
        },
        "clock_network": {
            "roots": [{"net": root, "inverted": inv,
                       "source": source_of(root), "num_flops": len(ffs)}
                      for (root, inv), ffs in sorted(
                          clock_sources.items(), key=lambda kv: -len(kv[1]))],
            "gated_clocks": gated_clocks,
        },
        "constants": [{"instance": c, "cell": nl.cell_of[c],
                       "nets": {p: nl.pin_net[c].get(p) for p in
                                nl.cell_info[cell_class(nl.cell_of[c])]["outputs"]}}
                      for c in sorted(consts)],
        # The reduced netlist itself: every live cell with its resolved
        # (net, inv) inputs. Cones and blocks list instance ids and join
        # against this, so a gate in five cones is still stored once.
        "cells": {
            inst: {
                "cell": nl.cell_of[inst],
                "kind": nl.kind(inst),
                "inputs": conns[inst],
                "outputs": {p: nl.pin_net[inst].get(p) for p in
                            nl.cell_info[cell_class(nl.cell_of[inst])]["outputs"]},
            }
            for inst in sorted(live)
        },
        "flops": flop_records,
        "cones": cones,
        "gate_to_cones": gate_to_cones,
        "sequential_edges": seq_edges,
        "blocks": blocks,
        "stats": {
            "cells_in": sum(1 for i in nl.cell_of if not nl.is_port[i]),
            "cells_out": len(comb) + len(flops) + len(consts),
            "num_comb_cells": len(comb),
            "num_flops": len(flops),
            "num_constants": len(consts),
            "num_cones": len(cones),
            "largest_cone": max((c["num_gates"] for c in cones), default=0),
            "max_cone_depth": max((c["depth"] for c in cones), default=0),
            # Sum of the cone rosters, NOT a gate count: a gate feeding N
            # cones contributes N. Always >= num_comb_cells when logic is
            # shared; the design still has num_comb_cells gates.
            "sum_of_cone_sizes": sum(c["num_gates"] for c in cones),
            "shared_gates": sum(1 for ids in gate_to_cones.values()
                                if len(ids) > 1),
            "max_cones_per_gate": max((len(ids) for ids in
                                       gate_to_cones.values()), default=0),
            "num_blocks": len(blocks),
            "largest_block": max((b["num_gates"] for b in blocks), default=0),
            "max_block_depth": max((b["depth"] for b in blocks), default=0),
        },
    }


def block_for_net(net, root_driver, block_of):
    drv = root_driver.get(net) if net else None
    if drv and drv[0] in block_of:
        return f"B{block_of[drv[0]]}"
    return None


def block_depth(gates, conns, root_driver, gateset):
    """Longest chain of gates inside the block (block inputs are level 0)."""
    level = {}

    def depth(g):
        if g in level:
            return level[g]
        level[g] = 0                       # guards against a comb loop
        best = 0
        for c in conns[g].values():
            drv = root_driver.get(c["net"])
            if drv and drv[0] in gateset:
                best = max(best, depth(drv[0]) + 1)
        level[g] = best
        return best

    return max((depth(g) for g in gates), default=0)


def build_cones(nl, conns, root_driver, data_sinks, source_of):
    """THE primary structure. One cone per data sink -- every flop D pin and
    every primary output -- holding the transitive fan-in of combinational
    gates that feeds that sink alone, stopping at flops, constants and input
    ports.

    Cones overlap, deliberately and heavily: a gate that fans out to five
    flops appears in all five cones, because it really does participate in
    all five functions. That overlap is the point, and nothing here tries to
    deduplicate it -- a cone is "the function landing in this one register
    bit", which is what word-grouping and functional classification need.

    Computed straight off the reduced netlist. It does not consult the block
    partition, and does not need to.
    """
    cones = []
    for net, sink in data_sinks:
        gates, sources, stack = set(), {}, []
        drv = root_driver.get(net)
        if drv and nl.kind(drv[0]) == "comb":
            stack.append(drv[0])
        else:
            # Sink fed straight from a flop, constant or input port with no
            # logic in between -- a real, empty cone, not a missing one.
            sources[net] = source_of(net)

        while stack:
            cur = stack.pop()
            if cur in gates:
                continue
            gates.add(cur)
            for c in conns[cur].values():
                d = root_driver.get(c["net"])
                if d and nl.kind(d[0]) == "comb":
                    stack.append(d[0])
                else:
                    sources[c["net"]] = source_of(c["net"])

        if sink["kind"] == "flop_data":
            cone_id = f"u{sink['instance'].split(':', 1)[0]}.{sink['pin']}"
        else:
            cone_id = f"port:{sink['port']}"

        levels = cone_levels(gates, conns, root_driver)
        src_list = [sources[k] for k in sorted(sources)]
        cones.append({
            "id": cone_id,
            "sink": sink,
            "root_net": net,
            "num_gates": len(gates),
            "depth": (max(levels.values()) + 1) if levels else 0,
            "gates": sorted(gates),
            "sources": src_list,
            "source_flops": sorted({s["instance"] for s in src_list
                                    if s["kind"] == "flop"}),
            "source_ports": sorted({s["port"] for s in src_list
                                    if s["kind"] == "input_port"}),
            "source_consts": sorted({s["instance"] for s in src_list
                                     if s["kind"] == "const"}),
            "cell_histogram": dict(sorted(Counter(
                cell_class(nl.cell_of[g]) for g in gates).items())),
            "shape": cone_shape(gates, levels, nl),
        })
    return cones


def cone_levels(gates, conns, root_driver):
    """Level of each gate in a cone: 0 if all its inputs come from outside
    the cone (flops/ports/constants), otherwise one past its deepest input."""
    level = {}

    def lvl(g):
        if g in level:
            return level[g]
        level[g] = 0                        # also guards a combinational loop
        best = 0
        for c in conns[g].values():
            d = root_driver.get(c["net"])
            if d and d[0] in gates:
                best = max(best, lvl(d[0]) + 1)
        level[g] = best
        return best

    for g in gates:
        lvl(g)
    return level


def cone_shape(gates, levels, nl):
    """A cheap grouping key: the multiset of (level, cell class) over the
    cone, hashed. Two cones that are the same bit-slice of a wide function
    get the same shape; two cones with the same shape are very likely but
    NOT provably the same function. Use it to prefilter candidate groups,
    then confirm properly -- it is not a canonical graph form and does not
    claim to be."""
    key = ";".join(sorted(f"{levels[g]}:{cell_class(nl.cell_of[g])}"
                          for g in gates))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Reduced Verilog, for equivalence-checking the reduction
# --------------------------------------------------------------------------

def emit_verilog(netlist, reducer, graph, module_name="puzzle"):
    """Re-emit the reduced netlist as Verilog so the transformation can be
    checked against the original with the existing testbench: buffers gone,
    inverters expressed as ~ on the consuming port connection. If this
    reproduces the reference waveform, the reduction preserved the function.
    """
    nl, rd = netlist, reducer

    port_of_input_net = {}
    for inst in nl.cell_of:
        if not nl.is_port[inst]:
            continue
        name = inst.split(":", 1)[1]
        for net in nl.pin_net[inst].values():
            drv = nl.driver.get(net)
            if drv and drv[0] == inst:
                port_of_input_net[net] = name

    def wire(net):
        return port_of_input_net.get(net, f"net_{net}")

    def expr(conn):
        return ("~" if conn["inv"] else "") + wire(conn["net"])

    live = [i for i in sorted(nl.cell_of)
            if not nl.is_port[i] and nl.kind(i) in ("comb", "seq", "const")]

    used = set()
    extra_wires = set()
    body = []
    for inst in live:
        cell = nl.cell_of[inst]
        info = nl.cell_info[cell_class(cell)]
        args = []
        for pin in info["inputs"]:
            net = nl.pin_net[inst].get(pin)
            if net is None:
                continue
            root, inv = rd.resolve(net)
            used.add(root)
            args.append(f"    .{pin}({('~' if inv else '') + wire(root)})")
        for pin in info["outputs"]:
            net = nl.pin_net[inst].get(pin)
            if net is None:
                # An output that goes nowhere (an unused conb LO, say) has no
                # net in the module graph at all -- single-instance nets are
                # dropped upstream. Give it a dangling wire of its own rather
                # than leaving the pin off the instantiation, so the file
                # still elaborates warning-clean.
                dangling = f"net_unused_u{inst.split(':', 1)[0]}_{pin}"
                extra_wires.add(dangling)
                args.append(f"    .{pin}({dangling})")
                continue
            used.add(net)
            args.append(f"    .{pin}({wire(net)})")
        body.append(f"{cell} u{inst.split(':', 1)[0]} (\n" +
                    ",\n".join(args) + "\n);")

    out_assigns = []
    for port, c in sorted(graph["ports"]["outputs"].items()):
        used.add(c["net"])
        out_assigns.append(f"assign {port} = {expr(c)};")

    # Output ports arrive as flat names; bit-sliced ones (O[3]) belong to one
    # declared bus, so regroup them by base name before declaring.
    ins = sorted(set(port_of_input_net.values()))
    outs = sorted(graph["ports"]["outputs"])
    widths = defaultdict(list)
    for p in outs:
        base, _, idx = p.partition("[")
        widths[base].append(int(idx.rstrip("]")) if idx else None)
    decl = [f"    input  {p}" for p in ins]
    for base in sorted(widths):
        bits = widths[base]
        if bits == [None]:
            decl.append(f"    output {base}")
        else:
            decl.append(f"    output [{max(bits)}:0] {base}")

    wires = sorted(set(extra_wires) |
                   {w for w in (wire(n) for n in used) if w.startswith("net_")})
    lines = [
        "// Auto-generated by logicGraph.py -- MODULE_GRAPH.json with every",
        "// buffer spliced out and every inverter absorbed into the consuming",
        "// pin as a ~. Functionally identical to puzzleNetlist.v by",
        "// construction; run it through the same testbench to prove it.",
        "",
        f"module {module_name} (",
        ",\n".join(decl),
        ");",
        "",
    ]
    lines += [f"wire {w};" for w in sorted(set(wires))]
    lines.append("")
    lines += out_assigns
    lines.append("")
    lines += body
    lines.append("")
    lines.append(f"endmodule // {module_name}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    parser.add_argument("module_graph", nargs="?",
                        default=os.path.join(here, "MODULE_GRAPH.json"))
    parser.add_argument("--prims", default=os.path.join(here, "sky130_prims.v"))
    parser.add_argument("--out", default=os.path.join(here, "LOGIC_GRAPH.json"))
    parser.add_argument("--verilog", default=None,
                        help="also write the reduced netlist as Verilog here")
    args = parser.parse_args()

    with open(args.module_graph) as f:
        module_graph = json.load(f)

    cell_info = classify_cells(args.prims)
    nl = Netlist(module_graph, cell_info)
    rd = Reducer(nl)
    graph = build_logic_graph(nl, rd)

    r, s = graph["reduction"], graph["stats"]
    print(f"Reduction: {s['cells_in']} cells -> {s['cells_out']} "
          f"({r['num_buffers_removed']} buffers spliced out, "
          f"{r['num_inverters_removed']} inverters absorbed, "
          f"{r['num_diodes_dropped']} diodes dropped); "
          f"{r['inverted_connections']} inverted connections recorded")
    print(f"Logic: {s['num_comb_cells']} combinational cells, "
          f"{s['num_flops']} flops, {s['num_constants']} constant cells")
    gated = graph["clock_network"]["gated_clocks"]
    print(f"Clock: {len(graph['clock_network']['roots'])} distinct clock "
          f"source(s); {len(gated)} gated" + (" <-- CHECK" if gated else ""))
    cones = graph["cones"]
    csz = sorted((c["num_gates"] for c in cones), reverse=True)
    if cones:
        print(f"Cones: {len(cones)} "
              f"({sum(1 for c in cones if c['sink']['kind'] == 'flop_data')} flop D, "
              f"{sum(1 for c in cones if c['sink']['kind'] == 'output_port')} "
              f"output ports); gates per cone max={csz[0]}, "
              f"median={csz[len(csz) // 2]}, min={csz[-1]}")
        print(f"       cone sizes sum to {s['sum_of_cone_sizes']} over "
              f"{s['num_comb_cells']} distinct gates -- a shared gate is "
              f"counted once per cone it feeds, not duplicated")
        busiest = max(graph["gate_to_cones"].items(), key=lambda kv: len(kv[1]))
        print(f"Sharing: {s['shared_gates']} of {s['num_comb_cells']} gates "
              f"feed more than one cone; busiest is {busiest[0]} in "
              f"{len(busiest[1])} cones (see gate_to_cones)")
        by_shape = defaultdict(list)
        for c in cones:
            by_shape[c["shape"]].append(c)
        repeated = sorted((g for g in by_shape.values() if len(g) > 1),
                          key=lambda g: (-len(g), -g[0]["num_gates"]))
        print(f"Cone shapes: {len(by_shape)} distinct, "
              f"{len(repeated)} shared by more than one cone")
        for grp in repeated:
            print(f"    {len(grp):>3} cones x {grp[0]['num_gates']:>3} gates, "
                  f"depth {grp[0]['depth']}: "
                  f"{', '.join(c['id'] for c in grp[:6])}"
                  f"{' ...' if len(grp) > 6 else ''}")
    sizes = sorted((b["num_gates"] for b in graph["blocks"]), reverse=True)
    print(f"Blocks (diagnostic only, not consumed downstream): {len(sizes)}, "
          f"sizes {sizes[:15]}, max depth {s['max_block_depth']}")

    with open(args.out, "w") as f:
        json.dump(graph, f, indent=2)
    print(f"Wrote {args.out}")

    if args.verilog:
        with open(args.verilog, "w") as f:
            f.write(emit_verilog(nl, rd, graph))
        print(f"Wrote {args.verilog}")


if __name__ == "__main__":
    main()
