"""
blockMatch.py -- lift MODULE_GRAPH.json's flat gate soup into named
combinational blocks (adders, muxes, carry merges, compare trees, ...).

The pipeline is the one you'd run by hand with a printout and a pencil:

  1. WALK. Start at every combinational source -- a primary input port or
     a flip-flop's Q -- and walk forward net by net, stopping the moment a
     sequential pin (a flip-flop's D/CLK/RESET_B) or an output port is
     reached. Everything walked between two flop boundaries is one
     combinational cloud: a self-contained cone of logic whose inputs are
     all registered or primary, so it can be reasoned about (and truth-
     tabled) on its own. Clouds are the unit everything below works on.

  2. LOOSE NODES. Inside a cloud, the "loose" gates are the ones every
     input of which is a cloud source -- level 0, nothing upstream of them
     inside the cloud. Those are the seeds: a bit-slice of a datapath
     always starts at a gate chewing directly on operand bits, so the
     fan-in-2 loose gates are where the interesting structure begins.
     (Fan-in-2 gates are called out separately in the report, since a
     2-input gate on two registered bits is the classic slice anchor.)

  3. STATE MACHINE. From a seed gate, matching runs as a small NFA over
     gate types. A state is (template, step index, bindings); the start
     state set is "every template whose first step accepts this gate's
     cell type" -- so anchoring on an AND2 puts you in every template that
     opens with an AND-class gate at once, and each further step narrows
     the set. A step's inputs are symbols; a symbol either is already
     bound to a net (from an earlier step, so the step must sit on that
     wire) or gets bound here. Candidate gates for a step are only those
     touching an already-bound net, which keeps the search local instead
     of scanning the cloud. Every step is tried under all input
     permutations the cell's symmetry allows (A/B of an AND2 are
     interchangeable; A0/A1/S of a mux are not), and the search
     backtracks, so all matches are enumerated, not just the first.

  4. VERIFY. Structure is a hypothesis, not a proof: the same gate types
     wired slightly differently are a different function. So every
     complete match is checked by exhaustively simulating the gates it
     claims and comparing the resulting truth table against the block's
     reference function. Equivalence is checked up to output inversion
     ("out_inv") for templates whose polarity synthesis is free to flip,
     or up to full NPN equivalence (negate inputs, permute inputs, negate
     output) for carry-merge / reduce cells, where synthesis routinely
     runs a stage in the dual domain. Matches that fail are dropped.

  5. SELECT. Verified matches compete for gates: highest priority (bigger,
     more specific blocks first) claims its gates, and matches overlapping
     an already-claimed gate are dropped.

  6. GROW. What's left after step 5 is the connective tissue a shape
     match can't name on its own: the AND-OR compound cells of the carry
     chain, the sum bits, the reduce tree above them. An AND-OR cell is a
     carry merge because of *what it is wired to*, not what it looks like,
     so these are labelled by closure outward from the matched bit slices
     until nothing new is reachable (see grow_carry_network). Blocks say
     which way they were established -- by function, by structure, or by
     context -- so a contextual label is never mistaken for a proof.

  7. GROUP. Blocks are collected per combinational cloud, plus shift links
     across flop boundaries, and each group is labelled by its contents: a
     chain of enable-registers linked Q->A1 is a shift register (and its
     position in the chain names each bit, which is what lets the datapath
     print as A[3]+B[3] instead of net numbers), bit slices plus carry
     merges are an N-bit adder, an AND-tree over sum bits is a compare
     tree.

Usage:
    python3 blockMatch.py [MODULE_GRAPH.json] [--out BLOCK_MATCH.json]
                          [--verbose]
"""

import argparse
import inspect
import itertools
import json
import os
import re
from collections import Counter, defaultdict, namedtuple

CELL_PREFIX = "sky130_fd_sc_hd__"


# ---------------------------------------------------------------------------
# Cell library
# ---------------------------------------------------------------------------
# The library is READ FROM sky130_prims.v rather than written out here. The
# behavioural Verilog is the only real statement of what a cell is, and a
# second copy of that fact in Python is a copy that drifts: a mistyped pin
# name or a stale function makes templates quietly stop matching instead of
# failing loudly. Deriving it means a cell added to the Verilog is a cell
# this matcher immediately understands.
#
# Entries are keyed by "cell class": the library name with the sky130 prefix
# and the drive-strength suffix stripped (sky130_fd_sc_hd__and2_2 -> AND2),
# since drive strength is a physical property with no bearing on function --
# clkbuf_4, clkbuf_8 and clkbuf_16 are one class, CLKBUF.
#
#   ins  -- input pin names, in the cell's declared order
#   out  -- the primary output pin (None for a cell with no output at all,
#           like an antenna diode)
#   sym  -- groups of input positions that are interchangeable, DERIVED by
#           testing the function rather than asserted: the matcher may try
#           A&B against B&A, but never A0/A1/S of a mux in the wrong order
#   fn   -- the Boolean function, keyed by pin name
#   seq  -- True for state elements (a clocked always block); the walk stops
#           at these
#   extra_outs -- ((pin, fn), ...) for the rare multi-output cell (conb)
CellSpec = namedtuple("CellSpec", "ins out sym fn seq extra_outs")


def cell_class(cell_name):
    """sky130_fd_sc_hd__and2_2 -> AND2; clkbuf_4/_8/_16 -> CLKBUF."""
    base = cell_name[len(CELL_PREFIX):] if cell_name.startswith(CELL_PREFIX) else cell_name
    return re.sub(r"_\d+$", "", base).upper()


def verilog_to_python(expr):
    """Rewrite a 1-bit Verilog expression as an equivalent Python one.

    & | ^ ~ and parentheses carry over as-is; Python's ~ makes intermediates
    negative, which is harmless because every operator here is bitwise and
    the caller masks the result back to one bit. Sized literals and the one
    conditional form the library uses are rewritten explicitly."""
    expr = re.sub(r"\d*'b([01])", r"\1", expr).strip()
    ternary = re.fullmatch(r"(.+?)\?(.+?):(.+)", expr, re.DOTALL)
    if ternary:
        cond, then, other = (part.strip() for part in ternary.groups())
        if any("?" in part for part in (cond, then, other)):
            raise ValueError(f"nested conditional not supported: {expr}")
        expr = f"(({then}) if ({cond}) else ({other}))"
    return expr


def derive_symmetry(ins, fn):
    """Which inputs are interchangeable, established by trying it: swap two
    inputs, and if the truth table is unchanged for every assignment they
    are symmetric. Groups are the connected components of that relation,
    each confirmed closed under all its own permutations before being
    trusted (a component that isn't is dropped rather than assumed)."""
    n = len(ins)
    if n < 2 or fn is None:
        return ()

    def table(order):
        return tuple(fn(**{ins[order[k]]: (a >> k) & 1 for k in range(n)})
                     for a in range(1 << n))

    base = table(tuple(range(n)))

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in itertools.combinations(range(n), 2):
        order = list(range(n))
        order[i], order[j] = order[j], order[i]
        if table(tuple(order)) == base:
            parent[find(j)] = find(i)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    confirmed = []
    for group in groups.values():
        if len(group) < 2:
            continue
        ok = True
        for perm in itertools.permutations(group):
            order = list(range(n))
            for slot, src in zip(group, perm):
                order[slot] = src
            if table(tuple(order)) != base:
                ok = False
                break
        if ok:
            confirmed.append(tuple(sorted(group)))
    return tuple(sorted(confirmed))


def load_cell_library(path):
    """Parse sky130_prims.v into {class: CellSpec}.

    A module with continuous assignments is combinational and its functions
    are compiled from those assignments; a module with a clocked always
    block is sequential and gets no function (the walk stops there, so no
    template ever needs to evaluate it). Drive-strength variants of the
    same class must agree -- they are the same cell -- and disagreement is
    an error rather than a silent last-one-wins."""
    with open(path) as f:
        text = f.read()

    cells = {}
    for name, ports, body in re.findall(
            r"module\s+(\w+)\s*\((.*?)\);(.*?)endmodule", text, re.DOTALL):
        declared = re.findall(r"\b(input|output)\s+(?:reg\s+|wire\s+)?(\w+)", ports)
        ins = tuple(pin for kind, pin in declared if kind == "input")
        outs = tuple(pin for kind, pin in declared if kind == "output")
        seq = bool(re.search(r"\balways\b", body))

        fns = {}
        if not seq:
            for target, expr in re.findall(r"assign\s+(\w+)\s*=\s*(.*?);", body, re.DOTALL):
                source = (f"lambda {', '.join(ins)}: "
                          f"({verilog_to_python(expr)}) & 1")
                # The only input is this repo's own cell library, and the
                # expression grammar accepted above is a handful of bitwise
                # operators -- but evaluate with no builtins regardless.
                fns[target] = eval(source, {"__builtins__": {}})  # noqa: S307

        primary = outs[0] if outs else None
        spec = CellSpec(
            ins=ins,
            out=primary,
            sym=derive_symmetry(ins, fns.get(primary)),
            fn=fns.get(primary),
            seq=seq,
            extra_outs=tuple((pin, fns[pin]) for pin in outs[1:] if pin in fns),
        )

        cls = cell_class(name)
        if cls in cells:
            before = cells[cls]
            assert before.ins == spec.ins and before.out == spec.out, (
                f"{cls}: drive-strength variants disagree on pinout "
                f"({before.ins}->{before.out} vs {spec.ins}->{spec.out})")
            continue
        cells[cls] = spec
    return cells


HERE = os.path.dirname(os.path.abspath(__file__))
CELLS = load_cell_library(os.path.join(HERE, "sky130_prims.v"))

# Cell classes that are functionally "an AND / OR / XOR of two things" once
# input and output inversions are ignored -- used by templates that don't
# care which polarity synthesis happened to pick for a stage. Restricted to
# classes the library actually defines, so a trimmed-down sky130_prims.v
# narrows the templates instead of breaking them.
def _present(*names):
    return tuple(n for n in names if n in CELLS)


AND_LIKE2 = _present("AND2", "NAND2")
OR_LIKE2 = _present("OR2", "NOR2")
XOR_LIKE2 = _present("XOR2", "XNOR2")
REDUCE2 = _present("AND2", "NAND2", "OR2", "NOR2")
# Compound cells that fold one term into a 2-wide AND-OR (or its dual) --
# the shape a carry merge takes.
MERGE3 = _present("A21O", "A21OI", "A21BO", "A21BOI", "O21A", "O21AI",
                  "O21BA", "O21BAI")
MERGE4 = _present("A31O", "A31OI", "O31A", "O31AI")


def allowed_perms(spec):
    """Every input ordering the cell's symmetry permits, as tuples `p`
    where template slot i corresponds to cell pin spec.ins[p[i]]."""
    n = len(spec.ins)
    groups = [list(g) for g in spec.sym]
    perms = []
    for combo in itertools.product(*[itertools.permutations(g) for g in groups]):
        p = list(range(n))
        for group, order in zip(groups, combo):
            for slot, src in zip(group, order):
                p[slot] = src
        perms.append(tuple(p))
    return perms or [tuple(range(n))]


PERMS = {cls: allowed_perms(spec) for cls, spec in CELLS.items()}


# ---------------------------------------------------------------------------
# Netlist
# ---------------------------------------------------------------------------

class Gate:
    """One standard-cell instance, with its pins resolved to nets."""

    def __init__(self, inst_id, cell):
        self.id = inst_id
        self.cell = cell
        self.cls = cell_class(cell)
        self.spec = CELLS.get(self.cls)
        self.ins = {}        # pin -> net
        self.outs = {}       # pin -> net (more than one only for conb)

    @property
    def out_pin(self):
        """The cell's primary output. Nearly every cell has exactly one; a
        constant generator has two (HI and LO) and an antenna diode none,
        so this is the library's declared primary, with `outs` holding the
        full picture."""
        if self.spec and self.spec.out in self.outs:
            return self.spec.out
        return next(iter(sorted(self.outs)), None)

    @property
    def out_net(self):
        pin = self.out_pin
        return self.outs.get(pin) if pin else None

    @property
    def seq(self):
        return bool(self.spec and self.spec.seq)

    @property
    def complete(self):
        """All declared input pins resolved, and a function to evaluate.
        Geometry extraction can leave an endpoint without a pin name, and a
        physical-only cell (an antenna diode) has no output at all; neither
        can be matched against a template, only reported."""
        return (bool(self.spec) and self.spec.out is not None
                and all(p in self.ins for p in self.spec.ins))

    @property
    def fanin(self):
        return len(self.ins)

    def __repr__(self):
        return f"{self.cls}#{self.id.split(':', 1)[0]}"


class Netlist:
    """Directed view of MODULE_GRAPH.json: gates, ports, and for each net
    its single driver plus every load."""

    def __init__(self, graph):
        self.gates = {}
        self.ports = {}
        self.net_driver = {}                  # net -> (owner_id, pin)
        self.net_loads = defaultdict(list)    # net -> [(owner_id, pin)]
        self.net_port = {}                    # net -> chip port name, if any

        for node in graph["nodes"]:
            if node["port"]:
                self.ports[node["id"]] = {"id": node["id"], "name": node["id"].split(":", 1)[1],
                                          "drives": [], "loads": []}
            else:
                self.gates[node["id"]] = Gate(node["id"], node["cell"])

        for edge in graph["edges"]:
            net = edge["net"]
            if edge["port"]:
                self.net_port[net] = edge["port"]
            for pin in edge["pins"]:
                owner, name, direction = pin["instance"], pin["pin"], pin["direction"]
                if name is None:
                    continue
                if direction == "driver":
                    self.net_driver[net] = (owner, name)
                    if owner in self.gates:
                        self.gates[owner].outs[name] = net
                    else:
                        self.ports[owner]["drives"].append(net)
                else:
                    self.net_loads[net].append((owner, name))
                    if owner in self.gates:
                        self.gates[owner].ins[name] = net
                    else:
                        self.ports[owner]["loads"].append(net)

    def driver_gate(self, net):
        owner = self.net_driver.get(net, (None, None))[0]
        return self.gates.get(owner)

    def load_gates(self, net):
        return [self.gates[o] for o, _ in self.net_loads.get(net, []) if o in self.gates]

    def net_label(self, net):
        """Human-readable name for a net: its chip port if it has one,
        else n<net>(<driver>)."""
        if net in self.net_port:
            return self.net_port[net]
        owner, pin = self.net_driver.get(net, (None, None))
        if owner is None:
            return f"n{net}"
        if owner in self.gates:
            return f"n{net}({self.gates[owner]!r}.{pin})"
        return f"n{net}({owner})"

    def source_kind(self, net):
        """Where a cloud input comes from: a chip port, a flip-flop's Q, or
        (shouldn't happen) an undriven net."""
        owner, pin = self.net_driver.get(net, (None, None))
        if owner is None:
            return "undriven", None
        if owner in self.ports:
            return "port", self.ports[owner]["name"]
        gate = self.gates[owner]
        return ("register" if gate.seq else "logic"), gate.id


# ---------------------------------------------------------------------------
# Step 1-2: walk to the flip-flops, carve out clouds, find the loose nodes
# ---------------------------------------------------------------------------

class Cloud:
    """A combinational region bounded by flip-flops / chip ports."""

    def __init__(self, cid, gates):
        self.id = cid
        self.gates = gates            # [Gate], combinational only
        self.sources = []             # nets entering the cloud
        self.sinks = []               # {net, to: [...]} leaving the cloud
        self.levels = {}              # gate id -> topological level
        self.loose = []               # gate ids with all inputs at the boundary
        self.fanin2 = []              # gate ids with exactly 2 inputs
        self.flops = []               # flip-flops the walk terminated at


def walk_clouds(nl):
    """Walk forward from every combinational source (chip input port or
    flip-flop Q) until a sequential pin or output port stops the walk, then
    group everything walked into clouds -- maximal connected sets of
    combinational gates. Returns [Cloud], plus the walk's own record of
    which gates each source reached."""
    sources = []
    for port in nl.ports.values():
        sources.extend(port["drives"])
    for gate in nl.gates.values():
        if gate.seq and gate.out_net is not None:
            sources.append(gate.out_net)

    reached = {}          # source net -> set of gate ids walked from it
    stopped_at = defaultdict(set)   # source net -> flip-flop ids hit
    walked = set()
    for src in sources:
        seen, frontier = set(), [src]
        while frontier:
            net = frontier.pop()
            for gate in nl.load_gates(net):
                if gate.seq:
                    # The walk ends here: this is the far side of the
                    # combinational cloud.
                    stopped_at[src].add(gate.id)
                    continue
                if gate.id in seen:
                    continue
                seen.add(gate.id)
                if gate.out_net is not None:
                    frontier.append(gate.out_net)
        reached[src] = seen
        walked |= seen

    # Union the walked gates into clouds: two gates are in the same cloud
    # if one drives the other.
    parent = {gid: gid for gid in walked}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for gid in walked:
        gate = nl.gates[gid]
        if gate.out_net is None:
            continue
        for load in nl.load_gates(gate.out_net):
            if load.id in parent:
                union(gid, load.id)

    members = defaultdict(list)
    for gid in walked:
        members[find(gid)].append(gid)

    clouds = []
    for cid, (_, gids) in enumerate(sorted(members.items(), key=lambda kv: -len(kv[1]))):
        cloud = Cloud(cid, [nl.gates[g] for g in sorted(gids)])
        inside = set(gids)

        source_nets, sink_nets = set(), set()
        for gate in cloud.gates:
            for net in gate.ins.values():
                drv = nl.driver_gate(net)
                if drv is None or drv.id not in inside:
                    source_nets.add(net)
            if gate.out_net is not None:
                if any(dst not in inside for dst, _ in nl.net_loads.get(gate.out_net, [])) \
                        or gate.out_net in nl.net_port:
                    sink_nets.add(gate.out_net)

        for net in sorted(source_nets):
            kind, origin = nl.source_kind(net)
            cloud.sources.append({"net": net, "kind": kind, "from": origin,
                                  "label": nl.net_label(net)})
        for net in sorted(sink_nets):
            dests = []
            for owner, pin in nl.net_loads.get(net, []):
                if owner in inside:
                    continue
                dests.append({"instance": owner, "pin": pin})
                if owner in nl.gates and nl.gates[owner].seq:
                    cloud.flops.append(owner)
            if net in nl.net_port:
                dests.append({"instance": f"PORT:{nl.net_port[net]}", "pin": nl.net_port[net]})
            cloud.sinks.append({"net": net, "label": nl.net_label(net), "to": dests})
        cloud.flops = sorted(set(cloud.flops))

        # Topological level inside the cloud; level 0 == "loose", i.e. every
        # input arrives from the cloud boundary (a register or a port).
        for gate in cloud.gates:
            if gate.fanin == 2:
                cloud.fanin2.append(gate.id)
        pending = {g.id for g in cloud.gates}
        while pending:
            progressed = False
            for gid in sorted(pending):
                gate = nl.gates[gid]
                deps = [nl.driver_gate(n) for n in gate.ins.values()]
                deps = [d.id for d in deps if d is not None and d.id in inside]
                if all(d in cloud.levels for d in deps):
                    cloud.levels[gid] = 1 + max((cloud.levels[d] for d in deps), default=-1)
                    pending.discard(gid)
                    progressed = True
            if not progressed:      # combinational loop: level the rest flat
                for gid in sorted(pending):
                    cloud.levels[gid] = -1
                break
        cloud.loose = sorted(g.id for g in cloud.gates if cloud.levels.get(g.id) == 0)
        clouds.append(cloud)

    return clouds, reached, stopped_at


# ---------------------------------------------------------------------------
# Step 3: the template library and the matching state machine
# ---------------------------------------------------------------------------

class Step:
    """One gate a template is looking for: which cell classes are
    acceptable, and the symbols its inputs must line up with. An optional
    step is tried but not required -- that's how one template covers a
    bit-slice whether or not synthesis also built the spare polarity."""

    def __init__(self, role, cells, ins, optional=False):
        self.role = role
        self.cells = cells
        self.ins = ins
        self.optional = optional


class Template:
    """A named block: an ordered list of steps (gates to find), the
    reference function each output role must compute, and any equalities
    between symbols that structure alone can't express.

    Every required step after the first must reference at least one symbol
    bound by an earlier step -- that's what keeps matching a local walk
    outward from the seed instead of a search over the whole cloud.
    """

    def __init__(self, name, steps, expect=None, equiv="out_inv", prio=10,
                 constraints=(), check="function", note=""):
        self.name = name
        self.steps = steps
        self.expect = expect or {}
        self.equiv = equiv
        self.prio = prio
        self.constraints = constraints
        self.check = check
        self.note = note
        if not steps:
            return
        bound = {steps[0].role} | set(steps[0].ins)
        for step in steps[1:]:
            assert step.optional or (bound & set(step.ins)), \
                f"{name}: step {step.role} is not anchored"
            bound.add(step.role)
            bound |= set(step.ins)
        for step in steps:
            for cls in step.cells:
                assert cls in CELLS, f"{name}: unknown cell class {cls}"
                assert len(CELLS[cls].ins) == len(step.ins), \
                    f"{name}: step {step.role} arity != {cls}"


TEMPLATES = [
    # --- register slices -------------------------------------------------
    Template(
        "LOAD_ENABLE_REG",
        [Step("mux", ("MUX2",), ("hold", "load", "sel")),
         Step("ff", ("DFRTP",), ("clk", "mux", "rst"))],
        expect=None, check="structure", prio=90,
        constraints=(("hold", "ff"),),
        note="mux2 + flop whose A0 leg is the flop's own Q: the bit holds "
             "unless sel steers the load leg in",
    ),
    Template(
        "MUXED_REG",
        [Step("mux", ("MUX2",), ("a0", "a1", "sel")),
         Step("ff", ("DFRTP",), ("clk", "mux", "rst"))],
        expect=None, check="structure", prio=85,
        note="mux2 + flop with no hold path: the flop takes one of two "
             "sources every clock",
    ),

    # --- adder bit-slices ------------------------------------------------
    Template(
        "FULL_ADDER",
        [Step("p", XOR_LIKE2, ("a", "b")),
         Step("sum", XOR_LIKE2, ("p", "cin")),
         Step("g", AND_LIKE2, ("a", "b")),
         Step("cout", MERGE3, ("p", "cin", "g"))],
        expect={"sum": lambda a, b, cin: a ^ b ^ cin,
                "cout": lambda a, b, cin: (a & b) | (a & cin) | (b & cin)},
        prio=80, note="sum + carry-out over operand bits a,b and a carry-in",
    ),
    Template(
        "HALF_ADDER",
        [Step("sum", XOR_LIKE2, ("a", "b")),
         Step("carry", AND_LIKE2, ("a", "b")),
         Step("p", OR_LIKE2, ("a", "b"), optional=True)],
        expect={"sum": lambda a, b: a ^ b, "carry": lambda a, b: a & b,
                "p": lambda a, b: a | b},
        prio=70, note="sum + carry over two operand bits (adder LSB / incrementer)",
    ),
    # One template for the whole carry-lookahead bit-slice family: the
    # generate and propagate terms are mandatory, and synthesis may or may
    # not have added the spare propagate polarity and the half-sum -- the
    # latter either straight off the operand bits (x) or rebuilt out of
    # generate and propagate (xs = p & ~g = a ^ b), which is what a
    # gate-level adder does when the XOR would otherwise be duplicated.
    Template(
        "ADDER_SLICE",
        [Step("g", AND_LIKE2, ("a", "b")),
         Step("p", OR_LIKE2, ("a", "b")),
         Step("p2", OR_LIKE2, ("a", "b"), optional=True),
         Step("x", XOR_LIKE2, ("a", "b"), optional=True),
         Step("xs", REDUCE2, ("g", "p"), optional=True)],
        expect={"g": lambda a, b: a & b, "p": lambda a, b: a | b,
                "p2": lambda a, b: a | b, "x": lambda a, b: a ^ b,
                "xs": lambda a, b: a ^ b},
        prio=60,
        note="carry-lookahead bit slice: generate, propagate, and (where "
             "built) the half-sum of one operand bit pair",
    ),

    # --- generic combinational shapes -----------------------------------
    Template(
        "XOR3",
        [Step("t", XOR_LIKE2, ("a", "b")),
         Step("y", XOR_LIKE2, ("t", "c"))],
        expect={"y": lambda a, b, c: a ^ b ^ c},
        prio=30, note="3-input parity / sum without a carry output",
    ),
    Template(
        "MUX2_CELL",
        [Step("mux", ("MUX2",), ("a0", "a1", "sel"))],
        expect={"mux": lambda a0, a1, sel: a1 if sel else a0},
        equiv="exact", prio=20, note="a standalone 2:1 multiplexer cell",
    ),
    Template(
        "CLOCK_BUFFER",
        [Step("buf", ("CLKBUF",), ("a",))],
        expect={"buf": lambda a: a},
        equiv="exact", prio=15, note="clock-tree buffer",
    ),
]

# Blocks discovered by the closure passes rather than by the state machine
# (see grow_carry_network): their gates are single cells whose identity
# comes from what they are wired to, not from a shape the matcher can pin
# down on its own.
CLOSURE_TEMPLATES = {
    name: Template(name, [], check="context", prio=prio, note=note)
    for name, prio, note in [
        ("CARRY_MERGE", 45,
         "AND-OR compound folding a bit slice's generate term into the carry chain"),
        ("SUM_BIT", 35,
         "a sum bit of the adder: combines slice terms and the carry into it, and "
         "is consumed only by the reduce tree"),
        ("EQ_REDUCE", 25,
         "AND-class gate reducing sum/compare bits toward a single equality output"),
    ]
}

TEMPLATES_BY_FIRST_CELL = defaultdict(list)
for _t in TEMPLATES:
    for _cls in _t.steps[0].cells:
        TEMPLATES_BY_FIRST_CELL[_cls].append(_t)


class Match:
    """A template matched onto concrete gates."""

    def __init__(self, template, gates, bindings, pinmaps, cloud_id):
        self.template = template
        self.gates = gates          # role -> Gate
        self.bindings = bindings    # symbol -> net
        self.pinmaps = pinmaps      # role -> {pin: symbol}
        self.cloud_id = cloud_id
        self.check = template.check

    @property
    def key(self):
        return (self.template.name, frozenset(g.id for g in self.gates.values()))

    @property
    def gate_ids(self):
        return {g.id for g in self.gates.values()}


def step_perms(template, cls):
    """Input orderings to try for a gate. Normally only the ones the cell's
    own symmetry allows; for a template verified up to NPN equivalence, any
    ordering, since that check tolerates permuted (and inverted) inputs
    anyway and the interesting compounds are asymmetric."""
    if template.equiv == "npn":
        return list(itertools.permutations(range(len(CELLS[cls].ins))))
    return PERMS[cls]


def bind_gate(template, gate, step, bindings):
    """Every way this gate can satisfy this step: for each admissible input
    ordering, line the step's symbols up with the gate's pins and keep it
    if that agrees with what's already bound."""
    spec = CELLS[gate.cls]
    for perm in step_perms(template, gate.cls):
        new_bindings = dict(bindings)
        pinmap = {}
        ok = True
        for slot, sym in enumerate(step.ins):
            pin = spec.ins[perm[slot]]
            net = gate.ins[pin]
            if new_bindings.setdefault(sym, net) != net:
                ok = False
                break
            pinmap[pin] = sym
        if not ok:
            continue
        if gate.out_net is not None:
            if new_bindings.setdefault(step.role, gate.out_net) != gate.out_net:
                continue
        yield new_bindings, pinmap


def next_states(nl, template, state, step):
    """The state machine's transition: from the current partial match, all
    ways the next step's gate can be pinned down. Candidates are gates
    touching an already-bound net -- driver or load -- so the search only
    ever walks one hop out from what's already matched."""
    gates, bindings, pinmaps = state
    used = {g.id for g in gates.values()}
    candidates = {}
    for sym in step.ins:
        if sym not in bindings:
            continue
        net = bindings[sym]
        drv = nl.driver_gate(net)
        if drv is not None:
            candidates[drv.id] = drv
        for load in nl.load_gates(net):
            candidates[load.id] = load

    for gate in candidates.values():
        if gate.id in used or gate.cls not in step.cells or not gate.complete:
            continue
        for new_bindings, pinmap in bind_gate(template, gate, step, bindings):
            yield ({**gates, step.role: gate}, new_bindings, {**pinmaps, step.role: pinmap})


def advance(nl, template, index, state):
    """Drive the state machine from step `index` onward, branching over
    every admissible next gate and (for optional steps) over skipping it."""
    if index == len(template.steps):
        yield state
        return
    step = template.steps[index]
    for nxt in next_states(nl, template, state, step):
        yield from advance(nl, template, index + 1, nxt)
    if step.optional:
        yield from advance(nl, template, index + 1, state)


def match_from(nl, seed, cloud_id):
    """Run the state machine from one seed gate. The start state set is
    every template opening with this gate's cell class ("if gate 1 is an
    AND, next_state is every block that starts with an AND"); each step
    then narrows it, and every surviving path is a candidate match."""
    if not seed.complete:
        return []

    results = []
    for template in TEMPLATES_BY_FIRST_CELL.get(seed.cls, ()):
        first = template.steps[0]
        for bindings, pinmap in bind_gate(template, seed, first, {}):
            start = ({first.role: seed}, bindings, {first.role: pinmap})
            for gates, done_bindings, pinmaps in advance(nl, template, 1, start):
                if any(done_bindings.get(x) != done_bindings.get(y) or x not in done_bindings
                       for x, y in template.constraints):
                    continue
                results.append(Match(template, gates, done_bindings, pinmaps, cloud_id))
    return results


# ---------------------------------------------------------------------------
# Step 4: verification by exhaustive simulation
# ---------------------------------------------------------------------------

def truth_tables(nl, match):
    """Simulate the matched gates over every assignment of their free
    inputs. Free inputs are bound nets not driven from inside the match.
    Returns (free_symbols, {role: truth table}) or (None, None) if the
    match can't be simulated (sequential cell, or too wide)."""
    inside = {g.out_net for g in match.gates.values() if g.out_net is not None}
    if any(g.seq for g in match.gates.values()):
        return None, None

    free_syms, free_nets = [], []
    for sym, net in sorted(match.bindings.items()):
        if net in inside or net in free_nets:
            continue
        free_syms.append(sym)
        free_nets.append(net)
    if len(free_nets) > 12:
        return None, None

    order = []
    pending = list(match.gates.items())
    known = set(free_nets)
    while pending:
        progressed = False
        for role, gate in list(pending):
            if all(net in known or net not in inside for net in gate.ins.values()):
                order.append((role, gate))
                known.add(gate.out_net)
                pending.remove((role, gate))
                progressed = True
        if not progressed:
            return None, None

    tables = defaultdict(list)
    for assignment in range(1 << len(free_nets)):
        values = {net: (assignment >> i) & 1 for i, net in enumerate(free_nets)}
        for role, gate in order:
            spec = CELLS[gate.cls]
            args = {pin: values.get(gate.ins[pin], 0) for pin in spec.ins}
            values[gate.out_net] = spec.fn(**args)
            tables[role].append(values[gate.out_net])
    return free_syms, tables


def reference_table(fn, free_syms):
    """Truth table of a template's reference function, indexed the same way
    as truth_tables()'s: bit i of the index is free_syms[i]."""
    params = list(inspect.signature(fn).parameters)
    if any(p not in free_syms for p in params):
        return None
    idx = [free_syms.index(p) for p in params]
    return [fn(*[(a >> i) & 1 for i in idx]) for a in range(1 << len(free_syms))]


def npn_equal(got, want, n):
    """True if `got` equals `want` up to negating inputs, permuting inputs
    and negating the output -- the freedom synthesis actually uses when it
    pushes a stage into the dual domain."""
    inv = [1 - v for v in got]
    for perm in itertools.permutations(range(n)):
        for mask in range(1 << n):
            table = []
            for a in range(1 << n):
                b = 0
                for j in range(n):
                    b |= (((a >> perm[j]) & 1) ^ ((mask >> j) & 1)) << j
                table.append(want[b])
            if table == got or table == inv:
                return True
    return False


def verify(nl, match):
    """Check every output role against the template's reference function.
    A template with no reference (one containing a flip-flop) is accepted
    on structure alone and says so."""
    if not match.template.expect:
        return True

    free_syms, tables = truth_tables(nl, match)
    if tables is None:
        return False

    for role, fn in match.template.expect.items():
        if role not in match.gates:
            continue        # optional step this match didn't take
        if role not in tables:
            return False
        want = reference_table(fn, free_syms)
        if want is None:
            return False
        got = tables[role]
        if match.template.equiv == "exact":
            if got != want:
                return False
        elif match.template.equiv == "out_inv":
            if got != want and [1 - v for v in got] != want:
                return False
        else:
            if not npn_equal(got, want, len(free_syms)):
                return False
    return True


# ---------------------------------------------------------------------------
# Step 5: pick a non-overlapping set of blocks, then group them
# ---------------------------------------------------------------------------

def find_blocks(nl, clouds, verbose=False):
    """Seed the state machine at every gate, loose (level 0) gates first as
    the natural bit-slice anchors, then deeper ones so structures further
    into the cone are still found. Verified matches then compete for gates,
    biggest and most specific first."""
    matches, seen = [], set()
    for cloud in clouds:
        seeds = sorted(cloud.gates, key=lambda g: (cloud.levels.get(g.id, 99), g.id))
        for seed in seeds:
            for match in match_from(nl, seed, cloud.id):
                if match.key in seen:
                    continue
                seen.add(match.key)
                if verify(nl, match):
                    matches.append(match)
                elif verbose:
                    print(f"  rejected {match.template.name} at {seed!r}: "
                          f"structure matches but function does not")

    matches.sort(key=lambda m: (-m.template.prio, -len(m.gates), sorted(m.gate_ids)))
    claimed, blocks = set(), []
    for match in matches:
        if match.gate_ids & claimed:
            continue
        claimed |= match.gate_ids
        blocks.append(match)
    return blocks, claimed


# The state machine finds shapes that are self-evident from wiring alone.
# What's left over in a real synthesised datapath is the connective tissue:
# the compound AND-OR cells of the carry chain, the sum bits, and the
# reduce tree above them. Those cells are ordinary gates -- what makes one
# a carry merge rather than a random AND-OR is *what it is wired to*. So
# they're identified by growing outward from the bit slices to a fixpoint
# rather than by matching a shape.

REDUCE_CLASSES = ("AND2", "NAND2", "OR2", "NOR2", "AND3", "AND4BB")


def gates_reaching_ports(nl):
    """Every gate with a path to a chip output port."""
    reach, frontier = set(), [net for net in nl.net_port
                              if any(o in nl.ports for o, _ in nl.net_loads.get(net, []))]
    while frontier:
        gate = nl.driver_gate(frontier.pop())
        if gate is None or gate.id in reach:
            continue
        reach.add(gate.id)
        frontier.extend(gate.ins.values())
    return reach


def grow_carry_network(nl, blocks, claimed, cloud_of):
    """Label the gates the templates couldn't, by closure over what the
    templates already found:

      A. carry merges -- an AND-OR compound cell with two or more inputs
         coming out of the datapath (bit slices, or carries already
         labelled) is folding a generate term into the carry chain;
      B. sum bits -- an XOR-class gate whose inputs are all datapath, plus
         any labelled carry whose output feeds only the reduce tree (that
         is a result bit, not a carry, however the cell is drawn);
      C. reduce tree -- an AND-class gate whose inputs are all sum bits,
         datapath, or other reduce gates, and which has a path to an
         output port: the equality/compare tree collapsing the result.
    """
    datapath = {gate.out_net for b in blocks if b.template.name in ADDER_KINDS
                for gate in b.gates.values() if gate.out_net is not None}
    to_port = gates_reaching_ports(nl)
    free = [g for g in nl.gates.values()
            if g.id not in claimed and not g.seq and g.complete]

    labels = {}      # gate id -> kind

    def sweep(kind, predicate, into):
        added = True
        while added:
            added = False
            for gate in free:
                if gate.id in labels or not predicate(gate):
                    continue
                labels[gate.id] = kind
                into.add(gate.out_net)
                added = True

    sweep("CARRY_MERGE",
          lambda g: (g.cls in MERGE3 + MERGE4
                     and sum(1 for n in g.ins.values() if n in datapath) >= 2),
          datapath)

    sumbits = set()
    sweep("SUM_BIT",
          lambda g: (g.cls in XOR_LIKE2
                     and all(n in datapath for n in g.ins.values())),
          sumbits)

    reduce_nets = set()
    sweep("EQ_REDUCE",
          lambda g: (g.cls in REDUCE_CLASSES and g.id in to_port
                     and all(n in datapath or n in sumbits or n in reduce_nets
                             for n in g.ins.values())),
          reduce_nets)

    # A "carry merge" whose output is only ever consumed by the reduce tree
    # isn't in the chain at all -- it is the result bit for that column,
    # drawn as a compound cell.
    for gid, kind in list(labels.items()):
        if kind != "CARRY_MERGE":
            continue
        dests = [g.id for g in nl.load_gates(nl.gates[gid].out_net)]
        if dests and all(labels.get(d) == "EQ_REDUCE" for d in dests):
            labels[gid] = "SUM_BIT"

    grown = []
    for gid, kind in sorted(labels.items()):
        gate = nl.gates[gid]
        match = Match(CLOSURE_TEMPLATES[kind], {"cell": gate},
                      {"cell": gate.out_net,
                       **{f"in_{pin}": net for pin, net in sorted(gate.ins.items())}},
                      {"cell": {pin: f"in_{pin}" for pin in gate.ins}},
                      cloud_of.get(gid))
        grown.append(match)
        claimed.add(gid)
    return grown


def chain_registers(nl, blocks):
    """Order LOAD_ENABLE_REG blocks into shift chains: stage X follows
    stage Y when X's load leg is driven by Y's flip-flop."""
    regs = [b for b in blocks if b.template.name == "LOAD_ENABLE_REG"]
    by_q = {b.gates["ff"].out_net: b for b in regs}
    prev = {}
    for b in regs:
        src = by_q.get(b.bindings["load"])
        if src is not None and src is not b:
            prev[id(b)] = src
    heads = [b for b in regs if id(b) not in prev]
    nxt = {id(v): k for k, v in ((b, prev[id(b)]) for b in regs if id(b) in prev)}
    chains = []
    for head in heads:
        chain, cur = [head], head
        while id(cur) in nxt:
            cur = nxt[id(cur)]
            if cur in chain:
                break
            chain.append(cur)
        chains.append(chain)
    return chains


def group_blocks(nl, blocks, chains):
    """Collect blocks into higher-level structures.

    Grouping deliberately does NOT just union everything that shares a wire
    -- flip-flop Q's feed the datapath and the clock tree feeds every flop,
    so that would return the whole chip as one blob. Instead a group is one
    combinational cloud's worth of blocks (the clouds are already the right
    boundary), and the only links allowed to cross a cloud boundary are
    register-to-register shift links, which is what turns 8 one-mux clouds
    back into one shift register."""
    parent = list(range(len(blocks)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_cloud = defaultdict(list)
    for i, block in enumerate(blocks):
        by_cloud[block.cloud_id].append(i)
    for members in by_cloud.values():
        for other in members[1:]:
            union(members[0], other)

    index = {id(b): i for i, b in enumerate(blocks)}
    for chain in chains:
        for block in chain[1:]:
            union(index[id(chain[0])], index[id(block)])

    groups = defaultdict(list)
    for i in range(len(blocks)):
        groups[find(i)].append(i)
    return [sorted(v) for v in groups.values()]


ADDER_KINDS = {"FULL_ADDER", "HALF_ADDER", "ADDER_SLICE"}
CARRY_KINDS = {"CARRY_MERGE"}
EQ_KINDS = {"EQ_REDUCE"}
REG_KINDS = {"LOAD_ENABLE_REG", "MUXED_REG"}


def register_bit_names(nl, chains):
    """Name each flip-flop by its position in its shift chain: the stage
    fed by the serial input is bit 0, and every stage downstream is one bit
    up. That turns the datapath's operand nets from net numbers into
    A[3]/B[3], which is what makes a pile of bit slices readable as an
    adder over two registers."""
    names = {}
    for chain in chains:
        if len(chain) < 2:
            continue
        source = nl.net_port.get(chain[0].bindings["load"]) \
            or nl.net_label(chain[0].bindings["load"])
        for bit, block in enumerate(chain):
            names[block.gates["ff"].id] = f"{source}[{bit}]"
    return names


def describe_group(nl, blocks, idxs, chains, bit_names):
    """Name a group from what's in it, and pull out the numbers that make
    it a *block* rather than a pile of gates: how many bit-slices, which
    registers feed them, where the result leaves."""
    kinds = Counter(blocks[i].template.name for i in idxs)
    members = [blocks[i] for i in idxs]
    parts, detail = [], {}

    slices = [b for b in members if b.template.name in ADDER_KINDS]
    if slices:
        def operand(net):
            drv = nl.driver_gate(net)
            return (bit_names.get(drv.id) if drv else None) or nl.net_label(net)

        operands = []
        for b in slices:
            # Order the two operands so the same register always lands in
            # the same column, and sort the slices into bit order.
            pair = sorted((operand(b.bindings["a"]), operand(b.bindings["b"])))
            operands.append({
                "bit": re.sub(r"^.*\[(\d+)\]$", r"\1", pair[0]) if "[" in pair[0] else None,
                "a": pair[0], "b": pair[1],
                "kind": b.template.name,
                "terms": sorted(b.gates),
            })
        operands.sort(key=lambda o: (int(o["bit"]) if o["bit"] is not None else 99, o["a"]))
        detail["operand_bits"] = operands
        detail["width"] = len(operands)
        merges = sum(kinds[k] for k in CARRY_KINDS)
        sums = sum(kinds[k] for k in ("SUM_BIT",) if k in kinds)
        parts.append(f"{len(operands)}-bit adder"
                     + (f" ({merges} carry merges, {sums} sum bits)" if merges else ""))
    elif any(k in CARRY_KINDS for k in kinds):
        parts.append(f"{sum(kinds[k] for k in CARRY_KINDS)}-stage carry chain")

    if any(k in EQ_KINDS for k in kinds):
        parts.append(f"compare/equality tree ({sum(kinds[k] for k in EQ_KINDS)} reduce stages)")

    if kinds.get("CLOCK_BUFFER"):
        flops = {owner for b in members for g in b.gates.values()
                 for owner, pin in nl.net_loads.get(g.out_net, [])
                 if owner in nl.gates and nl.gates[owner].seq and pin == "CLK"}
        parts.append(f"clock tree ({kinds['CLOCK_BUFFER']} buffers -> "
                     f"{len(flops)} flop clock pins)")
        detail["clocked_flops"] = sorted(flops)

    regs = [b for b in members if b.template.name in REG_KINDS]
    if regs:
        member_ids = {id(b) for b in regs}
        my_chains = [c for c in chains if any(id(b) in member_ids for b in c)]
        long_chains = [c for c in my_chains if len(c) > 1]
        if long_chains:
            detail["shift_chains"] = [
                {"length": len(c),
                 "serial_in": nl.net_label(c[0].bindings["load"]),
                 "enable": nl.net_label(c[0].bindings["sel"]),
                 "stages": [{"bit": bit, "flop": b.gates["ff"].id,
                             "name": bit_names.get(b.gates["ff"].id)}
                            for bit, b in enumerate(c)]}
                for c in sorted(long_chains, key=len, reverse=True)
            ]
            widths = ", ".join(str(len(c)) for c in sorted(long_chains, key=len, reverse=True))
            parts.append(f"{len(long_chains)} enable-gated shift register(s), width {widths}")
        else:
            parts.append(f"{len(regs)} enable-gated register bit(s)")

    if not parts:
        parts.append(" + ".join(f"{n}x{k}" for k, n in kinds.most_common()))

    sinks = sorted({nl.net_port[g.out_net]
                    for b in members for g in b.gates.values()
                    if g.out_net in nl.net_port})
    if sinks:
        detail["drives_ports"] = sinks

    return " + ".join(parts), dict(kinds), detail


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def block_json(nl, block):
    return {
        "kind": block.template.name,
        "note": block.template.note,
        "cloud": block.cloud_id,
        # how the block was established: "function" -- its truth table was
        # checked against the reference; "structure" -- shape only (it
        # contains a flip-flop, so there is no combinational function to
        # check); "context" -- labelled by what it is wired to.
        "check": block.check,
        "gates": [
            {"role": role, "instance": gate.id, "cell": gate.cls,
             "pins": {pin: {"symbol": sym, "net": gate.ins[pin],
                            "label": nl.net_label(gate.ins[pin])}
                      for pin, sym in block.pinmaps[role].items()},
             "out": gate.out_net}
            for role, gate in block.gates.items()
        ],
        "signals": {sym: {"net": net, "label": nl.net_label(net)}
                    for sym, net in sorted(block.bindings.items())},
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="Walk MODULE_GRAPH.json's combinational clouds and match "
                    "their gates against a library of larger blocks (adders, "
                    "muxes, carry merges, compare trees)."
    )
    parser.add_argument("in_json", nargs="?", default=os.path.join(here, "MODULE_GRAPH.json"),
                        help="module graph produced by moduleGraph.py")
    parser.add_argument("--out", default=os.path.join(here, "BLOCK_MATCH.json"),
                        help="where to write the matched-block JSON")
    parser.add_argument("--verbose", action="store_true",
                        help="also report structural matches rejected by verification")
    args = parser.parse_args()

    print(f"Cell library: {len(CELLS)} classes derived from sky130_prims.v "
          f"({sum(1 for s in CELLS.values() if s.seq)} sequential, "
          f"{sum(1 for s in CELLS.values() if s.sym)} with symmetric inputs)")

    with open(args.in_json) as f:
        graph = json.load(f)
    nl = Netlist(graph)

    unknown = sorted({g.cls for g in nl.gates.values() if g.spec is None})
    if unknown:
        print(f"WARNING: sky130_prims.v defines no cell for {', '.join(unknown)} -- "
              f"those gates can only be reported, not matched")

    clouds, reached, stopped_at = walk_clouds(nl)
    blocks, claimed = find_blocks(nl, clouds, verbose=args.verbose)
    cloud_of = {g.id: c.id for c in clouds for g in c.gates}
    blocks += grow_carry_network(nl, blocks, claimed, cloud_of)
    chains = chain_registers(nl, blocks)
    groups = group_blocks(nl, blocks, chains)

    print(f"\n{len(nl.gates)} cells, {len(nl.ports)} ports -> "
          f"{len(clouds)} combinational cloud(s) between flop boundaries")
    trivial = 0
    for cloud in clouds:
        if len(cloud.gates) == 1:
            trivial += 1
            continue
        srcs = Counter(s["kind"] for s in cloud.sources)
        print(f"\ncloud {cloud.id}: {len(cloud.gates)} gates, "
              f"{len(cloud.sources)} inputs ({', '.join(f'{n} {k}' for k, n in srcs.most_common())}), "
              f"{len(cloud.sinks)} outputs, walk stopped at {len(cloud.flops)} flop(s)")
        print(f"  depth {max(cloud.levels.values(), default=0) + 1}, "
              f"{len(cloud.loose)} loose gate(s), {len(cloud.fanin2)} fan-in-2 gate(s)")
        loose2 = [g for g in cloud.loose if g in set(cloud.fanin2)]
        if loose2:
            print("  loose fan-in-2 seeds: "
                  + ", ".join(repr(nl.gates[g]) for g in loose2[:12])
                  + (" ..." if len(loose2) > 12 else ""))
    if trivial:
        singles = Counter(c.gates[0].cls for c in clouds if len(c.gates) == 1)
        print(f"\n{trivial} single-gate cloud(s): "
              + ", ".join(f"{n}x {k}" for k, n in singles.most_common()))

    print(f"\n{len(blocks)} block(s) identified over {len(claimed)} of {len(nl.gates)} cells:")
    for kind, count in Counter(b.template.name for b in blocks).most_common():
        checks = Counter(b.check for b in blocks if b.template.name == kind)
        how = ", ".join(f"{n} by {c}" for c, n in checks.most_common())
        print(f"  {count:3}x {kind:<16} ({how})")

    bit_names = register_bit_names(nl, chains)
    group_json = []
    print("\nGrouped into higher-level structures:")
    for idxs in sorted(groups, key=len, reverse=True):
        label, kinds, detail = describe_group(nl, blocks, idxs, chains, bit_names)
        print(f"  [cloud {blocks[idxs[0]].cloud_id}] {label}")
        for chain in detail.get("shift_chains", []):
            print(f"      {chain['length']} stages, serial in {chain['serial_in']}, "
                  f"enabled by {chain['enable']}: "
                  + " -> ".join(s["name"] for s in chain["stages"]))
        if "operand_bits" in detail:
            for op in detail["operand_bits"]:
                print(f"      bit {op['bit'] or '?':>2}: {op['a']} + {op['b']}"
                      f"   [{op['kind']}: {', '.join(op['terms'])}]")
        if "drives_ports" in detail:
            print(f"      drives port(s): {', '.join(detail['drives_ports'])}")
        group_json.append({"label": label, "cloud": blocks[idxs[0]].cloud_id,
                           "kind_counts": kinds, "blocks": idxs, **detail})

    unmatched = sorted(g.id for g in nl.gates.values() if g.id not in claimed and not g.seq)
    if unmatched:
        print(f"\n{len(unmatched)} combinational cell(s) left unmatched: "
              + ", ".join(repr(nl.gates[g]) for g in unmatched))

    out = {
        "clouds": [{
            "id": c.id,
            "gates": [g.id for g in c.gates],
            "sources": c.sources,
            "sinks": c.sinks,
            "flops_reached": c.flops,
            "levels": c.levels,
            "loose_gates": c.loose,
            "fanin2_gates": c.fanin2,
        } for c in clouds],
        # The raw walk, one entry per combinational source: exactly the
        # gates reached before the flops stopped it.
        "walks": [{
            "source": nl.net_label(net),
            "net": net,
            "gates_walked": sorted(gids),
            "stopped_at_flops": sorted(stopped_at.get(net, ())),
        } for net, gids in sorted(reached.items()) if gids or stopped_at.get(net)],
        "blocks": [block_json(nl, b) for b in blocks],
        "groups": group_json,
        "unmatched_gates": unmatched,
        "stats": {
            "cells": len(nl.gates),
            "ports": len(nl.ports),
            "clouds": len(clouds),
            "blocks": len(blocks),
            "cells_claimed": len(claimed),
            "cells_unmatched": len(unmatched),
            "block_kinds": dict(Counter(b.template.name for b in blocks)),
        },
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
