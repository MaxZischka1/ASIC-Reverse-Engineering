"""
coneClasses.py -- group per-flop fan-in cones by the Boolean function they
compute, not by the gates they happen to be built from.

Two register bits of the same datapath word are the same function even when
synthesis implemented one with AND/OR and the other with the De Morgan dual,
and even when the operand bits arrive on different pins. Neither difference
survives a truth table, so a truth table is what this compares.

WHAT IT READS
=============
`LOGIC_GRAPH.json`, exactly as logicGraph.py writes it. Three keys matter:

  cells       [[inst_id, {cell, kind, inputs:{pin:{net,inv}},
                          outputs:{pin:net}}], ...]
              `kind` is "comb" | "seq" | "const". `inputs[pin].inv` is the
              absorbed-inverter flag: logicGraph splices inverters out and
              records the inversion on the consuming pin, so a pin means
              "this net, complemented" when inv is true. Honouring it is not
              optional -- ignore it and 47 connections in the real puzzle
              evaluate to the wrong polarity.

  cones       [{id, sink:{kind,instance|port,pin,inv}, root_net, gates:[inst],
                sources:[{kind,net,...}], ...}, ...]
              `gates` is a flat instance list, NOT a DAG -- the edges are
              recovered here by resolving each gate input net back to its
              driver via `cells`. `sources` are the cone's leaves, already in
              a deterministic order (logicGraph sorts them by net id) which
              this module adopts as leaf order 0..k-1.

              A source with kind "const" carries `value` (1 for a conb HI pin,
              0 for LO). Constants are substituted as fixed columns rather
              than treated as free variables: a tied-low input is not a degree
              of freedom, and folding it lowers k, which is what makes the
              wide cones tractable. So k = number of NON-const sources.

              `sink.inv` applies to the cone's root. It is part of the
              function delivered to the flop's D pin, so it is applied.

              A cone with zero gates is legal (a flop fed straight from
              another flop); its function is the identity on one leaf.

  constants   only used to sanity-check the above; the per-source `value` is
              authoritative.

Cell behaviour comes from `sky130_prims.v`, parsed by cellLibrary.py, so a
cell added there is understood here with no edit. Pin symmetry is reused from
cellLibrary.CELLS, where it is *derived* by testing the truth table rather
than asserted, which is what tells the structural hash that an AND2's A/B may
be sorted but a MUX2's A0/A1/S may not.

HOW IT CLASSIFIES
=================
1. EVALUATE. Each cone's DAG is simulated in topological order with numpy
   bit-packed words: node values are uint64 arrays holding one bit per input
   assignment, so 64 assignments are evaluated per machine op and the whole
   2^k truth table is built by construction -- there is no Python loop over
   assignments anywhere. Leaf i is the column with period 2^(i+1), laid down
   as a broadcast constant (i < 6) or a per-word all-ones/all-zeros selection
   (i >= 6).

2. BUCKET BY k. Cones with different leaf counts cannot be equal; this is the
   O(n) pre-filter that runs before anything expensive.

3. CANONICALIZE UNDER LEAF PERMUTATION, so two cones that differ only in
   which physical bit landed on which pin still match:
     - small k (<= --brute-k): try all k! leaf orderings, keep the
       lexicographically smallest truth table. Complete, so no match is
       missed;
     - larger k: partition leaves by a FUNCTIONAL invariant -- the weight of
       the cofactor f|xi=1, refined by the second-order weights f|xi=1,xj=1 --
       and permute only within a tied class. Permuting leaves permutes these
       weights and nothing else, so ordering classes by the invariant is a
       canonical order that two cones share whenever they compute the same
       function, HOWEVER differently they were built. That last property is
       why the invariant is functional and not structural: a De Morgan
       variant is a different graph computing the same function, so a
       Weisfeiler-Leman colour would split leaves that must stay together and
       would make exactly the cross-implementation matches this module exists
       to find unreachable;
     - a tied class whose leaves are genuinely interchangeable (every
       transposition preserves the truth table -- the inputs of a reduce tree,
       typically) collapses to ONE ordering instead of |C|!, since all of them
       yield identical bytes. This is what keeps the search affordable;
     - if a class is still too large, it is split further by the structural
       Weisfeiler-Leman colour (refine_leaves). That narrows the search and so
       may lose a cross-implementation match, but every match it still reports
       is truth-table verified;
     - anything wider than --max-eval-k, or still over --perm-cap, falls back
       to structural-hash-only and is flagged as such.
   The permutation that won is kept: it is the leaf correspondence, i.e. which
   physical flop/port plays which functional role, which is what makes bus
   recovery possible downstream.

4. GROUP BY SIGNATURE. blake2b over the canonical truth table, bucketed
   through a dict -- O(n) per bucket, never pairwise.

5. STRUCTURAL HASH, computed for every cone regardless of k: a bottom-up hash
   of cell class + per-pin (inv, child hash), with children sorted only inside
   a symmetry group. It is the only classifier available to cones that fall
   out at 3c.

   NOTE it is deliberately NOT used to pre-partition candidates for step 3.
   A De Morgan variant and a pin-swapped instance hash differently on purpose,
   so partitioning on it first would hide exactly the matches step 3 exists to
   find. It is used instead as a MEMO KEY: equal structural hash implies an
   equal truth table under equal leaf order, so the canonical form is computed
   once per distinct structure and reused. Same saving, nothing lost.

SOUNDNESS
=========
Every reported FUNCTIONAL group is exact: membership is decided by comparing
complete truth tables, so there are no false positives even when the leaf
search was restricted by refinement. Restricting the search can only cause a
genuine equivalence to be MISSED (reported as two groups instead of one), never
invented -- refinement is a completeness compromise, not a soundness one.
Groups flagged "structural" are heuristic in the other direction: same shape,
function never checked.

USAGE
=====
    python3 coneClasses.py [LOGIC_GRAPH.json] [--out CONE_CLASSES.json]
                           [--prims sky130_prims.v] [--max-eval-k 20]
                           [--brute-k 8] [--perm-cap 200000] [--verbose]
"""

import argparse
import hashlib
import itertools
import json
import os
import sys
from collections import defaultdict

import numpy as np

from cellLibrary import CELLS, cell_class

WORD_BITS = 64

# Leaf columns for i < 6 repeat inside a single 64-bit word, because the
# pattern index p = w*64 + b has (p >> i) & 1 == (b >> i) & 1 for i < 6.
_LEAF_WORD = [
    0xAAAAAAAAAAAAAAAA,  # i=0, period 2
    0xCCCCCCCCCCCCCCCC,  # i=1, period 4
    0xF0F0F0F0F0F0F0F0,  # i=2, period 8
    0xFF00FF00FF00FF00,  # i=3, period 16
    0xFFFF0000FFFF0000,  # i=4, period 32
    0xFFFFFFFF00000000,  # i=5, period 64
]


# ---------------------------------------------------------------------------
# Cell library: numpy-evaluable functions compiled from sky130_prims.v
# ---------------------------------------------------------------------------
# cellLibrary.py already compiles these cells, but its lambdas end in `& 1` to
# keep a scalar result one bit wide. That mask is wrong for bit-packed words
# (it would keep only pattern 0), so the expressions are recompiled here
# without it. Symmetry and pin order are still taken from cellLibrary.CELLS,
# which derives them by testing the function rather than asserting them.

def verilog_to_numpy(expr):
    """Rewrite a 1-bit Verilog expression as a numpy-evaluable Python one.

    & | ^ ~ and parentheses apply unchanged to uint64 arrays. The library's
    single conditional (the mux) is rewritten to bitwise select, because
    `A1 if S else A0` on an array raises rather than selecting per bit.
    """
    import re
    expr = expr.strip()
    ternary = re.fullmatch(r"(.+?)\?(.+?):(.+)", expr, re.DOTALL)
    if ternary:
        cond, then, other = (p.strip() for p in ternary.groups())
        if any("?" in p for p in (cond, then, other)):
            raise ValueError(f"nested conditional not supported: {expr}")
        return f"((({other}) & ~({cond})) | (({then}) & ({cond})))"
    return expr


def build_numpy_library(prims_path):
    """{cell class: (input pin names, callable(**pins) -> uint64 array)}.

    Only combinational cells with a primary output get an entry; sequential
    cells and the constant generator are never evaluated as cone gates.
    """
    import re
    with open(prims_path) as f:
        text = f.read()

    lib = {}
    for name, ports, body in re.findall(
            r"module\s+(\w+)\s*\((.*?)\);(.*?)endmodule", text, re.DOTALL):
        if re.search(r"\balways\b", body):
            continue
        declared = re.findall(r"\b(input|output)\s+(?:reg\s+|wire\s+)?(\w+)", ports)
        ins = tuple(p for kind, p in declared if kind == "input")
        outs = tuple(p for kind, p in declared if kind == "output")
        if not outs or not ins:
            continue                      # conb (no inputs), diode (no output)
        assigns = dict(re.findall(r"assign\s+(\w+)\s*=\s*(.*?);", body, re.DOTALL))
        if outs[0] not in assigns:
            continue
        expr = verilog_to_numpy(assigns[outs[0]])
        if "'" in expr:
            continue                      # a literal driver, not a gate
        src = f"lambda {', '.join(ins)}: ({expr})"
        fn = eval(src, {"__builtins__": {}})  # noqa: S307 -- own library only
        cls = cell_class(name)
        lib.setdefault(cls, (ins, fn))
    return lib


# ---------------------------------------------------------------------------
# Cone -> DAG
# ---------------------------------------------------------------------------

class ConeDag:
    """One cone reassembled as an evaluable DAG.

    leaves      [source dict], in logicGraph's order, constants removed
    leaf_index  net -> leaf position 0..k-1
    consts      net -> 0/1 for the substituted constant sources
    order       gate instance ids in topological order (leaves first)
    inputs      gate -> [(pin, net, inv)] in the cell's declared pin order
    root        (net, inv) delivered to the sink
    """

    def __init__(self, cone, cells, lib):
        self.cone = cone
        self.cells = cells

        srcs = cone["sources"]
        self.consts = {s["net"]: int(s.get("value", 0))
                       for s in srcs if s["kind"] == "const"}
        self.leaves = [s for s in srcs if s["kind"] != "const"]
        self.leaf_index = {s["net"]: i for i, s in enumerate(self.leaves)}
        self.k = len(self.leaves)

        inside = set(cone["gates"])
        self.inputs = {}
        for g in inside:
            cell = cells[g]
            cls = cell_class(cell["cell"])
            pins = lib[cls][0] if cls in lib else tuple(cell["inputs"])
            self.inputs[g] = [(p, cell["inputs"][p]["net"],
                               bool(cell["inputs"][p]["inv"]))
                              for p in pins if p in cell["inputs"]]

        # Topological order by DFS over driver edges that stay inside the cone.
        driver = {}
        for g in inside:
            for pin, net in cells[g]["outputs"].items():
                if net is not None:
                    driver[net] = g
        self.order = []
        seen, stack = set(), []
        for g in sorted(inside):
            if g in seen:
                continue
            stack.append((g, False))
            while stack:
                node, done = stack.pop()
                if done:
                    self.order.append(node)
                    continue
                if node in seen:
                    continue
                seen.add(node)
                stack.append((node, True))
                for _, net, _ in self.inputs[node]:
                    d = driver.get(net)
                    if d is not None and d not in seen:
                        stack.append((d, False))
        self.driver = driver

        sink = cone["sink"]
        self.root_net = cone["root_net"]
        self.root_inv = bool(sink.get("inv", False))


# ---------------------------------------------------------------------------
# Step 1: bit-packed evaluation
# ---------------------------------------------------------------------------

def leaf_column(i, words, n_patterns):
    """Packed column for leaf i: bit p is (p >> i) & 1, for p in [0, 2^k)."""
    if i < 6:
        col = np.full(words, _LEAF_WORD[i], dtype=np.uint64)
    else:
        w = np.arange(words, dtype=np.uint64)
        hi = ((w >> np.uint64(i - 6)) & np.uint64(1)).astype(bool)
        col = np.zeros(words, dtype=np.uint64)
        col[hi] = np.uint64(0xFFFFFFFFFFFFFFFF)
    if n_patterns < WORD_BITS:
        col &= np.uint64((1 << n_patterns) - 1)
    return col


def evaluate(dag, lib):
    """Simulate the cone over every input assignment at once.

    Returns the packed uint64 truth table of the function delivered to the
    sink, or None if the cone contains a cell the library cannot evaluate.
    """
    k, n = dag.k, 1 << dag.k
    words = max(1, (n + WORD_BITS - 1) // WORD_BITS)
    tail = np.uint64((1 << n) - 1) if n < WORD_BITS else None

    ZERO = np.zeros(words, dtype=np.uint64)
    ONE = np.full(words, 0xFFFFFFFFFFFFFFFF, dtype=np.uint64)
    if tail is not None:
        ONE = ONE & tail

    values = {}
    for net, i in dag.leaf_index.items():
        values[net] = leaf_column(i, words, n)
    for net, v in dag.consts.items():
        values[net] = ONE if v else ZERO

    def read(net, inv):
        v = values.get(net)
        if v is None:
            return None
        if inv:
            v = ~v
            if tail is not None:
                v = v & tail
        return v

    for g in dag.order:
        cell = dag.cells[g]
        cls = cell_class(cell["cell"])
        entry = lib.get(cls)
        if entry is None:
            return None
        pins, fn = entry
        args = {}
        for pin, net, inv in dag.inputs[g]:
            v = read(net, inv)
            if v is None:
                return None
            args[pin] = v
        if len(args) != len(pins):
            return None
        out = fn(**args)
        if tail is not None:
            out = out & tail
        for pin, net in cell["outputs"].items():
            if net is not None:
                values[net] = out

    return read(dag.root_net, dag.root_inv)


def unpack(table, n_patterns):
    """Packed uint64 words -> uint8 array of length 2^k, index == pattern."""
    b = table.astype("<u8").view(np.uint8)
    bits = np.unpackbits(b, bitorder="little")
    return bits[:n_patterns]


# ---------------------------------------------------------------------------
# Step 5: structural hash + Weisfeiler-Leman leaf refinement
# ---------------------------------------------------------------------------

def _h(*parts):
    d = hashlib.blake2b(digest_size=16)
    for p in parts:
        d.update(str(p).encode())
        d.update(b"\x1f")
    return d.hexdigest()


def sym_groups(cls, pins):
    """position -> symmetry-group id, from cellLibrary's derived symmetry.
    Positions in the same group may be sorted; everything else keeps order."""
    spec = CELLS.get(cls)
    gid = {i: i for i in range(len(pins))}
    if spec:
        for g, group in enumerate(spec.sym):
            for pos in group:
                gid[pos] = -1 - g
    return gid


def node_hashes(dag, leaf_colour):
    """Bottom-up structural hash of every gate, given a colour per leaf net."""
    hashes = {}
    for net, i in dag.leaf_index.items():
        hashes[net] = leaf_colour[i]
    for net, v in dag.consts.items():
        hashes[net] = _h("const", v)

    for g in dag.order:
        cell = dag.cells[g]
        cls = cell_class(cell["cell"])
        pins = [p for p, _, _ in dag.inputs[g]]
        gid = sym_groups(cls, pins)
        items = defaultdict(list)
        for pos, (pin, net, inv) in enumerate(dag.inputs[g]):
            items[gid[pos]].append(_h(inv, hashes.get(net, "?")))
        parts = []
        for key in sorted(items, key=lambda x: (x >= 0, x)):
            vals = items[key]
            parts.append(tuple(sorted(vals)) if key < 0 else tuple(vals))
        hv = _h(cls, *parts)
        hashes[g] = hv
        for pin, net in cell["outputs"].items():
            if net is not None:
                hashes[net] = hv
    return hashes


def structural_hash(dag):
    """Whole-cone structural hash, with every free leaf given the SAME colour.

    That makes it invariant under leaf permutation, which is what we want for
    grouping "same shape" cones -- but it also means it says nothing about
    which leaf landed on which pin, so it can NOT certify that two cones have
    the same truth table in their own leaf orders. Use positional_hash() for
    that. (Conflating the two silently mis-assigns leaf roles: two shift-
    register bits have identical shape but opposite A0/A1 wiring.)
    """
    hashes = node_hashes(dag, ["leaf"] * dag.k)
    return _h("root", dag.root_inv, hashes.get(dag.root_net, "?"), dag.k)


def positional_hash(dag):
    """Structural hash with leaf i coloured by its position i.

    Equal positional hash means the two DAGs are isomorphic *with leaves in
    corresponding positions*, which does imply an identical truth table under
    the cones' own leaf orders -- so this, and only this, is sound as a memo
    key for the canonicalisation result.
    """
    hashes = node_hashes(dag, [_h("leaf", i) for i in range(dag.k)])
    return _h("root", dag.root_inv, hashes.get(dag.root_net, "?"), dag.k)


def refine_leaves(dag, rounds=4):
    """Weisfeiler-Leman colour refinement over the cone DAG.

    Returns a list of leaf-index classes. Two leaves land in different classes
    only if some purely structural property separates them, so permuting
    within a class is the only permuting that can possibly matter -- and the
    restriction can lose an equivalence but never invent one, because every
    surviving match is still checked against the full truth table.
    """
    colour = ["leaf"] * dag.k

    # Consumers of each leaf net, with the pin context they arrive on.
    consumers = defaultdict(list)
    for g in dag.order:
        cell = dag.cells[g]
        cls = cell_class(cell["cell"])
        pins = [p for p, _, _ in dag.inputs[g]]
        gid = sym_groups(cls, pins)
        for pos, (pin, net, inv) in enumerate(dag.inputs[g]):
            if net in dag.leaf_index:
                consumers[dag.leaf_index[net]].append((g, gid[pos], inv))

    prev = None
    for _ in range(rounds):
        hashes = node_hashes(dag, colour)
        new = []
        for i in range(dag.k):
            ctx = sorted(_h(hashes.get(g, "?"), grp, inv)
                         for g, grp, inv in consumers.get(i, []))
            new.append(_h(colour[i], len(ctx), *ctx))
        sig = tuple(sorted(set(new)))
        colour = new
        if prev is not None and len(sig) == len(prev):
            break
        prev = sig

    classes = defaultdict(list)
    for i, c in enumerate(colour):
        classes[c].append(i)
    # Ordered BY COLOUR, not by leaf index. The colour is derived purely from
    # cone structure, so this order is identical for isomorphic cones whatever
    # order their nets happened to be named in -- which is what lets two
    # instances of the same bit-slice land on the same canonical form. Sorting
    # by leaf index instead would pin each class to its own net-id positions
    # and only the identity permutation would ever be reachable.
    return [sorted(v) for _, v in sorted(classes.items())]


# ---------------------------------------------------------------------------
# Step 3: canonicalisation under leaf permutation
# ---------------------------------------------------------------------------

def functional_classes(bits, k, bitmasks, second_order=True):
    """Partition leaves by an invariant read off the FUNCTION, not the gates.

    The first-order invariant of leaf i is the weight (popcount) of the
    positive cofactor f|xi=1. Permuting the leaves permutes these weights and
    nothing else, so sorting leaves by weight is a canonical order; leaves
    that tie are the only ones that still need searching. Ties are then split
    by the second-order invariant -- the sorted multiset of |f|xi=1,xj=1| over
    the other leaves -- which separates most remaining cases.

    Why this and not the structural (Weisfeiler-Leman) refinement: a De Morgan
    variant is a DIFFERENT graph computing the SAME function, so a structural
    colour splits leaves that a functional invariant keeps together. Refining
    on structure would make exactly the cross-implementation matches this
    module exists to find unreachable. WL refinement is still used for cones
    whose function is never evaluated (the structural-only fallback).

    Returns classes ordered by their invariant, so the ordering is identical
    for any two cones computing the same function however they are built.
    """
    pos = [bitmasks[i].astype(bool) for i in range(k)]
    first = [int(bits[pos[i]].sum()) for i in range(k)]

    if second_order:
        inv = []
        for i in range(k):
            pair = sorted(int(bits[pos[i] & pos[j]].sum())
                          for j in range(k) if j != i)
            inv.append((first[i], tuple(pair)))
    else:
        inv = [(w, ()) for w in first]

    classes = defaultdict(list)
    for i, key in enumerate(inv):
        classes[key].append(i)
    return [sorted(v) for _, v in sorted(classes.items())]


def swap_index(k, i, j):
    """Index array remapping the truth table by exchanging leaves i and j."""
    q = np.arange(1 << k, dtype=np.int64)
    d = ((q >> i) & 1) ^ ((q >> j) & 1)
    return q ^ (d << i) ^ (d << j)


def class_orderings(bits, k, classes):
    """For each leaf class, the orderings that actually need trying.

    A class whose leaves are genuinely interchangeable -- every transposition
    inside it leaves the truth table unchanged -- contributes exactly ONE
    ordering, because all |C|! of them produce the identical table. That is
    the common case for the inputs of an AND/OR reduce tree, where searching
    9! orderings would be 362880 ways of computing the same bytes. Since the
    symmetric group is generated by its transpositions, checking every pair is
    enough to prove the whole class symmetric.
    """
    out = []
    for c in classes:
        n = len(c)
        if n == 1:
            out.append([(0,)])
            continue
        symmetric = True
        for a in range(n):
            for b in range(a + 1, n):
                if not np.array_equal(bits[swap_index(k, c[a], c[b])], bits):
                    symmetric = False
                    break
            if not symmetric:
                break
        out.append([tuple(range(n))] if symmetric
                   else list(itertools.permutations(range(n))))
    return out


def perm_iter(classes, k, orderings=None):
    """All leaf orderings allowed by the class partition.

    Classes arrive in canonical (colour) order and are laid down on
    CONTIGUOUS blocks of canonical bit positions: the first class takes
    positions 0..|C0|-1, the next |C0|..|C0|+|C1|-1, and so on. Only the
    order *within* a block is searched. Assigning each class back onto its
    own leaf indices instead would leave the between-class order fixed at
    whatever the net names happened to sort to, so two isomorphic cones with
    differently-named nets could never reach a common canonical form.
    """
    bases, off = [], 0
    for c in classes:
        bases.append(off)
        off += len(c)
    per_class = (orderings if orderings is not None
                 else [list(itertools.permutations(range(len(c)))) for c in classes])
    for combo in itertools.product(*per_class):
        pi = [0] * k
        for cls, base, order in zip(classes, bases, combo):
            for leaf, slot in zip(cls, order):
                pi[leaf] = base + slot
        yield tuple(pi)


def perm_count(orderings):
    total = 1
    for o in orderings:
        total *= len(o)
        if total > 10 ** 12:
            return total
    return total


def canonical(table_bits, k, classes, bitmasks, orderings=None):
    """Smallest truth table over the allowed leaf orderings, and the ordering
    that produced it. `bitmasks[i]` is the precomputed column ((q>>i)&1)."""
    best_bytes, best_pi = None, None
    for pi in perm_iter(classes, k, orderings):
        idx = np.zeros(1 << k, dtype=np.int64)
        for i in range(k):
            idx |= bitmasks[pi[i]] << i
        cand = table_bits[idx]
        cb = cand.tobytes()
        if best_bytes is None or cb < best_bytes:
            best_bytes, best_pi = cb, pi
    return best_bytes, best_pi


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def classify(logic_graph, prims_path, max_eval_k=20, brute_k=8,
             perm_cap=200000, verbose=False):
    cells = dict(logic_graph["cells"])
    lib = build_numpy_library(prims_path)
    cones = logic_graph["cones"]

    records = []
    for cone in cones:
        dag = ConeDag(cone, cells, lib)
        rec = {
            "id": cone["id"],
            "k": dag.k,
            "num_gates": cone["num_gates"],
            "struct": structural_hash(dag),
            "struct_pos": positional_hash(dag),
            "dag": dag,
            "leaves": dag.leaves,
            "func": None,
            "perm": None,
            "method": None,
            "reason": None,
        }
        records.append(rec)

    # --- functional pass, bucketed by k -----------------------------------
    by_k = defaultdict(list)
    for r in records:
        by_k[r["k"]].append(r)

    # Memo keyed on the POSITIONAL hash: isomorphic DAG with leaves in
    # corresponding positions => identical truth table in the cone's own leaf
    # order => identical canonical form and permutation. The plain structural
    # hash must never be used here (see structural_hash's docstring).
    memo = {}
    for k in sorted(by_k):
        bucket = by_k[k]
        if k > max_eval_k:
            for r in bucket:
                r["method"] = "structural"
                r["reason"] = f"k={k} exceeds --max-eval-k={max_eval_k}"
            continue

        n = 1 << k
        bitmasks = [((np.arange(n, dtype=np.int64) >> i) & 1) for i in range(k)]

        for r in bucket:
            hit = memo.get(r["struct_pos"])
            if hit is not None:
                r["func"], r["perm"], r["method"] = hit[0], hit[1], "function"
                continue

            packed = evaluate(r["dag"], lib)
            if packed is None:
                r["method"] = "structural"
                r["reason"] = "cone contains a cell with no evaluable model"
                continue
            bits = unpack(packed, n)

            if k <= brute_k:
                classes = [list(range(k))]
                how = "brute"
            else:
                classes = functional_classes(bits, k, bitmasks)
                how = "cofactor"
            orderings = class_orderings(bits, k, classes)
            total = perm_count(orderings)

            if total > perm_cap and how != "brute":
                # Last resort before giving up on the function entirely: split
                # the still-too-large functional classes further by the
                # structural (Weisfeiler-Leman) colour. This can only make the
                # search smaller, so it can lose a cross-implementation match
                # -- but a narrowed search still verifies every match it does
                # report against the full truth table, which beats dropping to
                # structure-only for the whole cone.
                wl = {i: c for c, leaves in enumerate(refine_leaves(r["dag"]))
                      for i in leaves}
                split = []
                for c in classes:
                    sub = defaultdict(list)
                    for i in c:
                        sub[wl.get(i, -1)].append(i)
                    split.extend(sorted(v) for _, v in sorted(sub.items()))
                if len(split) > len(classes):
                    classes = split
                    orderings = class_orderings(bits, k, classes)
                    total = perm_count(orderings)
                    how = "cofactor+wl"

            if total > perm_cap:
                r["method"] = "structural"
                r["reason"] = (f"{total} leaf orderings after {how} refinement "
                               f"exceeds --perm-cap={perm_cap}")
                continue

            sig_bytes, pi = canonical(bits, k, classes, bitmasks, orderings)
            sig = hashlib.blake2b(sig_bytes, digest_size=16).hexdigest()
            r["func"], r["perm"], r["method"] = sig, pi, "function"
            r["search"] = how
            r["orderings"] = total
            memo[r["struct_pos"]] = (sig, pi)

    # --- group -------------------------------------------------------------
    groups = defaultdict(list)
    for r in records:
        key = (("func", r["k"], r["func"]) if r["method"] == "function"
               else ("struct", r["k"], r["struct"]))
        groups[key].append(r)

    out = []
    for (kind, k, sig), members in sorted(
            groups.items(), key=lambda kv: (-len(kv[1]), kv[0][1], kv[0][2])):
        ref = members[0]
        entry = {
            "signature": sig,
            "verified_by": "function" if kind == "func" else "structural",
            "k": k,
            "size": len(members),
            "reference_cone": ref["id"],
            "cones": [],
        }
        if kind == "struct":
            entry["fallback_reason"] = ref["reason"]
        for m in members:
            item = {"id": m["id"], "num_gates": m["num_gates"]}
            if m["perm"] is not None:
                item["leaf_roles"] = [
                    {"canonical_position": m["perm"][i],
                     "kind": leaf["kind"],
                     "net": leaf["net"],
                     "from": leaf.get("instance") or leaf.get("port")}
                    for i, leaf in enumerate(m["leaves"])
                ]
            entry["cones"].append(item)
        out.append(entry)
    return out, records


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("logic_graph", nargs="?",
                   default=os.path.join(here, "LOGIC_GRAPH.json"))
    p.add_argument("--prims", default=os.path.join(here, "sky130_prims.v"))
    p.add_argument("--out", default=os.path.join(here, "CONE_CLASSES.json"))
    p.add_argument("--max-eval-k", type=int, default=20,
                   help="cones with more free leaves are structural-only")
    p.add_argument("--brute-k", type=int, default=8,
                   help="at or below this k, try all k! leaf orderings")
    p.add_argument("--perm-cap", type=int, default=200000,
                   help="give up on permutation search past this many orderings")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    with open(args.logic_graph) as f:
        lg = json.load(f)

    groups, records = classify(lg, args.prims, args.max_eval_k, args.brute_k,
                               args.perm_cap, args.verbose)

    n_fn = sum(1 for r in records if r["method"] == "function")
    n_st = len(records) - n_fn
    fn_groups = [g for g in groups if g["verified_by"] == "function"]
    st_groups = [g for g in groups if g["verified_by"] == "structural"]

    print(f"{len(records)} cones -> {len(groups)} classes "
          f"({len(fn_groups)} function-verified, {len(st_groups)} structural-only)")
    print(f"  {n_fn} cones classified by truth table, {n_st} by structure alone")
    multi = [g for g in groups if g["size"] > 1]
    print(f"  {len(multi)} class(es) with more than one member:")
    for g in multi:
        tag = "fn " if g["verified_by"] == "function" else "st "
        ids = ", ".join(c["id"] for c in g["cones"][:6])
        more = "" if g["size"] <= 6 else f" ... (+{g['size'] - 6})"
        print(f"    {tag}k={g['k']:<3} x{g['size']:<3} {ids}{more}")
    if args.verbose:
        for g in st_groups:
            print(f"  structural-only: {g['reference_cone']} -- {g.get('fallback_reason')}")

    with open(args.out, "w") as f:
        json.dump({"groups": groups,
                   "stats": {"cones": len(records),
                             "classes": len(groups),
                             "function_verified_cones": n_fn,
                             "structural_only_cones": n_st}}, f, indent=1)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
