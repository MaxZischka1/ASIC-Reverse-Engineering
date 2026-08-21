"""
genVerilog.py -- turn MODULE_GRAPH.json + puzzleGraph.json into a structural
Verilog netlist (puzzleNetlist.v): one module instantiation per logic-cell
node, one wire per internal net, and the design's real external ports --
however many it turns out to have -- declared on the module header.

WHERE EACH PIECE OF DATA COMES FROM
====================================
- MODULE_GRAPH.json's `nodes` -- one entry per real standard-cell instance
  (id, cell type) plus one synthetic PORT:<name> node per external port.
  Power/ground nets are already dropped entirely by moduleGraph.py's
  build_module_graph(), so nothing here needs to re-filter VPWR/VGND nets
  out -- they were never nets in this file to begin with.
- MODULE_GRAPH.json's `edges` -- only used here for the net -> port-name
  mapping (`edges[].port`, non-null exactly on the handful of nets that
  reach a real external port). Each edge's full `pins` list (every
  (instance, pin) endpoint on that wire) is NOT used here -- it carries
  no direction, so it's useless for wiring up named module ports on its
  own; that job goes to puzzleGraph.json's instance_pins instead.
- puzzleGraph.json's `instance_pins` -- instance_id -> {net_id -> {pin
  names}} -- THE piece that actually answers "which named pin does this
  specific instance use on this specific net," recovered from matching
  each instance's own GDS pin labels to the (now exact point-in-polygon,
  not bbox) shape it sits on. This is what lets us emit `.A(net_123)`
  instead of just "these two cells are connected somehow."

PER-CELL-TYPE PINOUT
=====================
Rather than hand-typing sky130_fd_sc_hd pin names from memory (risky --
several cells here, like a21bo/a21boi/o21bai/and4bb, have inverted-input
pin names like "B1_N"/"A_N" that are easy to misremember), the pinout
for every cell type actually used in this design is derived empirically
from instance_pins itself: the union of non-power pin names seen across
every instance of that type. Verified (see the conversation) that every
instance's own pin set matches its type's full set exactly -- no
instance is missing a pin -- so this LUT is complete for this design.

TOP-LEVEL PORTS: DERIVED, NOT HAND-SUPPLIED
============================================
The port list and each port's direction both come from
MODULE_GRAPH.json, not from a table here. Earlier versions hardcoded
the warmup adder's ports (A, B, S, clk, en, rst_n); on the real puzzle
that produced a module header naming six ports, four of which the
design does not have, while the thirteen ports it does have (I, clk,
enable, rst_n, O[0..7], success) went undeclared even though the
instance port maps referenced them. The netlist body was right and only
the header was wrong, which is exactly the failure a hardcoded table
produces when the design changes underneath it -- so the table is gone.

Port *names* come from the PORT:<name> nodes moduleGraph.py creates
from the GDS's top-level TEXT labels. Port *directions* come from the
`direction` field moduleGraph.py already resolves on each port pin by
elimination: a port is the "driver" of its net when no standard-cell
output pin drives that net (so the signal enters the circuit there --
a primary input), and a "load" when a cell already drives it (so the
signal leaves -- a primary output). That is a fact recoverable from
connectivity alone, which is why it does not need to be hand-supplied.
On this design all thirteen resolve unambiguously.

Bus bits are re-collapsed into vectors: the GDS labels an 8-bit output
as eight separate texts O[0]..O[7], which moduleGraph.py faithfully
keeps as eight scalar PORT nodes. Emitting eight scalar `output O[0]`
declarations is not legal Verilog, so bits sharing a base name are
regrouped into a single `output [7:0] O` and the per-bit names stay
valid as bit-selects in the instance port maps.
"""

import argparse
import json
import os
import re

POWER_NAMES = {"VPWR", "VGND", "VPB", "VNB"}

# "O[3]" -> base "O", index 3. Anything without a subscript is a scalar port.
BUS_BIT_RE = re.compile(r"^(.*?)\[(\d+)\]$")


def load_graphs(module_graph_path, net_graph_path):
    with open(module_graph_path) as f:
        module_graph = json.load(f)
    with open(net_graph_path) as f:
        net_graph = json.load(f)
    # The net graph is netGraph.py's output (puzzleGraph.json), NOT
    # gdsParser.py's raw dump (puzzleNetlist.json) -- the two are easy to
    # mix up since moduleGraph.py legitimately consumes the raw dump.
    # Fail here, naming the file, instead of 130 lines later on a bare
    # KeyError.
    if "instance_pins" not in net_graph:
        raise SystemExit(
            f"{net_graph_path} has no 'instance_pins' (keys: "
            f"{sorted(net_graph)}) -- expected netGraph.py's output "
            f"(puzzleGraph.json), not gdsParser.py's raw netlist dump."
        )
    return module_graph, net_graph


def build_pinout_lut(module_graph, instance_pins):
    """cell_type -> sorted list of every non-power pin name seen on any
    instance of that type. See module docstring for why this is derived
    from the real matched labels instead of hardcoded from memory."""
    inst_to_type = {n["id"]: n["cell"] for n in module_graph["nodes"] if n["cell"]}
    lut = {}
    for inst, by_net in instance_pins.items():
        ctype = inst_to_type.get(inst)
        if ctype is None:
            continue  # a via/TOP entry, if any ever showed up here -- not a logic cell
        pins = lut.setdefault(ctype, set())
        for pin_names in by_net.values():
            pins |= (set(pin_names) - POWER_NAMES)
    return {ctype: sorted(pins) for ctype, pins in lut.items()}


def build_net_port_map(module_graph):
    """net id -> real external port name, from the one place that fact is
    recorded: edges whose `port` field is set."""
    net_port = {}
    for e in module_graph["edges"]:
        if e["port"]:
            net_port.setdefault(e["net"], e["port"])
    return net_port


def derive_port_directions(module_graph):
    """{port_name: "input"|"output"} from the port-pin directions
    moduleGraph.py resolved by elimination. See module docstring for why
    this is derivable from connectivity rather than hand-supplied.

    "driver" means the port puts the signal onto its net -- nothing inside
    drives it -- so it is a primary input; "load" means a cell already
    drives the net and the port takes the signal away, a primary output.
    """
    directions = {}
    for e in module_graph["edges"]:
        for p in e["pins"]:
            if not p["instance"].startswith("PORT:"):
                continue
            name = p["instance"].split(":", 1)[1]
            if p["direction"] is None:
                # moduleGraph.py leaves this None only when the net had
                # multiple driver pins, i.e. the upstream labeling is
                # wrong. Guessing a direction here would bake that error
                # into a netlist that still elaborates, which is worse
                # than stopping.
                raise SystemExit(
                    f"port {name!r} (net {e['net']}) has an unresolved direction -- "
                    f"its net has multiple drivers upstream, so it is not safe to "
                    f"guess whether it is an input or an output."
                )
            direction = "input" if p["direction"] == "driver" else "output"
            if directions.setdefault(name, direction) != direction:
                raise SystemExit(
                    f"port {name!r} resolves to both input and output on different nets"
                )
    return directions


def group_ports(port_directions):
    """Regroup scalar bit labels (O[0]..O[7]) into vector declarations.

    Returns a list of (decl_string, sort_key) for the module header, in
    a stable order: inputs first, then outputs, alphabetical within each.
    """
    buses = {}      # base -> {"dir": ..., "bits": {index}}
    scalars = {}    # name -> dir
    for name, direction in port_directions.items():
        m = BUS_BIT_RE.match(name)
        if not m:
            scalars[name] = direction
            continue
        base, idx = m.group(1), int(m.group(2))
        bus = buses.setdefault(base, {"dir": direction, "bits": set()})
        if bus["dir"] != direction:
            raise SystemExit(f"bus {base!r} has bits of conflicting direction")
        bus["bits"].add(idx)

    decls = []
    for name, direction in scalars.items():
        decls.append((direction, name, f"{direction:<6} {name}"))
    for base, bus in buses.items():
        lo, hi = min(bus["bits"]), max(bus["bits"])
        # A gap here means the GDS was missing a bit's label; declaring
        # [hi:lo] anyway would silently invent a wire nothing connects to.
        missing = set(range(lo, hi + 1)) - bus["bits"]
        if missing:
            raise SystemExit(
                f"bus {base!r} is missing labels for bit(s) {sorted(missing)} -- "
                f"saw {sorted(bus['bits'])}"
            )
        decls.append((bus["dir"], base, f"{bus['dir']:<6} [{hi}:{lo}] {base}"))

    decls.sort(key=lambda d: (d[0] != "input", d[1]))
    return [d[2] for d in decls]


def instance_connections(inst, instance_pins, net_port):
    """This instance's own {pin_name: wire_name} map, built straight from
    its instance_pins entry -- skip any net whose only label here was a
    power name (that net has nothing left to connect once VPWR/VGND is
    stripped, exactly the nets you said to discount)."""
    conns = {}
    for net_id, pin_names in instance_pins.get(inst, {}).items():
        nonpower = set(pin_names) - POWER_NAMES
        if not nonpower:
            continue  # this net, for this instance, was purely a power/ground label
        # Verified empty in the driving conversation, but guard anyway
        # rather than silently pick one if a future re-run regresses it.
        assert len(nonpower) == 1, f"{inst} net {net_id}: ambiguous pins {nonpower}"
        pin = next(iter(nonpower))
        wire = net_port.get(net_id, f"net_{net_id}")
        conns[pin] = wire
    return conns


def build_touched_nodes(module_graph):
    """Instance ids that appear as an endpoint on at least one edge --
    i.e. every node module_graph's wire-level connectivity actually
    knows something about. Anything not in this set is a node with no
    edges: moduleGraph.py's own filtering (power/ground nets dropped,
    single-instance-no-port nets dropped as intra-cell routing) can
    leave a node with every one of its nets stripped out, so it never
    shows up as a pin on any edge."""
    touched = set()
    for e in module_graph["edges"]:
        for p in e["pins"]:
            touched.add(p["instance"])
    return touched


def generate_verilog(module_graph, net_graph, module_name="puzzle",
                     module_graph_name="MODULE_GRAPH.json",
                     net_graph_name="puzzleGraph.json"):
    instance_pins = net_graph["instance_pins"]
    pinout_lut = build_pinout_lut(module_graph, instance_pins)
    net_port = build_net_port_map(module_graph)
    touched_nodes = build_touched_nodes(module_graph)
    port_directions = derive_port_directions(module_graph)

    logic_nodes = [n for n in module_graph["nodes"] if not n["port"]]
    # Nodes module_graph's edges never mention -- no net to wire them to,
    # so there's nothing to instantiate meaningfully. Surface each one as
    # its own `output reg` on the module header instead of an
    # all-pins-unconnected instance.
    connected_nodes = [n for n in logic_nodes if n["id"] in touched_nodes]
    disconnected_nodes = [n for n in logic_nodes if n["id"] not in touched_nodes]

    # Every net actually used by some instance's non-power connections,
    # so we know which `wire` declarations are needed (ports get declared
    # in the module header instead, not as plain wires).
    used_nets = set()
    for n in connected_nodes:
        used_nets.update(instance_connections(n["id"], instance_pins, net_port).values())
    internal_wires = sorted(w for w in used_nets if w not in net_port.values())

    lines = []
    lines.append(f"// Auto-generated by genVerilog.py from {module_graph_name} "
                 f"+ {net_graph_name}.")
    lines.append("// Power/ground nets are already absent from MODULE_GRAPH.json's nodes/edges")
    lines.append("// (dropped upstream by moduleGraph.py); this file never reintroduces them.")
    lines.append("")

    def inst_name(inst_id):
        return f"u{inst_id.split(':', 1)[0]}"

    # Module header: the design's own ports, names and directions both
    # derived from MODULE_GRAPH.json (see module docstring), bus bits
    # regrouped into vectors, plus one `output reg` per disconnected node.
    orphan_regs = [inst_name(n["id"]) for n in disconnected_nodes]
    lines.append(f"module {module_name} (")
    port_decls = (
        [f"    {decl}" for decl in group_ports(port_directions)]
        + [f"    output reg {r}" for r in orphan_regs]
    )
    lines.append(",\n".join(port_decls))
    lines.append(");")
    lines.append("")

    for w in internal_wires:
        lines.append(f"wire {w};")
    if internal_wires:
        lines.append("")

    for n in connected_nodes:
        inst_id, cell = n["id"], n["cell"]
        name = inst_name(inst_id)
        conns = instance_connections(inst_id, instance_pins, net_port)
        full_pinout = pinout_lut[cell]
        # Sanity check, not expected to ever fire on this design (verified
        # in the driving conversation): every pin the cell type is known
        # to have should have been matched for this specific instance too.
        missing = [p for p in full_pinout if p not in conns]
        port_map = ",\n".join(f"    .{p}({conns[p]})" for p in full_pinout if p in conns)
        lines.append(f"{cell} {name} (")
        lines.append(port_map)
        if missing:
            lines.append(f"    // UNCONNECTED (label unmatched): {missing}")
        lines.append(");")
        lines.append("")

    lines.append(f"endmodule // {module_name}")
    return "\n".join(lines)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default_module_graph = os.path.join(here, "MODULE_GRAPH.json")
    default_net_graph = os.path.join(here, "puzzleGraph.json")
    default_out = os.path.join(here, "puzzleNetlist.v")

    parser = argparse.ArgumentParser(
        description="Emit a structural Verilog netlist from moduleGraph.py's "
                    "module graph plus netGraph.py's net graph."
    )
    parser.add_argument("--module-graph", default=default_module_graph,
                        help=f"module graph from moduleGraph.py "
                             f"(default: {default_module_graph})")
    parser.add_argument("--net-graph", default=default_net_graph,
                        help=f"net graph from netGraph.py, the file carrying "
                             f"instance_pins (default: {default_net_graph})")
    parser.add_argument("--out", default=default_out,
                        help=f"where to write the Verilog (default: {default_out})")
    # "puzzle" is the scope name example_inputs.vcd uses for the design, so
    # keeping it means the generated netlist's signal paths line up with the
    # reference waveform when both are opened in a viewer.
    parser.add_argument("--module-name", default="puzzle",
                        help="top-level module name to emit (default: puzzle)")
    args = parser.parse_args()

    module_graph, net_graph = load_graphs(args.module_graph, args.net_graph)
    verilog = generate_verilog(module_graph, net_graph, module_name=args.module_name,
                               module_graph_name=os.path.basename(args.module_graph),
                               net_graph_name=os.path.basename(args.net_graph))
    with open(args.out, "w") as f:
        f.write(verilog + "\n")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
