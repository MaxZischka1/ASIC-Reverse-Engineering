#!/usr/bin/env python3
"""coneClasses.py — group cones into functional equivalence classes.

Stage 5 of the pipeline (see CLAUDE.md). Each cone's Boolean function is derived
from the cell-function tables, then cones computing the same function are put in
the same class. How the function is compared depends on the cone's leaf count k:

  k <= 8    exact truth table (2^k entries). Two cones are equivalent iff their
            tables match, which is both sound and complete. Tables are also
            compared under permutation of the inputs, so the same logic applied
            to different signals -- a bit slice of a datapath -- lands in one
            class.

  8 < k < 20  the function is normalised instead of tabulated: DeMorgan pushes
            negations to the leaves, constants are folded away, associative
            operators are flattened, commutative operands are deduplicated and
            sorted, and identical subexpressions are shared (see boolExpr).
            Equal canonical keys prove equivalence; unequal keys do not disprove
            it, so these classes are reported as "normalised", not "exact".

  k >= 20   out of scope: reported with no key and left unclassified rather than
            guessed at.

Output (CONE_CLASSES.json):

{
  "classes": [
    {"id": int, "method": "truth_table" | "normalised",
     "n_leaves": int, "members": [cone ids],
     "representative": cone id, "key": str,
     "permutation_matched": bool}     # truth-table classes only
  ],
  "cones": [{"id", "n_leaves", "class": int|null, "method", "key",
             "expr_nodes": int, "unclassified_reason": str|null}],
  "summary": {...},
  "warnings": [...]
}
"""

import argparse
import json
import sys
from collections import defaultdict
from itertools import permutations

import boolExpr
from boolExpr import build_and, build_or, build_xor, negate, ONE, ZERO
from coneDecompose import Netlist
from genCellModels import FAMILY_FUNCS, family_of

EXACT_MAX_LEAVES = 8         # exact truth tables up to here
NORMALISE_MAX_LEAVES = 20    # normalised comparison below here (exclusive)
PERM_BUDGET = 40320          # cap on permutations tried when canonicalising


# ------------------------------------------------------------- cone -> function

def cell_expressions(cell_name):
    """{output pin: parsed AST} for a combinational cell type."""
    fam = family_of(cell_name)
    funcs = FAMILY_FUNCS.get(fam)
    if funcs is None:
        return None
    return {pin: boolExpr.parse(text) for pin, text in funcs.items()}


def cone_function(nl, cone, expr_cache, warnings):
    """Build the canonical Boolean node for a cone, plus its variable ordering.

    Returns (node, leaf_nets) or (None, reason).
    """
    root = cone["root_net"]
    value = {}                       # net -> canonical node
    leaf_index = {}                  # net -> variable index
    leaf_nets = []

    def var_for(net):
        if net not in leaf_index:
            leaf_index[net] = len(leaf_nets)
            leaf_nets.append(net)
        return ("v", leaf_index[net])

    def leaf_value(net):
        """The node for a boundary signal: a constant if the net carries one
        (a tie cell, a rail tie, or an input held constant for the analysis),
        otherwise a fresh variable."""
        if net in value:
            return value[net]
        if net in nl.const_of_net:
            node = ONE if nl.const_of_net[net] == "1" else ZERO
        else:
            node = var_for(net)
        value[net] = node
        return node

    # Leaves first, in the order the cone records them, so variable numbering is
    # deterministic and independent of traversal order.
    inside = set(cone["cells"])
    for inst in cone["cells"]:
        for _pin, src in nl.input_nets(inst):
            if isinstance(src, tuple):
                continue                          # tied constant
            if src is None:
                continue
            drv = nl.driver.get(src)
            if drv is not None and drv[1] in inside:
                continue                          # internal signal
            leaf_value(src)

    if not cone["cells"]:
        # the cone is just the signal itself
        return leaf_value(root), leaf_nets

    for inst in cone["cells"]:
        cell = nl.inst_cell[inst]
        exprs = expr_cache.get(cell, "miss")
        if exprs == "miss":
            exprs = cell_expressions(cell)
            expr_cache[cell] = exprs
        if exprs is None:
            return None, "cell %s has no Boolean model" % cell
        env = {}
        for pin, src in nl.input_nets(inst):
            if isinstance(src, tuple):
                env[pin] = ONE if src[1] == "1" else ZERO
            elif src is None:
                return None, "%s.%s is unconnected" % (inst, pin)
            else:
                env[pin] = leaf_value(src)
        for pin, ast in exprs.items():
            out_net = nl.net_of.get((inst, pin))
            if out_net is None:
                continue                          # unused output of this cell
            try:
                value[out_net] = boolExpr.from_ast(ast, env)
            except KeyError as e:
                return None, "cell %s: %s" % (cell, e)

    if root not in value:
        return None, "root net %d was never produced" % root
    return value[root], leaf_nets


# --------------------------------------------------------------- truth tables

def truth_table(node, k):
    """Exact truth table of a k-variable function, as an integer of 2^k bits."""
    n = 1 << k
    mask = (1 << n) - 1
    values = {}
    for i in range(k):
        # column i: bit m is set when variable i is 1 in minterm m
        col = 0
        block = 1 << i
        m = 0
        while m < n:
            if (m & block):
                col |= 1 << m
            m += 1
        values[i] = col
    return boolExpr.evaluate(node, values, mask) & mask


def permute_table(tt, k, order):
    """Rewrite a truth table under a reordering of its inputs.

    `order` lists old variable indices in their new positions: the variable that
    ends up at position j is order[j]. So bit j of a rewritten minterm is read
    from bit order[j] of the original.
    """
    out = 0
    n = 1 << k
    for m in range(n):
        if not (tt >> m) & 1:
            continue
        dst = 0
        for j in range(k):
            if (m >> order[j]) & 1:
                dst |= 1 << j
        out |= 1 << dst
    return out


def canonical_table(tt, k):
    """Smallest truth table over all permutations of the inputs, so that the
    same function of differently-ordered signals gets one key.

    Variables are first bucketed by a permutation-invariant signature (how many
    minterms each one appears in), which usually leaves only a few orderings to
    try; permutations are enumerated within tied buckets only.
    """
    if k == 0:
        return tt, True
    sig = []
    for i in range(k):
        cnt = 0
        for m in range(1 << k):
            if (m >> i) & 1 and (tt >> m) & 1:
                cnt += 1
        sig.append(cnt)
    buckets = defaultdict(list)
    for i, s in enumerate(sig):
        buckets[s].append(i)

    total = 1
    for group in buckets.values():
        f = 1
        for x in range(2, len(group) + 1):
            f *= x
        total *= f
        if total > PERM_BUDGET:
            break
    if total > PERM_BUDGET:
        order = [i for s in sorted(buckets) for i in buckets[s]]
        return permute_table(tt, k, order), False

    groups = [buckets[s] for s in sorted(buckets)]

    best = None
    def expand(idx, prefix):
        nonlocal best
        if idx == len(groups):
            cand = permute_table(tt, k, prefix)
            if best is None or cand < best:
                best = cand
            return
        for arrangement in permutations(groups[idx]):
            expand(idx + 1, prefix + list(arrangement))
    expand(0, [])
    return best, True


# ---------------------------------------------------------------- classification

def classify(graph, cones_data, warnings):
    nl = Netlist(graph, warnings)
    expr_cache = {}
    records = []

    for cone in cones_data["cones"]:
        k = cone["n_leaves"]
        rec = {"id": cone["id"], "n_leaves": k, "class": None,
               "method": None, "key": None, "expr_nodes": None,
               "support": None, "n_support": None, "leaves_dropped": None,
               "unclassified_reason": None}

        node, extra = cone_function(nl, cone, expr_cache, warnings)
        if node is None:
            rec["unclassified_reason"] = extra
            warnings.append("cone %d: %s" % (cone["id"], extra))
            records.append(rec)
            continue

        leaf_nets = extra
        rec["expr_nodes"] = boolExpr.size(node)
        kvars = len(leaf_nets)

        # Which leaves the function actually depends on. Constant folding can
        # remove a leaf entirely -- logic gated off by a tied input, or a term
        # that cancels -- and a leaf outside the support cannot affect the cone
        # no matter how it is wired.
        support = []
        for i in sorted(boolExpr.variables(node)):
            kind, payload = nl.source_of(leaf_nets[i])
            if kind == "leaf":
                support.append(payload)
        rec["support"] = support
        rec["n_support"] = len(support)
        # How many of the leaves the wiring provides the function does not
        # actually use -- constants folded in, or terms that cancel.
        rec["leaves_dropped"] = max(0, k - len(support))

        # Equivalence classification is bounded by leaf count, but the support
        # above is not: it needs only the folded expression, so even a cone too
        # large to classify still reports exactly what it depends on.
        if k >= NORMALISE_MAX_LEAVES:
            rec["unclassified_reason"] = ("%d leaves is at or above the %d-leaf "
                                          "limit" % (k, NORMALISE_MAX_LEAVES))
            records.append(rec)
            continue

        if kvars <= EXACT_MAX_LEAVES:
            tt = truth_table(node, kvars)
            canon, proven = canonical_table(tt, kvars)
            rec["method"] = "truth_table"
            rec["key"] = "tt%d:%x" % (kvars, canon)
            rec["permutation_matched"] = proven
        else:
            rec["method"] = "normalised"
            rec["key"] = "nf%d:%s" % (kvars, boolExpr.key(node))
            rec["permutation_matched"] = False
        records.append(rec)

    groups = defaultdict(list)
    for rec in records:
        if rec["key"] is not None:
            groups[(rec["method"], rec["key"])].append(rec["id"])

    classes = []
    for (method, key), members in sorted(groups.items(),
                                         key=lambda kv: (-len(kv[1]), kv[0])):
        cid = len(classes)
        by_id = {r["id"]: r for r in records}
        classes.append({
            "id": cid,
            "method": method,
            "n_leaves": by_id[members[0]]["n_leaves"],
            "members": sorted(members),
            "representative": min(members),
            "key": key,
            "permutation_matched": all(by_id[m].get("permutation_matched", False)
                                       for m in members),
        })
        for m in members:
            by_id[m]["class"] = cid

    sizes = defaultdict(int)
    for c in classes:
        sizes[len(c["members"])] += 1
    summary = {
        "cones": len(records),
        "classified": sum(1 for r in records if r["class"] is not None),
        "unclassified": sum(1 for r in records if r["class"] is None),
        "classes": len(classes),
        "exact_classes": sum(1 for c in classes if c["method"] == "truth_table"),
        "normalised_classes": sum(1 for c in classes if c["method"] == "normalised"),
        "largest_class": max([len(c["members"]) for c in classes], default=0),
        "class_size_histogram": dict(sorted(sizes.items())),
        "cones_with_dropped_leaves": sum(1 for r in records
                                         if (r["leaves_dropped"] or 0) > 0),
        "total_leaves_dropped": sum(r["leaves_dropped"] or 0 for r in records),
    }
    return {"classes": classes, "cones": records, "summary": summary,
            "warnings": warnings}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("graph_json", nargs="?", default="NETLIST_GRAPH.json")
    ap.add_argument("cones_json", nargs="?", default="CONES.json")
    ap.add_argument("--out", default="CONE_CLASSES.json")
    args = ap.parse_args(argv)

    sys.setrecursionlimit(100000)
    with open(args.graph_json) as f:
        graph = json.load(f)
    with open(args.cones_json) as f:
        cones_data = json.load(f)

    warnings = []
    result = classify(graph, cones_data, warnings)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)
        f.write("\n")

    s = result["summary"]
    print("%s + %s -> %s" % (args.graph_json, args.cones_json, args.out))
    print("  %d cones: %d classified into %d classes, %d unclassified"
          % (s["cones"], s["classified"], s["classes"], s["unclassified"]))
    print("  %d exact (truth table), %d normalised"
          % (s["exact_classes"], s["normalised_classes"]))
    print("  largest class: %d cones; sizes: %s"
          % (s["largest_class"], s["class_size_histogram"]))
    print("  functional support: %d cones drop %d leaves the wiring suggests "
          "but the function ignores"
          % (s["cones_with_dropped_leaves"], s["total_leaves_dropped"]))
    for w in warnings[:10]:
        print("warning:", w)
    if len(warnings) > 10:
        print("... %d more warnings (see JSON)" % (len(warnings) - 10))
    return 0


if __name__ == "__main__":
    sys.exit(main())
