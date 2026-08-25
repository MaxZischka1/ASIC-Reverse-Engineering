"""
coneSignals.py -- invert the cone view: profile the SIGNALS instead, and name
the ones that act as shared control.

WHY
===
coneProfile.py describes each cone. That answers "what is this thing" but not
"what do these things have in common", and the second question is the one that
exposes structure: a bank of register bits is recognisable precisely because
every bit reads the SAME enable, the same reset, the same select -- and differs
only in its own data bit. Nothing in the cone-centric view names that shared
net, so nothing points at the enable.

This module builds the transpose. For every distinct leaf net it records who
consumes it, in which functional position, and how those consumers relate to
each other. A net read by all eleven members of one bank and by nothing else is
that bank's control line, and it is found by SET EQUALITY -- no threshold, no
tuning, no arbitrary cut-off. That is the whole idea.

WHAT IT READS
=============
`LOGIC_GRAPH.json` (cones and their sources), `CONE_GRAPH.json` (per-cone leaf
roles, giving the canonical position each leaf occupies, plus the label that
names the select and hold slots), and `CONE_PROFILE.json` (the class views, so
"bank" means something already computed rather than re-derived here). No
waveform, no layout.

HOW A SIGNAL GETS ITS ROLE
==========================
Roles are assigned from set relations against the candidate banks, which are
the `class_block` buckets of coneProfile.py -- the single shared definition of
a block, so this stage and coneBlocks agree by construction. In priority
order:

  bank_control  consumed by EVERY member of some bank, and by no cone outside
                it. A dedicated control line: the bank's enable, select or
                reset. This is an equality test between two sets, which is why
                it needs no threshold.
  shared_control consumed by every member of some bank AND by cones elsewhere.
                Real control, but not exclusive to one bank -- a global enable
                feeding several banks looks like this.
  broadcast     fanout at or above the derived control floor (see
                fanout_split) without covering any bank completely. Chip-wide
                state, decoded differently by each consumer.
  data          middling fanout, covers no bank. A register bit read by a few
                consumers.
  local         consumed by one or two cones.
  self_hold     consumed only by the cone its own flop drives. The hold path;
                per-bit by construction, so never shared.

POSITION STABILITY
==================
Canonical leaf positions are NOT comparable across groups. They come out of
coneClasses' per-function canonicalization, which orders leaves by cofactor
weight -- an ordering specific to the function being canonicalised -- so
position 3 in one group and position 3 in another mean nothing in common.
Comparing them globally produces noise that looks like signal: every wide net
appears "unstable" simply because it feeds several different functions.

So stability is measured WITHIN each group and then conjoined.
`positions_by_group` gives the position(s) a net occupies among the members of
each group it feeds, and `position_stable` is true when that is a single
position in every such group. That is the meaningful claim: a genuine control
line lands in the same functional slot in every member of a bank it drives.

Where a cone's label names its select slot (`load_enable(sel=~a, ...)`), the
net occupying that slot is flagged `is_select`. That is a second, independent
route to the same conclusion as the set-equality test, so the two agreeing is
corroboration and the two disagreeing is worth looking at.

CORRELATION
===========
The final section scores every pair of cones by Jaccard overlap of their leaf
sets and reports the clusters. Two cones sharing most of their inputs are doing
related work whatever their functions turn out to be -- which is exactly the
relation exact classification cannot see, and the reason 22 cones of one bank
came out as 22 singletons there.

NAMING
======
Names are derived, never invented. A port keeps its own name. A flop-sourced
net is named for the flop that drives it plus its role (`ctl_4182`, `d_5000`),
so a name is always traceable back to something real in the netlist. Nothing
here asserts what a signal MEANS -- only how it is used.

USAGE
=====
    python3 coneSignals.py [LOGIC_GRAPH.json] [--cone-graph CONE_GRAPH.json]
                           [--profile CONE_PROFILE.json]
                           [--out CONE_SIGNALS.json] [--md CONE_SIGNALS.md]
                           [--min-jaccard 0.6]
"""

import argparse
import json
import os
import re
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def position_letters(label):
    """Positions named in a label: 'load_enable(sel=~a, hold=d)' -> the select
    slot 0 and the hold slot 3. Letters index positions a=0, b=1, ... which is
    the convention coneGraph uses when it prints an expression."""
    out = {}
    for key, letter in re.findall(r"(\w+)=~?([a-z])\b", label or ""):
        out[key] = ord(letter) - ord("a")
    return out


def fanout_split(fanouts):
    """Where control stops and data starts, read off the data rather than
    fixed as a constant. Sorted distinct fanouts are scanned for the largest
    MULTIPLICATIVE gap; the split sits there. On the real puzzle the counts run
    ...,14,15,16,23,26,27,27,27,56,57,... and the widest ratio is 27->56, which
    separates the handful of chip-wide control lines from everything else. A
    hardcoded number would encode this design's shape into the tool; a gap
    scan re-derives it for whatever it is given. Returns the lowest fanout
    that counts as control."""
    vals = sorted(set(fanouts))
    if len(vals) < 3:
        return vals[-1] if vals else 0
    best, at = 0.0, vals[-1]
    for lo, hi in zip(vals, vals[1:]):
        ratio = hi / lo if lo else float("inf")
        if ratio > best:
            best, at = ratio, hi
    return at


def build(logic, cone_graph, profile, min_bank=3):
    cones = {c["id"]: c for c in logic["cones"]}
    flop_of_cone = {f["d_cone"]: f["instance"] for f in logic["flops"]
                    if f.get("d_cone")}
    prof = {p["id"]: p for p in profile["profiles"]}
    node_of = {n["id"]: n for n in cone_graph["nodes"]}
    group_of = cone_graph.get("cone_to_group", {})

    # Banks are coneProfile's `class_block` buckets. That field is the ONE
    # definition of a block (see coneProfile.block_key); reading it rather
    # than re-deriving one here is what stops this stage and coneBlocks from
    # disagreeing about what a bank is.
    all_banks = defaultdict(set)
    for p in profile["profiles"]:
        all_banks[p["class_block"]].add(p["id"])
    # Only banks of real width count for coverage. With 29 singleton and 7
    # two-member buckets, "covers a bank" is otherwise satisfied by almost
    # any net and the role assignment collapses to one value.
    banks = {b: m for b, m in all_banks.items() if len(m) >= min_bank}

    # net -> what consumes it, and in which slot
    consumers = defaultdict(set)
    positions = defaultdict(lambda: defaultdict(set))   # net -> group -> {pos}
    source_of = {}
    kind_of = {}
    select_nets = set()
    hold_nets = set()

    for cid, c in cones.items():
        gid = group_of.get(cid)
        node = node_of.get(gid) if gid else None
        named = position_letters(node.get("label") if node else "")
        sel_pos = named.get("sel")
        hold_pos = named.get("hold", named.get("q"))

        # canonical position per leaf net, where coneGraph recovered one
        pos_of_net = {}
        if node:
            for m in node["members"]:
                if m["id"] != cid:
                    continue
                for r in m.get("leaf_roles", []):
                    pos_of_net[r["net"]] = r["canonical_position"]

        for s in c["sources"]:
            if s["kind"] == "const":
                continue
            net = s["net"]
            consumers[net].add(cid)
            kind_of[net] = s["kind"]
            source_of[net] = s.get("instance") or s.get("from") or s.get("port")
            p = pos_of_net.get(net)
            if p is not None and gid:
                positions[net][gid].add(p)
                if sel_pos is not None and p == sel_pos:
                    select_nets.add(net)
                if hold_pos is not None and p == hold_pos:
                    hold_nets.add(net)

    control_floor = fanout_split([len(v) for v in consumers.values()])

    signals = []
    for net, cons in sorted(consumers.items(), key=lambda kv: -len(kv[1])):
        src = source_of.get(net)
        kind = kind_of.get(net)

        # Which banks does it cover completely?
        covered = [b for b, members in banks.items() if members <= cons]
        exclusive = [b for b in covered if cons <= banks[b]]
        spanned = {prof[c]["class_block"] for c in cons if c in prof}
        pos_by_group = {g: sorted(v) for g, v in positions.get(net, {}).items()}
        stable = all(len(v) == 1 for v in pos_by_group.values())

        own_cone = None
        for c in cons:
            if flop_of_cone.get(c) and flop_of_cone[c] == src:
                own_cone = c
        if len(cons) == 1 and own_cone:
            role = "self_hold"
        elif exclusive:
            role = "bank_control"
        elif covered:
            role = "shared_control"
        elif len(cons) >= control_floor:
            role = "broadcast"
        elif len(cons) <= 2:
            role = "local"
        else:
            role = "data"

        if kind == "input_port":
            name = src
        else:
            stem = (src or net).split(":")[0]
            prefix = {"bank_control": "ctl", "shared_control": "gctl",
                      "broadcast": "bcast", "local": "d",
                      "data": "d", "self_hold": "q"}[role]
            name = f"{prefix}_{stem}"

        signals.append({
            "net": net,
            "name": name,
            "kind": kind,
            "source": src,
            "role": role,
            "fanout": len(cons),
            "banks_covered": sorted(covered),
            "banks_exclusive": sorted(exclusive),
            "banks_spanned": len(spanned),
            "positions_by_group": pos_by_group,
            "position_stable": stable,
            "is_select": net in select_nets,
            "is_hold": net in hold_nets,
            "consumers": sorted(cons),
        })

    # Per-bank split: what every member reads, versus what is private.
    bank_report = []
    for b, members in sorted(banks.items(), key=lambda kv: -len(kv[1])):
        if len(members) < 2:
            continue
        leafsets = {m: {s["net"] for s in cones[m]["sources"]
                        if s["kind"] != "const"} for m in members}
        common = set.intersection(*leafsets.values())
        union = set.union(*leafsets.values())
        by_net = {s["net"]: s for s in signals}
        bank_report.append({
            "bank": b,
            "members": sorted(members),
            "width": len(members),
            "common_leaves": sorted(common),
            "common_named": [by_net[n]["name"] for n in sorted(common)
                             if n in by_net],
            "private_per_member": {m: sorted(leafsets[m] - common)
                                   for m in sorted(members)},
            "num_common": len(common),
            "num_union": len(union),
        })

    return signals, bank_report, cones, control_floor


def correlate(cones, min_j):
    leaf = {cid: {s["net"] for s in c["sources"] if s["kind"] != "const"}
            for cid, c in cones.items()}
    pairs = []
    ids = sorted(leaf)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            inter = len(leaf[a] & leaf[b])
            if not inter:
                continue
            j = inter / len(leaf[a] | leaf[b])
            if j >= min_j:
                pairs.append((round(j, 3), a, b, inter))
    # connected components over the surviving pairs
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, a, b, _ in pairs:
        parent[find(a)] = find(b)
    comps = defaultdict(list)
    for i in ids:
        comps[find(i)].append(i)
    clusters = sorted((sorted(v) for v in comps.values() if len(v) > 1),
                      key=len, reverse=True)
    return sorted(pairs, reverse=True), clusters


def write_md(path, signals, banks, pairs, clusters, min_j):
    L = ["# Cone signals", "",
         "Signals are leaf nets seen from the consumer side. Roles come from "
         "set relations against the banks in CONE_PROFILE.json, not from "
         "thresholds. See coneSignals.py for definitions.", ""]

    L += ["## Roles", ""]
    for r, n in Counter(s["role"] for s in signals).most_common():
        L.append(f"- {r}: {n}")
    L += ["", "## Control signals", "",
          "Nets that cover at least one whole bank. `exclusive` means the net "
          "is read by that bank and nothing else -- a dedicated control line.",
          "",
          "| name | net | source | role | fanout | banks covered | exclusive | groups | stable | select |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for s in signals:
        if s["role"] not in ("bank_control", "shared_control"):
            continue
        L.append("| {name} | {net} | {source} | {role} | {fanout} | {nc} | {ne} | {pos} | {st} | {sel} |".format(
            nc=len(s["banks_covered"]), ne=len(s["banks_exclusive"]),
            pos=len(s["positions_by_group"]) or "-",
            st="yes" if s["position_stable"] else "NO",
            sel="yes" if s["is_select"] else "", **s))

    L += ["", "## Broadcast signals", "",
          "| name | source | fanout | banks spanned | groups | stable |",
          "|---|---|---|---|---|---|"]
    for s in signals:
        if s["role"] != "broadcast":
            continue
        L.append("| {name} | {source} | {fanout} | {banks_spanned} | {pos} | {st} |".format(
            pos=len(s["positions_by_group"]) or "-",
            st="yes" if s["position_stable"] else "NO", **s))

    L += ["", "## Banks: shared versus private inputs", ""]
    for b in banks:
        L += [f"### `{b['bank']}` — {b['width']} members", "",
              f"- members: {', '.join(b['members'])}",
              f"- **{b['num_common']} leaves common to every member**"
              f" ({b['num_union']} in union)",
              f"- shared: {', '.join(b['common_named']) or '(none)'}", ""]
        widths = {m: len(v) for m, v in b["private_per_member"].items()}
        lo, hi = min(widths.values()), max(widths.values())
        L += [f"- private per member: {lo}" +
              (f"–{hi}" if hi != lo else "") + " leaf(s)", ""]

    L += [f"## Cone correlation (Jaccard ≥ {min_j})", "",
          f"{len(pairs)} pairs, {len(clusters)} clusters.", ""]
    for c in clusters:
        L.append(f"- **{len(c)} cones**: {', '.join(c[:14])}"
                 + (" ..." if len(c) > 14 else ""))
    L += ["", "### Strongest pairs", "",
          "| jaccard | a | b | shared leaves |", "|---|---|---|---|"]
    for j, a, b, n in pairs[:25]:
        L.append(f"| {j} | {a} | {b} | {n} |")
    L.append("")
    open(path, "w").write("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("logic_graph", nargs="?",
                    default=os.path.join(HERE, "LOGIC_GRAPH.json"))
    ap.add_argument("--cone-graph", default=os.path.join(HERE, "CONE_GRAPH.json"))
    ap.add_argument("--profile", default=os.path.join(HERE, "CONE_PROFILE.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "CONE_SIGNALS.json"))
    ap.add_argument("--md", default=os.path.join(HERE, "CONE_SIGNALS.md"))
    ap.add_argument("--min-jaccard", type=float, default=0.6)
    ap.add_argument("--min-bank", type=int, default=3,
                    help="minimum bank width for a net to count as covering it")
    args = ap.parse_args()

    logic = json.load(open(args.logic_graph))
    cone_graph = json.load(open(args.cone_graph))
    profile = json.load(open(args.profile))

    signals, banks, cones, control_floor = build(
        logic, cone_graph, profile, args.min_bank)
    pairs, clusters = correlate(cones, args.min_jaccard)

    stats = {
        "signals": len(signals),
        "roles": dict(Counter(s["role"] for s in signals)),
        "banks_with_shared_control": sum(1 for b in banks if b["num_common"]),
        "correlated_pairs": len(pairs),
        "correlation_clusters": len(clusters),
        "min_jaccard": args.min_jaccard,
        "control_floor": control_floor,
    }
    json.dump({"stats": stats, "signals": signals, "banks": banks,
               "clusters": clusters},
              open(args.out, "w"), indent=1)
    write_md(args.md, signals, banks, pairs, clusters, args.min_jaccard)

    print(f"{len(signals)} signals")
    for r, n in sorted(stats["roles"].items()):
        print(f"  {r:<16} {n:>4}")
    print(f"  banks with shared control: {stats['banks_with_shared_control']}"
          f" of {len(banks)}")
    print(f"  control floor (derived): fanout >= {control_floor}")
    print(f"  correlation: {len(pairs)} pairs, {len(clusters)} clusters")
    print(f"Wrote {args.out}")
    print(f"Wrote {args.md}")


if __name__ == "__main__":
    main()
