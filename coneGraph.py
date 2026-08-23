"""
coneGraph.py -- turn the flat cone classification into a connected dataflow
graph of recognised functional blocks.

Reads LOGIC_GRAPH.json (cones + cells + flops) and CONE_CLASSES.json
(coneClasses.py's grouping), and writes:

  CONE_GRAPH.md    the readable report -- one self-contained section per
                   group, then the inter-group adjacency
  CONE_GRAPH.json  the same thing as a data structure

NODES are the cone groups. Each carries its canonical Boolean function, a
label, k, size, and its member cones with the leaf->canonical-position
permutation recovered during matching.

EDGES are real dataflow: group A -> group B when some flop in A drives a leaf
of some cone in B. The relation is derived here, since LOGIC_GRAPH stores
cone sources (which flop feeds this cone) but not the group-level roll-up.
A cone's own flop is found through flops[].d_cone, so "flop u1228" and "cone
u1228.D" are two views of one node.

LABELS come from matching the canonical truth table against a small primitive
library -- AND/OR/NAND/NOR/XOR/XNOR of the right arity including the
mixed-polarity product/sum families (a conjunction of k literals of which j
are inverted, which is what an equality-against-a-constant comparator looks
like), half/full adder sum and carry, 2:1 and 4:1 muxes, constants and
buffers. Matching is done on the CANONICAL form, so a primitive is recognised
whatever order its inputs arrive in. Onset weight is permutation-invariant, so
it is used as a cheap pre-filter: only primitives with the same weight are
canonicalised at all. Anything unmatched is named unknown_k<N>_<hash prefix>.

Usage:
    python3 coneGraph.py [LOGIC_GRAPH.json] [--classes CONE_CLASSES.json]
                         [--prims sky130_prims.v] [--out CONE_GRAPH.md]
"""

import argparse
import itertools
import json
import os
from collections import defaultdict

import numpy as np

import coneClasses as CC


# ---------------------------------------------------------------------------
# Canonical form helpers (same path coneClasses used, so signatures line up)
# ---------------------------------------------------------------------------

def canon_bits(bits, k, brute_k=8):
    """Canonical truth table for a function given only its truth table."""
    bitmasks = [((np.arange(1 << k, dtype=np.int64) >> i) & 1) for i in range(k)]
    if k <= brute_k:
        classes = [list(range(k))]
    else:
        classes = CC.functional_classes(bits, k, bitmasks)
    orderings = CC.class_orderings(bits, k, classes)
    if CC.perm_count(orderings) > 200000:
        return None, None
    sig_bytes, pi = CC.canonical(bits, k, classes, bitmasks, orderings)
    return np.frombuffer(sig_bytes, dtype=np.uint8), pi


# ---------------------------------------------------------------------------
# Primitive library
# ---------------------------------------------------------------------------

def primitive_tables(k):
    """{name: truth table} for every primitive that has arity k."""
    q = np.arange(1 << k, dtype=np.int64)
    x = [((q >> i) & 1).astype(np.uint8) for i in range(k)]
    out = {}

    if k == 0:
        return out
    if k == 1:
        out["buffer"] = x[0]
        out["inverter"] = 1 - x[0]
        return out

    # AND / OR families with j inverted literals. j=0 is the plain gate; j>0
    # is "these bits must be 0, those must be 1" -- an equality test against a
    # constant pattern, which is what a comparator's output cone looks like.
    for j in range(k + 1):
        lits = [1 - x[i] if i < j else x[i] for i in range(k)]
        prod = lits[0].copy()
        for t in lits[1:]:
            prod = prod & t
        summ = lits[0].copy()
        for t in lits[1:]:
            summ = summ | t
        pname = f"and{k}" if j == 0 else f"and{k}_neg{j}"
        sname = f"or{k}" if j == 0 else f"or{k}_neg{j}"
        out[pname] = prod
        out[sname] = summ
        out["n" + pname] = 1 - prod
        out["n" + sname] = 1 - summ

    par = x[0].copy()
    for t in x[1:]:
        par = par ^ t
    out[f"xor{k}"] = par
    out[f"xnor{k}"] = 1 - par

    if k == 2:
        out["half_adder_sum"] = x[0] ^ x[1]
        out["half_adder_carry"] = x[0] & x[1]
        out["comparator_bit_xnor"] = 1 - (x[0] ^ x[1])
    if k == 3:
        out["full_adder_sum"] = x[0] ^ x[1] ^ x[2]
        out["full_adder_carry_majority"] = ((x[0] & x[1]) | (x[0] & x[2])
                                            | (x[1] & x[2]))
        out["mux2to1"] = np.where(x[2].astype(bool), x[1], x[0]).astype(np.uint8)
    if k == 6:
        sel = x[4] + 2 * x[5]
        m = np.zeros(1 << k, dtype=np.uint8)
        for i in range(4):
            m = np.where(sel == i, x[i], m)
        out["mux4to1"] = m.astype(np.uint8)
    return out


# ---------------------------------------------------------------------------
# State-update decomposition
# ---------------------------------------------------------------------------
# A flop's D-cone is usually not a combinational primitive at all: it is a
# register UPDATE rule written around the flop's own Q, which appears as one
# of the cone's leaves. Rather than enumerate every such function, split it on
# that leaf with Shannon cofactors and recognise the shape:
#
#   f = q | g        set-dominant  ("sticky": once set, stays set)
#   f = q & g        clear-dominant
#   f = q ^ g        toggle
#   f = s ? a : q    load-enable    (hold q unless s selects a)
#   f = (q&~r) | s   set/reset with hold
#
# The residual g is then itself described, which is where the interesting
# content lives (it is usually the pattern-match condition).

def cofactor(bits, k, v, val):
    """f restricted to x_v = val, over the remaining k-1 variables."""
    q = np.arange(1 << k, dtype=np.int64)
    keep = (((q >> v) & 1) == val)
    sel = q[keep]
    lo = sel & ((1 << v) - 1)
    hi = (sel >> (v + 1)) << v
    idx = lo | hi
    out = np.zeros(1 << (k - 1), dtype=np.uint8)
    out[idx] = bits[sel]
    return out


def is_projection(bits, k):
    """If f is just one of its variables, which one."""
    for v in range(k):
        col = ((np.arange(1 << k, dtype=np.int64) >> v) & 1).astype(np.uint8)
        if np.array_equal(bits, col):
            return v
    return None


def var_name(i):
    return chr(ord("a") + i) if i < 26 else f"x{i}"


def describe_residual(g, kk, names=None, depth=0):
    """Short description of a cofactor: primitive name, else a small SOP.

    `names` are the ORIGINAL variable names of the inputs that survive the
    split. Without them the residual would be re-lettered from "a", which
    reuses the name of the very variable we just split on and makes the
    expression unreadable (an "a" inside the residual of a split on a).
    """
    if kk == 0:
        return "1" if int(g[0]) else "0"
    canon, _ = canon_bits(g, kk)
    if canon is not None:
        name = match_primitive(canon, kk, canon)
        if name:
            return name
    if depth < 2:
        # The load leg of an enabled register is often itself a register shape
        # (a gated toggle, say), so recurse once rather than dumping its SOP.
        sub = decompose(g, kk, names=names, depth=depth + 1)
        if sub is not None:
            return sub[1].replace("f = ", "", 1)
    e = sop_expression(g, kk, max_terms=8, names=names)
    return e if e else f"<{kk}-input function>"


def decompose(bits, k, names=None, depth=0):
    """Recognise a register-update shape. Returns (label, note) or None."""
    if k < 2:
        return None
    nm = (lambda i: names[i]) if names else var_name
    ones = np.ones(1 << (k - 1), dtype=np.uint8)
    zeros = np.zeros(1 << (k - 1), dtype=np.uint8)

    # Two passes on purpose. The set/clear/toggle shapes say more about the
    # register than load_enable does, and a cone can present both on different
    # variables (f = i | (~a & p) is a sticky set on i AND a hold on a). Taking
    # the first variable that matches anything would let the weaker reading win
    # just because its variable sorts earlier.
    for v in range(k):
        f0 = cofactor(bits, k, v, 0)
        f1 = cofactor(bits, k, v, 1)
        if np.array_equal(f0, f1):
            continue                                  # f does not depend on v
        q = nm(v)
        rest = [nm(i) for i in range(k) if i != v]
        if np.array_equal(f1, ones):
            return (f"set_dominant_sticky(q={q})",
                    f"f = {q} | ({describe_residual(f0, k - 1, rest, depth)})")
        if np.array_equal(f0, zeros):
            return (f"clear_dominant(q={q})",
                    f"f = {q} & ({describe_residual(f1, k - 1, rest, depth)})")
        if np.array_equal(f1, 1 - f0):
            return (f"toggle(q={q})",
                    f"f = {q} ^ ({describe_residual(f0, k - 1, rest, depth)})")
    for v in range(k):
        f0 = cofactor(bits, k, v, 0)
        f1 = cofactor(bits, k, v, 1)
        if np.array_equal(f0, f1):
            continue
        q = nm(v)
        rest = [nm(i) for i in range(k) if i != v]
        pv = is_projection(f0, k - 1)
        if pv is not None:
            held = rest[pv]
            return (f"load_enable(sel={q}, hold={held})",
                    f"f = {q} ? ({describe_residual(f1, k - 1, rest, depth)}) : {held}")
        pv1 = is_projection(f1, k - 1)
        if pv1 is not None:
            # f holds when q is 1, so the LOAD leg is the q=0 cofactor.
            held = rest[pv1]
            return (f"load_enable(sel=~{q}, hold={held})",
                    f"f = {q} ? {held} : ({describe_residual(f0, k - 1, rest, depth)})")
    return None


_PRIM_CACHE = {}


def match_primitive(bits, k, canon):
    """Name of the primitive this function is, up to input permutation."""
    if k not in _PRIM_CACHE:
        tabs = primitive_tables(k)
        by_weight = defaultdict(list)
        for name, t in tabs.items():
            by_weight[int(t.sum())].append((name, t))
        _PRIM_CACHE[k] = by_weight
    want = int(bits.sum())
    for name, t in _PRIM_CACHE[k].get(want, []):
        c, _ = canon_bits(t, k)
        if c is not None and np.array_equal(c, canon):
            return name
    return None


# ---------------------------------------------------------------------------
# Cheap SOP via Quine-McCluskey with a greedy cover
# ---------------------------------------------------------------------------

def sop_expression(bits, k, max_terms=24, max_onset=4096, names=None):
    """A readable sum-of-products, or None when it would not be readable."""
    onset = np.nonzero(bits)[0]
    if len(onset) == 0:
        return "0"
    if len(onset) == (1 << k):
        return "1"
    if len(onset) > max_onset:
        return None

    # implicant = (value, mask) with mask bits set where the variable is fixed
    cur = {(int(m), (1 << k) - 1) for m in onset}
    primes = set()
    while cur:
        nxt, used = set(), set()
        bylist = list(cur)
        buckets = defaultdict(list)
        for v, msk in bylist:
            buckets[msk].append((v, msk))
        for msk, group in buckets.items():
            for (v1, _), (v2, _) in itertools.combinations(group, 2):
                d = v1 ^ v2
                if d and (d & (d - 1)) == 0:      # differ in exactly one bit
                    nm = msk & ~d
                    nxt.add((v1 & nm, nm))
                    used.add((v1, msk))
                    used.add((v2, msk))
        primes |= (cur - used)
        cur = nxt
        if len(primes) > 4000:
            return None

    def covers(imp, m):
        v, msk = imp
        return (m & msk) == v

    remaining = set(int(m) for m in onset)
    chosen = []
    plist = sorted(primes, key=lambda p: -bin(((1 << k) - 1) ^ p[1]).count("1"))
    while remaining:
        best, bestn = None, -1
        for p in plist:
            n = sum(1 for m in remaining if covers(p, m))
            if n > bestn:
                best, bestn = p, n
        if best is None or bestn <= 0:
            return None
        chosen.append(best)
        remaining -= {m for m in remaining if covers(best, m)}
        if len(chosen) > max_terms:
            return None

    if names is None:
        names = [var_name(i) for i in range(k)]
    terms = []
    for v, msk in chosen:
        lits = [(names[i] if (v >> i) & 1 else "~" + names[i])
                for i in range(k) if (msk >> i) & 1]
        terms.append("&".join(lits) if lits else "1")
    return " | ".join(terms)


def truth_table_block(bits, k):
    """Full table, only when it is short enough to actually read."""
    if k > 4:
        return None
    names = [chr(ord("a") + i) for i in range(k)]
    lines = ["  " + " ".join(f"{n}" for n in reversed(names)) + " | f",
             "  " + "-" * (2 * k + 4)]
    for p in range(1 << k):
        row = " ".join(str((p >> i) & 1) for i in reversed(range(k)))
        lines.append(f"  {row} | {int(bits[p])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(lg, classes, prims):
    cells = dict(lg["cells"])
    lib = CC.build_numpy_library(prims)
    cone_by_id = {c["id"]: c for c in lg["cones"]}
    # flop instance -> the cone that feeds it
    dcone = {f["instance"]: f["d_cone"] for f in lg["flops"]}

    groups = []
    for gi, g in enumerate(sorted(classes["groups"],
                                  key=lambda x: (-x["size"], x["k"], x["signature"])), 1):
        gid = f"G{gi:02d}"
        ref = cone_by_id[g["reference_cone"]]
        entry = {
            "id": gid,
            "signature": g["signature"],
            "verified_by": g["verified_by"],
            "k": g["k"],
            "size": g["size"],
            "reference_cone": g["reference_cone"],
            "members": g["cones"],
            "label": None,
            "decomposition": None,
            "expression": None,
            "truth_table": None,
            "onset": None,
        }
        if g["verified_by"] == "function":
            dag = CC.ConeDag(ref, cells, lib)
            packed = CC.evaluate(dag, lib)
            bits = CC.unpack(packed, 1 << dag.k)
            canon, _ = canon_bits(bits, dag.k)
            if canon is not None:
                entry["onset"] = int(canon.sum())
                name = match_primitive(canon, dag.k, canon)
                if name is None:
                    dec = decompose(canon, dag.k)
                    if dec is not None:
                        name, entry["decomposition"] = dec
                entry["label"] = name or f"unknown_k{dag.k}_{g['signature'][:8]}"
                entry["expression"] = sop_expression(canon, dag.k)
                entry["truth_table"] = truth_table_block(canon, dag.k)
        if entry["label"] is None:
            entry["label"] = (f"structural_only_k{g['k']}_{g['signature'][:8]}"
                              if g["verified_by"] != "function"
                              else f"unknown_k{g['k']}_{g['signature'][:8]}")
        groups.append(entry)

    gid_of_cone = {m["id"]: e["id"] for e in groups for m in e["members"]}

    # --- edges: flop in A drives a leaf of a cone in B ----------------------
    edges = defaultdict(lambda: defaultdict(list))
    for e in groups:
        for m in e["members"]:
            cone = cone_by_id[m["id"]]
            for s in cone["sources"]:
                if s["kind"] != "flop":
                    continue
                src_cone = dcone.get(s["instance"])
                if src_cone is None:
                    continue
                a = gid_of_cone.get(src_cone)
                if a is None:
                    continue
                edges[a][e["id"]].append((src_cone, m["id"]))

    adjacency = {a: {b: len(v) for b, v in sorted(d.items())}
                 for a, d in sorted(edges.items())}
    return groups, adjacency, edges, gid_of_cone


def report(groups, adjacency, edges, gid_of_cone, lg):
    cone_by_id = {c["id"]: c for c in lg["cones"]}
    L = []
    n_edges = sum(len(d) for d in adjacency.values())
    L.append("# Cone function graph\n")
    L.append(f"{len(groups)} groups, {n_edges} directed group-to-group edges.\n")
    L.append("Nodes are cone groups (functionally equivalent fan-in cones); an edge "
             "A -> B means some flop whose D-cone is in A drives a leaf of some "
             "cone in B. Variables a,b,c... are CANONICAL positions: member "
             "permutations below map each physical leaf onto one of them.\n")

    L.append("\n## Index\n")
    L.append("| ID | label | k | size | verified |")
    L.append("|----|-------|---|------|----------|")
    for e in groups:
        L.append(f"| {e['id']} | `{e['label']}` | {e['k']} | {e['size']} | {e['verified_by']} |")

    L.append("\n## Groups\n")
    for e in groups:
        L.append(f"\n### {e['id']} -- `{e['label']}`\n")
        L.append(f"- k (fan-in) = **{e['k']}**, size (cones in group) = **{e['size']}**")
        L.append(f"- established by: **{e['verified_by']}**"
                 + ("  (truth-table verified, exact)" if e["verified_by"] == "function"
                    else "  (shape only -- function NOT verified)"))
        L.append(f"- canonical signature: `{e['signature']}`")
        if e["onset"] is not None:
            L.append(f"- onset: {e['onset']} of {1 << e['k']} minterms")
        L.append("")
        if e.get("decomposition"):
            L.append("**Recognised form**\n")
            L.append("```")
            L.append(e["decomposition"])
            L.append("```")
        if e["expression"]:
            L.append("**Canonical function**\n")
            L.append("```")
            L.append(f"f({', '.join(chr(ord('a')+i) for i in range(min(e['k'], 26)))}) = "
                     f"{e['expression']}")
            L.append("```")
        elif e["verified_by"] == "function":
            L.append("**Canonical function** -- too large to print as a readable "
                     "expression; identified by signature only.\n")
        else:
            L.append("**Canonical function** -- not computed (structural-only group).\n")
        if e["truth_table"]:
            L.append("\n```")
            L.append(e["truth_table"])
            L.append("```")

        L.append(f"\n**Members ({e['size']})** -- leaf -> canonical position\n")
        for m in e["members"]:
            cone = cone_by_id[m["id"]]
            L.append(f"- `{m['id']}` ({m['num_gates']} gates)")
            if "leaf_roles" in m:
                roles = sorted(m["leaf_roles"], key=lambda r: r["canonical_position"])
                cells_ = []
                for r in roles:
                    who = r["from"]
                    who = ("u" + who.split(":")[0]) if who and ":" in who else who
                    var = chr(ord("a") + r["canonical_position"]) \
                        if r["canonical_position"] < 26 else f"x{r['canonical_position']}"
                    cells_.append(f"{var}={who}")
                for i in range(0, len(cells_), 8):
                    L.append("    " + "  ".join(cells_[i:i + 8]))
            else:
                L.append("    (no permutation -- structural-only group)")

    L.append("\n\n## Inter-group dataflow\n")
    L.append("Adjacency list, `A -> B (n)` where n is the number of distinct "
             "(driving cone, driven cone) pairs.\n")
    L.append("```")
    lbl = {e["id"]: e["label"] for e in groups}
    for a in sorted(adjacency):
        outs = adjacency[a]
        L.append(f"{a}  [{lbl[a]}]")
        for b, n in sorted(outs.items(), key=lambda kv: -kv[1]):
            self_ = "  (self)" if a == b else ""
            L.append(f"    -> {b:<5} x{n:<4} [{lbl[b]}]{self_}")
    L.append("```")

    indeg = defaultdict(int)
    outdeg = defaultdict(int)
    for a, d in adjacency.items():
        for b in d:
            outdeg[a] += 1
            indeg[b] += 1
    L.append("\n### Sources and sinks\n")
    srcs = [e["id"] for e in groups if indeg[e["id"]] == 0]
    snks = [e["id"] for e in groups if outdeg[e["id"]] == 0]
    L.append(f"- no incoming edges ({len(srcs)}): {', '.join(srcs) or 'none'}")
    L.append(f"- no outgoing edges ({len(snks)}): {', '.join(snks) or 'none'}")
    return "\n".join(L) + "\n"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("logic_graph", nargs="?",
                   default=os.path.join(here, "LOGIC_GRAPH.json"))
    p.add_argument("--classes", default=os.path.join(here, "CONE_CLASSES.json"))
    p.add_argument("--prims", default=os.path.join(here, "sky130_prims.v"))
    p.add_argument("--out", default=os.path.join(here, "CONE_GRAPH.md"))
    p.add_argument("--json", default=None)
    args = p.parse_args()

    with open(args.logic_graph) as f:
        lg = json.load(f)
    with open(args.classes) as f:
        classes = json.load(f)

    groups, adjacency, edges, gid_of_cone = build(lg, classes, args.prims)
    text = report(groups, adjacency, edges, gid_of_cone, lg)
    with open(args.out, "w") as f:
        f.write(text)

    jpath = args.json or os.path.splitext(args.out)[0] + ".json"
    with open(jpath, "w") as f:
        json.dump({
            "nodes": [{kk: vv for kk, vv in e.items() if kk != "truth_table"}
                      for e in groups],
            "adjacency": adjacency,
            "cone_to_group": gid_of_cone,
        }, f, indent=1)

    named = sum(1 for e in groups if not e["label"].startswith(("unknown", "structural")))
    n_edges = sum(len(d) for d in adjacency.values())
    print(f"{len(groups)} groups, {named} matched a known primitive, "
          f"{len(groups) - named} unnamed")
    print(f"{n_edges} directed group-to-group edges")
    print(f"Wrote {args.out}")
    print(f"Wrote {jpath}")


if __name__ == "__main__":
    main()
