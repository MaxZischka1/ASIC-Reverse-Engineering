"""
coneProfile.py -- describe every cone by a vector of measurable attributes,
and offer several granularities of class built from them.

WHY THIS EXISTS ALONGSIDE coneClasses.py
========================================
coneClasses.py answers "which cones compute the SAME function", exactly, by
comparing complete truth tables. That is the right question when you want
proof, and it is the reason its groups have no false positives. But it is an
all-or-nothing relation: two cones are equal or they are unrelated, and on the
real puzzle 65 of the 72 groups come out as singletons. A design where almost
every group has one member tells you very little about how the design is
organised, because the thing you actually want to know -- "is this cone LIKE
that one" -- is not a question exact equality can answer.

This module asks the softer question. Each cone gets a profile: how big it is,
how deep, how wide, what it is built from, where its inputs come from, where
its output goes, and how it sits in the cone-to-cone graph. Cones that are
merely similar land near each other, which is what makes the singletons
comparable at all.

The two are complementary and neither replaces the other. A profile match is
evidence; a signature match is proof. Every profile record therefore carries
its exact `signature` alongside, so a hypothesis raised here can always be
checked against the exact relation rather than assumed.

WHAT IT READS
=============
`LOGIC_GRAPH.json` for the cones themselves (gates, depth, sources, the
`shape` structural hash logicGraph already computes, and the flop table that
says which flop each cone drives), and `CONE_GRAPH.json` for the exact class,
label and onset weight of the group each cone belongs to. Nothing else --
in particular no waveform and no layout.

THE ATTRIBUTES
==============
Grouped by what they measure, since they answer different questions.

INTRINSIC -- the cone on its own:
  num_gates, depth, fan_in, num_consts, distinct_cell_classes,
  cell_histogram, shape, inverted_pins

  `fan_in` counts NON-const leaves, matching coneClasses' k: a tied-off input
  is not a degree of freedom. `inverted_pins` counts inversions absorbed into
  pins by logicGraph, which is a measure of how much polarity juggling the
  synthesiser did and distinguishes cones that are otherwise the same size.

FUNCTION -- what it computes (carried over from the exact classifier):
  signature, label, verified_by, onset, balance

  `balance` is onset / 2^fan_in: 0.5 means the output is true for half its
  input space. It is permutation-invariant, so two cones with different
  balance are provably different functions -- a cheap, sound separator.

LEAF COMPOSITION -- where its inputs come from:
  leaves_flop, leaves_port, leaves_const, self_feedback,
  leaves_same_group, leaves_same_shape, max_leaf_fanout, mean_leaf_fanout

  `self_feedback` is the hold slot: the cone reads the Q of the very flop it
  drives, the signature of a register bit with an enable rather than a plain
  pipeline stage. NOTE it is true for all 92 flop cones in the real puzzle --
  every register in that design has a hold path -- so on this input it is a
  fact worth knowing but useless as a discriminator, which is why `role` does
  not spend itself on it. `leaves_same_shape` counts how many inputs come from
  cones built like itself, distinguishing a bit inside a uniform vector from
  an isolated piece of control.

FANOUT -- where its output goes:
  q_fanout, q_fanout_groups, q_fanout_shapes, peer_fraction,
  peer_fraction_group, drives_output

  `peer_fraction` is the share of consuming cones that have the SAME shape as
  this one. High means the cone sits in a bank of like things (a bus feeding a
  bus); low means it feeds a diverse set (a control bit broadcasting).

POSITION -- where it sits in the sequential graph:
  scc_size, in_cycle, dist_from_port, dist_to_output

  Distances are in FLOP HOPS over the cone-to-cone graph, not gate levels, so
  they measure pipeline position rather than logic depth. Unreachable is
  reported as null rather than as a large number, so it cannot be mistaken
  for "far away".

ROLE -- one coarse primary role, first match wins:
  output_driver     -> drives a primary output port
  port_pipeline     -> reads a primary input, no hold path
  port_register     -> reads a primary input and holds
  internal_register -> holds, fed only from other flops
  internal_pipeline -> no hold path, fed only from other flops

  Deliberately a small closed set. Anything finer belongs in the attributes,
  where it can be measured, rather than in a name that merely asserts.

THE FOUR CLASS VIEWS
====================
Four class keys per cone. They are deliberately NOT presented as a hierarchy,
because measurement shows they are not one:

  class_shape    logicGraph's `shape` fingerprint. Identical gate makeup.
  class_exact    coneClasses' signature. Identical Boolean function.
  class_profile  (role, fan_in, num_gates, depth, self_feedback). The
                 descriptive class: "same kind of thing, same size".
  class_block    the BLOCK a cone belongs to -- class_profile without gate
                 count, plus label family, overridden by strongly-connected
                 component. See block_key() for why each of those three
                 choices is what it is. This module owns that definition;
                 coneSignals and coneBlocks both read it rather than deriving
                 their own, so the stages cannot disagree about what a bank is.
  class_coarse   (role, self_feedback, drives_output). Broad strokes.

Only ONE refinement actually holds: class_profile determines class_coarse.
Everything else is incomparable, and the reasons are worth stating because
each is a trap:

  * class_shape does NOT refine class_exact. `shape` is a coarse fingerprint
    and it collides on real input: cones u227.D and u228.D share it while
    computing different functions. They share 36 of their 37 gate instances
    and 56 of their 57 leaves -- two overlapping cones differing in one final
    gate -- which is precisely the case a makeup-based hash cannot separate.
    Treat class_shape as a similarity hint, never as proof of anything.

  * class_exact does NOT refine class_profile. Two cones can compute the same
    function from different numbers of gates (a De Morgan variant, a retimed
    stage), so one exact class routinely spans several sizes.

  * class_profile does NOT refine class_exact. Same size and role says
    nothing whatever about what a cone computes.

So read them as four independent views of the same cone, and pick the one
that suits the question. The single real refinement is asserted at the end; a
bug that broke it fails loudly instead of emitting incoherent classes.

USAGE
=====
    python3 coneProfile.py [LOGIC_GRAPH.json] [--cone-graph CONE_GRAPH.json]
                           [--out CONE_PROFILE.json] [--md CONE_PROFILE.md]
"""

import argparse
import json
import os
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CELL_PREFIX = "sky130_fd_sc_hd__"


def cell_class(name):
    base = name[len(CELL_PREFIX):] if name.startswith(CELL_PREFIX) else name
    return base.rsplit("_", 1)[0].upper() if base.rsplit("_", 1)[-1].isdigit() \
        else base.upper()


BLOCK_FIELDS = ("role", "fan_in", "depth", "self_feedback")

PRIMITIVE_LABELS = ("or", "and", "nand", "nor", "xor", "xnor", "buffer",
                    "inverter", "mux")


def label_family(label):
    """The coarse function family a label belongs to, ignoring its
    parameters: 'load_enable(sel=~a, hold=d)' -> 'load_enable'."""
    if not label:
        return None
    for f in ("load_enable", "set_dominant_sticky", "clear_dominant",
              "toggle", "buffer"):
        if label.startswith(f):
            return f
    return "other"


def strongly_connected(logic, min_size=3):
    """{cone_id: component id} for cones in a cycle of >= min_size cones.

    The graph is one node per cone, with an edge A -> B when the flop that
    cone A drives has its Q read by cone B. A strongly connected component is
    a maximal set of cones each of which can reach every other by following
    those edges -- so every bit in it depends, through some path, on every
    other bit including itself.

    Only components of >= min_size are returned. Two-cone components are left
    alone deliberately: they are common (46 cones sit in one here) and
    frequently mean nothing more than a pair of bits that happen to read each
    other, whereas a component of three or more is a coupled unit that cannot
    be taken apart. Tarjan, iterative -- fanout here is wide enough that
    recursion is a liability."""
    cones = {c["id"]: c for c in logic["cones"]}
    cone_of_flop = {f["instance"]: f["d_cone"] for f in logic["flops"]
                    if f.get("d_cone")}
    succ = defaultdict(set)
    for cid, c in cones.items():
        for s in c["sources"]:
            if s["kind"] != "flop":
                continue
            src = cone_of_flop.get(s["instance"])
            if src and src != cid:
                succ[src].add(cid)

    index, low, on, stack, out, ctr, comp = {}, {}, {}, [], {}, [0], [0]
    for root in sorted(cones):
        if root in index:
            continue
        work = [(root, iter(sorted(succ.get(root, ()))))]
        index[root] = low[root] = ctr[0]; ctr[0] += 1
        stack.append(root); on[root] = True
        while work:
            node, it = work[-1]
            pushed = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = ctr[0]; ctr[0] += 1
                    stack.append(nxt); on[nxt] = True
                    work.append((nxt, iter(sorted(succ.get(nxt, ())))))
                    pushed = True
                    break
                if on.get(nxt):
                    low[node] = min(low[node], index[nxt])
            if pushed:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    w = stack.pop(); on[w] = False; members.append(w)
                    if w == node:
                        break
                if len(members) >= min_size:
                    for m in members:
                        out[m] = comp[0]
                    comp[0] += 1
    return out


def block_key(rec, family=None, scc=None):
    """The block a cone belongs to. THE one definition -- coneSignals and
    coneBlocks both read `class_block` rather than deriving their own, so the
    two stages cannot drift about what a bank is.

    It is class_profile MINUS num_gates, PLUS the label family, and overridden
    entirely by strongly-connected-component membership.

    Gate count is out because it is a sizing detail synthesis varies across
    bits of one uniform unit: the eight output bits run 28..55 gates and split
    into seven blocks if it is kept, and the wide k=13 bank runs 149/150/151
    and splits into three. DEPTH does not move -- all eight output cones sit
    at depth 8, all twenty-two k=13 cones at depth 14 -- because depth follows
    the shape of the function rather than how hard the sizer worked.

    The label family is in because without it the two k=13 banks here (eleven
    load_enable bits and eleven set_dominant_sticky bits, alike in role,
    fan-in and depth) collapse into one 22-member block. They are two
    different registers and only what they compute separates them.

    SCC membership overrides both, because mutually recursive state is one
    unit by construction and the attribute key shreds it -- the bits of a
    counter or state register have genuinely different fan-in and depth from
    one another. A shift chain is acyclic, so this leaves shifters and
    decoders untouched."""
    if scc is not None:
        return f"scc{scc}"
    return "|".join(str(rec[f]) for f in BLOCK_FIELDS) + f"|{family or '-'}"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_indexes(logic, cone_graph):
    """Everything downstream is a lookup away from one of these."""
    cones = {c["id"]: c for c in logic["cones"]}
    flops = logic["flops"]

    # cone -> the flop it drives (output-port cones drive no flop)
    flop_of_cone = {f["d_cone"]: f["instance"] for f in flops if f.get("d_cone")}
    cone_of_flop = {f["instance"]: f["d_cone"] for f in flops if f.get("d_cone")}

    cone_of_group = cone_graph.get("cone_to_group", {})
    node_of = {n["id"]: n for n in cone_graph["nodes"]}

    # cone -> cone edges: source flop's own cone feeds this cone
    consumers = defaultdict(set)   # cone -> cones it feeds
    producers = defaultdict(set)   # cone -> cones feeding it
    for cid, c in cones.items():
        for s in c["sources"]:
            if s["kind"] != "flop":
                continue
            src_cone = cone_of_flop.get(s["instance"])
            if src_cone is None:
                continue
            consumers[src_cone].add(cid)
            producers[cid].add(src_cone)

    return dict(cones=cones, flop_of_cone=flop_of_cone, cone_of_flop=cone_of_flop,
                cone_of_group=cone_of_group, node_of=node_of,
                consumers=consumers, producers=producers)


def sccs(nodes, succ):
    """Tarjan, iterative -- the cone graph has cycles by construction (every
    hold slot is a self-loop) and recursion would be fragile on wide fanout."""
    index = {}
    low = {}
    on_stack = {}
    stack = []
    result = []
    counter = [0]

    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(succ.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack[nxt] = True
                    work.append((nxt, iter(sorted(succ.get(nxt, ())))))
                    advanced = True
                    break
                if on_stack.get(nxt):
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)
    return result


def bfs_levels(sources, succ):
    """Hop counts from a source set. Absent == unreachable, reported as null."""
    dist = {s: 0 for s in sources}
    frontier = list(sources)
    while frontier:
        nxt = []
        for u in frontier:
            for v in succ.get(u, ()):
                if v not in dist:
                    dist[v] = dist[u] + 1
                    nxt.append(v)
        frontier = nxt
    return dist


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------

def profile_cones(logic, cone_graph):
    idx = build_indexes(logic, cone_graph)
    scc_of = strongly_connected(logic)
    cones = idx["cones"]
    cells = dict(logic["cells"])
    consumers, producers = idx["consumers"], idx["producers"]
    cone_of_flop, flop_of_cone = idx["cone_of_flop"], idx["flop_of_cone"]
    cone_of_group, node_of = idx["cone_of_group"], idx["node_of"]

    shape_of = {cid: c.get("shape") for cid, c in cones.items()}
    group_of = {cid: cone_of_group.get(cid) for cid in cones}

    # Position: distances are in flop hops over the cone graph.
    # Seed from cones that READ a primary input port. The obvious alternative
    # -- "cones with no flop leaves" -- is empty on this design, because every
    # register self-holds and so every cone has at least one flop leaf. That
    # would silently leave every distance null.
    port_fed = [cid for cid, c in cones.items()
                if any(s["kind"] == "input_port" for s in c["sources"])]
    out_cones = [cid for cid, c in cones.items()
                 if c["sink"]["kind"] == "output_port"]
    dist_in = bfs_levels(port_fed, consumers)
    dist_out = bfs_levels(out_cones, producers)

    comp_of = {}
    for comp in sccs(list(cones), consumers):
        for cid in comp:
            comp_of[cid] = len(comp)

    # How many cones each flop's Q feeds -- used for leaf fanout stats.
    q_fanout = {cid: len(consumers.get(cid, ())) for cid in cones}

    out = []
    for cid, c in sorted(cones.items()):
        sink = c["sink"]
        my_flop = flop_of_cone.get(cid)

        leaves = c["sources"]
        leaf_flops = [s for s in leaves if s["kind"] == "flop"]
        leaf_ports = [s for s in leaves if s["kind"] == "input_port"]
        leaf_const = [s for s in leaves if s["kind"] == "const"]
        fan_in = len(leaves) - len(leaf_const)

        self_feedback = any(s["instance"] == my_flop for s in leaf_flops) \
            if my_flop else False

        src_cones = [cone_of_flop.get(s["instance"]) for s in leaf_flops]
        src_cones = [s for s in src_cones if s]
        leaves_same_group = sum(1 for s in src_cones
                                if group_of.get(s) and group_of[s] == group_of[cid])
        leaves_same_shape = sum(1 for s in src_cones
                                if shape_of.get(s) and shape_of[s] == shape_of[cid])
        leaf_fanouts = [q_fanout.get(s, 0) for s in src_cones]

        cons = sorted(consumers.get(cid, ()))
        cons_groups = {group_of.get(x) for x in cons if group_of.get(x)}
        cons_shapes = {shape_of.get(x) for x in cons if shape_of.get(x)}
        peers = sum(1 for x in cons
                    if shape_of.get(x) and shape_of[x] == shape_of[cid])
        peers_group = sum(1 for x in cons
                          if group_of.get(x) and group_of[x] == group_of[cid])

        inverted = 0
        for g in c["gates"]:
            for pin in cells[g]["inputs"].values():
                if pin.get("inv"):
                    inverted += 1

        hist = Counter(cell_class(n) for n in c.get("cell_histogram", {})
                       for _ in range(c["cell_histogram"][n]))

        gid = group_of.get(cid)
        node = node_of.get(gid) if gid else None
        onset = node.get("onset") if node else None
        balance = (onset / float(1 << fan_in)) if (onset is not None
                                                  and fan_in <= 30) else None

        if sink["kind"] == "output_port":
            role = "output_driver"
        elif leaf_ports and not self_feedback:
            role = "port_pipeline"
        elif leaf_ports:
            role = "port_register"
        elif self_feedback:
            role = "internal_register"
        else:
            role = "internal_pipeline"

        rec = {
            "id": cid,
            "sink_kind": sink["kind"],
            "sink": sink.get("port") or sink.get("instance"),
            "flop": my_flop,
            "group": gid,
            "label": node.get("label") if node else None,
            "signature": node.get("signature") if node else None,
            "verified_by": node.get("verified_by") if node else None,

            "num_gates": c["num_gates"],
            "depth": c["depth"],
            "fan_in": fan_in,
            "num_consts": len(leaf_const),
            "distinct_cell_classes": len(hist),
            "cell_histogram": dict(sorted(hist.items())),
            "shape": shape_of.get(cid),
            "inverted_pins": inverted,

            "onset": onset,
            "balance": round(balance, 6) if balance is not None else None,

            "leaves_flop": len(leaf_flops),
            "leaves_port": len(leaf_ports),
            "leaves_const": len(leaf_const),
            "self_feedback": self_feedback,
            "leaves_same_group": leaves_same_group,
            "leaves_same_shape": leaves_same_shape,
            "max_leaf_fanout": max(leaf_fanouts) if leaf_fanouts else 0,
            "mean_leaf_fanout": round(sum(leaf_fanouts) / len(leaf_fanouts), 3)
                                if leaf_fanouts else 0.0,

            "q_fanout": len(cons),
            "q_fanout_groups": len(cons_groups),
            "q_fanout_shapes": len(cons_shapes),
            "peer_fraction": round(peers / len(cons), 3) if cons else None,
            "peer_fraction_group": round(peers_group / len(cons), 3) if cons else None,
            "drives_output": sink["kind"] == "output_port",
            "feeds": cons,

            "scc_size": comp_of.get(cid, 1),
            "in_cycle": comp_of.get(cid, 1) > 1 or self_feedback,
            "dist_from_port": dist_in.get(cid),
            "dist_to_output": dist_out.get(cid),

            "role": role,
        }

        rec["class_exact"] = rec["signature"] or f"unclassed:{cid}"
        rec["class_shape"] = rec["shape"] or f"unshaped:{cid}"
        rec["class_profile"] = "|".join(str(x) for x in (
            role, fan_in, c["num_gates"], c["depth"], int(self_feedback)))
        rec["class_coarse"] = "|".join(str(x) for x in (
            role, int(self_feedback), int(rec["drives_output"])))
        rec["class_block"] = block_key(
            rec, label_family(rec["label"]), scc_of.get(cid))
        rec["scc"] = scc_of.get(cid)
        out.append(rec)
    return out


# The only refinement that actually holds, verified against the data rather
# than assumed. See THE FOUR CLASS VIEWS for why the other pairings do not.
REFINEMENTS = (
    ("class_profile", "class_coarse"),
)


def check_refinement(profiles):
    """Assert every refinement in REFINEMENTS: cones equal under the finer key
    must be equal under the coarser one. A violation means an attribute is
    being computed inconsistently, so fail loudly rather than emit incoherent
    classes."""
    for finer, coarser in REFINEMENTS:
        seen = {}
        for p in profiles:
            f, c = p[finer], p[coarser]
            if f in seen and seen[f] != c:
                raise AssertionError(
                    f"{finer}={f} spans two {coarser} values "
                    f"({seen[f]!r} and {c!r}) -- {finer} does not refine "
                    f"{coarser}")
            seen[f] = c


def summarise(profiles, key):
    buckets = defaultdict(list)
    for p in profiles:
        buckets[p[key]].append(p["id"])
    return buckets


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_md(path, profiles):
    lines = ["# Cone profiles", "",
             f"{len(profiles)} cones. Attributes are measured; classes are "
             f"derived from them. See coneProfile.py for what each column "
             f"means.", ""]

    for key, title in (("class_shape", "Structural classes"),
                       ("class_exact", "Exact function classes"),
                       ("class_profile", "Profile classes"),
                       ("class_block", "Block classes"),
                       ("class_coarse", "Coarse classes")):
        b = summarise(profiles, key)
        multi = sum(1 for v in b.values() if len(v) > 1)
        lines += [f"## {title} (`{key}`)", "",
                  f"{len(b)} classes over {len(profiles)} cones; "
                  f"{multi} have more than one member.", ""]
        for name, ids in sorted(b.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(ids) < 2:
                continue
            lines.append(f"- `{name}` x{len(ids)}: {', '.join(sorted(ids)[:10])}"
                         + (" ..." if len(ids) > 10 else ""))
        lines.append("")

    lines += ["## Roles", ""]
    for role, n in Counter(p["role"] for p in profiles).most_common():
        lines.append(f"- {role}: {n}")
    lines.append("")

    cols = ["id", "role", "fan_in", "num_gates", "depth", "q_fanout",
            "peer_fraction", "self_feedback", "dist_from_port",
            "dist_to_output", "balance", "label"]
    lines += ["## Per-cone table", "",
              "| " + " | ".join(cols) + " |",
              "|" + "|".join("---" for _ in cols) + "|"]
    for p in sorted(profiles, key=lambda x: (-x["num_gates"], x["id"])):
        lines.append("| " + " | ".join(
            "" if p.get(c) is None else str(p.get(c)) for c in cols) + " |")
    lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("logic_graph", nargs="?",
                    default=os.path.join(HERE, "LOGIC_GRAPH.json"))
    ap.add_argument("--cone-graph", default=os.path.join(HERE, "CONE_GRAPH.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "CONE_PROFILE.json"))
    ap.add_argument("--md", default=os.path.join(HERE, "CONE_PROFILE.md"))
    args = ap.parse_args()

    logic = json.load(open(args.logic_graph))
    cone_graph = json.load(open(args.cone_graph))

    profiles = profile_cones(logic, cone_graph)
    check_refinement(profiles)

    stats = {
        "cones": len(profiles),
        "classes": {k: len(summarise(profiles, k))
                    for k in ("class_shape", "class_exact", "class_profile",
                              "class_block", "class_coarse")},
        "roles": dict(Counter(p["role"] for p in profiles)),
    }
    with open(args.out, "w") as f:
        json.dump({"stats": stats, "profiles": profiles}, f, indent=1)
    write_md(args.md, profiles)

    print(f"{len(profiles)} cones profiled")
    for k, v in stats["classes"].items():
        print(f"  {k:<14} {v:>4} classes")
    for k, v in sorted(stats["roles"].items()):
        print(f"  role {k:<16} {v:>4}")
    print(f"Wrote {args.out}")
    print(f"Wrote {args.md}")


if __name__ == "__main__":
    main()
