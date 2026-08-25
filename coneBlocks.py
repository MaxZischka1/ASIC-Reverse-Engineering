"""
coneBlocks.py -- the first level of abstraction above the cone: split the
design into a CONTROL domain and a DATA domain, then sort each domain into
blocks of a recognised kind (shifter, register, matcher, buffer, ...).

WHY A TWO-WAY SPLIT FIRST
========================
Everything above the cone needs to know which signals steer and which signals
are steered. It is the one distinction that makes the rest legible: a shift
register and its enable look identical as cones, and only differ in how the
rest of the design uses them. So the domain split runs first, and block kinds
are decided inside a domain rather than across the whole netlist.

FANOUT IS THE PRIMARY SIGNAL, BUT IT IS NOT SUFFICIENT
======================================================
Control fans out and data does not, and on the real puzzle the counts really
are bimodal -- most flops feed 2-4 cones, a handful feed 20-88. But the bands
OVERLAP where it matters: in the 20-29 range this design has 4 flops that
cover whole banks (control) and 5 that do not (data). A pure fanout threshold
has to get those wrong in one direction or the other, whatever value it picks.

So fanout is weighted heavily and then corroborated. The score is a small sum
of signed pieces of evidence, and every piece is recorded on the block, so a
classification can always be read back and argued with rather than taken on
trust. Nothing here is a black box and nothing is a tuned constant: the fanout
cut itself comes from coneSignals' derived control floor, not from a literal.

  toward CONTROL
    +3  the net covers at least one whole bank (coneSignals: *_control)
    +3  the net occupies a select/enable slot in some cone's decomposition
    +2  fanout at or above the derived control floor
    +1  fanout above the median

  toward DATA
    +3  the cone drives a primary output port
    +2  the cone is a member of a bank of width >= 3
    +2  the net is a PRIVATE leaf of a bank (read by one member only)
    +1  fanout at or below 4

A cone is CONTROL only at score >= 2. That threshold exists because "+1 fanout
above the median" fires on anything feeding 5 or more cones, and the median
here is 4 -- on its own it swept 22 cones into the control domain that had no
other evidence whatsoever. One weak signal must not decide a domain, so the
weak evidence can only corroborate: reaching 2 requires either a substantive
piece (bank coverage, select use, fanout past the floor) or two weak ones.

Ties break to data, the conservative direction: calling a control bit "data"
leaves it as an unexplained singleton, whereas calling a data bit "control"
invents a steering relationship that is not there. Each cone keeps its score
and a `confidence` of strong (|score| >= 3) or weak (|score| < 3), so the
borderline calls stay visible instead of hiding inside a binary.

BLOCK KINDS
===========
A block is coneProfile's `class_block` -- that module owns the definition (see
coneProfile.block_key), and this stage reads it rather than deriving its own,
so the two cannot drift. Each block is then given a kind:

  buffer      every member is a pass-through (no gates)
  shifter     the members form a CHAIN: the "reads another member's Q"
              relation over the block is injective and acyclic, i.e. a path.
              Detected on raw nets, deliberately NOT on canonical leaf
              positions, which are per-function and not comparable between
              groups (see coneSignals).
  register    members hold, and their data arrives from outside the block
  matcher     the function is a sticky set on an AND of literals -- a compare
              against a constant that latches once hit
  clearable   a dominant-clear gate on a held value
  toggle      the members toggle on a condition
  decoder     the block drives primary outputs from a shared leaf set
  feedback    the members are one strongly-connected component: mutually
              recursive state (counter, LFSR, state register). Says the bits
              belong together, NOT what they compute
  gate        every member is a single primitive combinational gate
  mixed       members disagree about their kind
  unknown     no rule fired

`shifter` and `register` are the same label family and are separated purely by
where the data comes from, which is why the chain test is done structurally
rather than read off the label.

USAGE
=====
    python3 coneBlocks.py [LOGIC_GRAPH.json] [--profile CONE_PROFILE.json]
                          [--signals CONE_SIGNALS.json]
                          [--cone-graph CONE_GRAPH.json]
                          [--out CONE_BLOCKS.json] [--md CONE_BLOCKS.md]
"""

import argparse
import json
import os
import statistics
from collections import Counter, defaultdict

from coneProfile import label_family

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Domain: control vs data
# ---------------------------------------------------------------------------

def classify_domains(profiles, signals, logic, control_floor):
    """Score every cone. Returns {cone_id: (domain, score, evidence)}."""
    q_of = {f["d_cone"]: f["q_net"] for f in logic["flops"] if f.get("d_cone")}
    sig = {s["net"]: s for s in signals}

    # A net is a bank's PRIVATE leaf when exactly one member of that bank
    # reads it -- the per-bit data input, as opposed to the shared control.
    banks = defaultdict(set)
    for p in profiles:
        banks[p["class_block"]].add(p["id"])
    wide = {b: m for b, m in banks.items() if len(m) >= 3}
    private = set()
    cones = {c["id"]: c for c in logic["cones"]}
    for members in wide.values():
        counts = Counter()
        for m in members:
            for s in cones[m]["sources"]:
                if s["kind"] != "const":
                    counts[s["net"]] += 1
        private |= {n for n, k in counts.items() if k == 1}

    fanouts = [p["q_fanout"] for p in profiles]
    median = statistics.median(fanouts) if fanouts else 0

    out = {}
    for p in profiles:
        cid = p["id"]
        net = q_of.get(cid)
        s = sig.get(net) if net else None
        ev = []
        score = 0

        if s and s["role"] in ("bank_control", "shared_control"):
            score += 3; ev.append("+3 covers a whole bank")
        if s and s["is_select"]:
            score += 3; ev.append("+3 used as a select/enable")
        if p["q_fanout"] >= control_floor:
            score += 2; ev.append(f"+2 fanout {p['q_fanout']} >= floor {control_floor}")
        elif p["q_fanout"] > median:
            score += 1; ev.append(f"+1 fanout {p['q_fanout']} > median {median}")

        if p["drives_output"]:
            score -= 3; ev.append("-3 drives a primary output")
        if len(banks[p["class_block"]]) >= 3:
            score -= 2; ev.append("-2 member of a bank of width >= 3")
        if net and net in private:
            score -= 2; ev.append("-2 private leaf of a bank")
        if p["q_fanout"] <= 4:
            score -= 1; ev.append(f"-1 fanout {p['q_fanout']} <= 4")

        domain = "control" if score >= 2 else "data"
        confidence = "strong" if abs(score) >= 3 else "weak"
        out[cid] = (domain, score, ev, confidence)
    return out


# ---------------------------------------------------------------------------
# Kind
# ---------------------------------------------------------------------------

def chain_of(members, cones, q_of):
    """The 'reads another member's Q' relation, restricted to the block.

    Returns (edges, is_path). A shift register makes this relation injective
    on both sides and acyclic -- a path through the block. Computed on raw
    nets so it does not depend on canonical leaf positions, which are
    per-function and not comparable across groups."""
    qnet = {q_of[m]: m for m in members if m in q_of}
    edges = []
    for m in members:
        for s in cones[m]["sources"]:
            if s["kind"] != "flop":
                continue
            src = qnet.get(s["net"])
            if src and src != m:
                edges.append((src, m))
    srcs = [a for a, _ in edges]
    dsts = [b for _, b in edges]
    injective = len(set(srcs)) == len(edges) and len(set(dsts)) == len(edges)
    # acyclic: walking forward from any head must terminate
    nxt = dict(edges)
    acyclic = True
    if injective and edges:
        seen = set()
        heads = [a for a in set(srcs) if a not in set(dsts)]
        for h in heads:
            cur, local = h, set()
            while cur in nxt:
                if cur in local:
                    acyclic = False
                    break
                local.add(cur)
                cur = nxt[cur]
            seen |= local
        if not heads:
            acyclic = False
    return edges, bool(edges) and injective and acyclic


PRIMITIVE_LABELS = ("or", "and", "nand", "nor", "xor", "xnor", "buffer",
                    "inverter", "mux")


def classify_kind(members, cones, prof, labels, q_of, in_scc=False):
    if all(cones[m]["num_gates"] == 0 for m in members):
        return "buffer", ["every member has no gates"]
    if all(cones[m]["num_gates"] <= 1 for m in members) and all(
            (labels.get(m) or "").rstrip("0123456789").startswith(
                PRIMITIVE_LABELS) for m in members):
        return "gate", ["single primitive gate per member: "
                        + ", ".join(sorted({labels[m] for m in members}))]
    if all(prof[m]["drives_output"] for m in members):
        return "decoder", ["every member drives a primary output"]

    fams = {label_family(labels.get(m)) for m in members}
    fams.discard(None)

    edges, is_path = chain_of(members, cones, q_of)
    if is_path and len(edges) >= max(2, len(members) - 1):
        return "shifter", [f"members form a path of {len(edges)} links"]

    if fams == {"set_dominant_sticky"}:
        return "matcher", ["sticky set on an AND of literals"]
    if fams == {"clear_dominant"}:
        return "clearable", ["dominant clear on a held value"]
    if fams == {"toggle"}:
        return "toggle", ["members toggle on a condition"]
    if fams == {"load_enable"}:
        why = ["hold path present, data arrives from outside the block"]
        if edges:
            why.append(f"{len(edges)} intra-block links, not a clean path")
        return "register", why
    if in_scc:
        return "feedback", [
            f"{len(members)} cones in one strongly-connected component; "
            f"mutually recursive state, not a chain"]
    if len(fams) > 1:
        return "mixed", [f"members span {sorted(fams)}"]
    return "unknown", ["no rule fired"]


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def build_blocks(logic, profile, signals_doc, cone_graph):
    profiles = profile["profiles"]
    prof = {p["id"]: p for p in profiles}
    cones = {c["id"]: c for c in logic["cones"]}
    q_of = {f["d_cone"]: f["q_net"] for f in logic["flops"] if f.get("d_cone")}
    group_of = cone_graph.get("cone_to_group", {})
    node_of = {n["id"]: n for n in cone_graph["nodes"]}
    labels = {cid: (node_of.get(group_of.get(cid), {}) or {}).get("label")
              for cid in cones}

    control_floor = signals_doc["stats"]["control_floor"]
    domains = classify_domains(profiles, signals_doc["signals"], logic,
                               control_floor)

    by_bank = defaultdict(list)
    for p in profiles:
        by_bank[p["class_block"]].append(p["id"])

    sig = {s["net"]: s for s in signals_doc["signals"]}
    blocks = []
    for bank, members in sorted(by_bank.items(), key=lambda kv: -len(kv[1])):
        members = sorted(members)
        doms = Counter(domains[m][0] for m in members)
        domain = doms.most_common(1)[0][0]
        kind, why = classify_kind(members, cones, prof, labels, q_of,
                                  in_scc=bank.startswith("scc"))

        leafsets = {m: {s["net"] for s in cones[m]["sources"]
                        if s["kind"] != "const"} for m in members}
        common = set.intersection(*leafsets.values()) if leafsets else set()

        blocks.append({
            "id": f"B{len(blocks):02d}",
            "bank": bank,
            "domain": domain,
            "domain_split": dict(doms),
            "kind": kind,
            "kind_evidence": why,
            "width": len(members),
            "members": members,
            "shared_control": sorted(
                sig[n]["name"] for n in common if n in sig),
            "private_per_member": {m: len(leafsets[m] - common)
                                   for m in members},
            "label_families": sorted({label_family(labels.get(m))
                                      for m in members} - {None}),
        })
    return blocks, domains


def write_md(path, blocks, domains, control_floor):
    L = ["# Blocks", "",
         "Two domains, then a kind per block. Domain is a scored decision "
         "(see coneBlocks.py); the evidence for every cone is in "
         "CONE_BLOCKS.json. The fanout cut is coneSignals' derived control "
         f"floor ({control_floor}), not a literal.", ""]

    dom = Counter(d for d, _, _, _ in domains.values())
    L += ["## Domains", ""]
    for k, v in dom.most_common():
        L.append(f"- **{k}**: {v} cones")
    L += ["", "## Blocks", "",
          "| id | domain | kind | width | shared control | private/member | members |",
          "|---|---|---|---|---|---|---|"]
    for b in blocks:
        pv = set(b["private_per_member"].values())
        pvs = str(min(pv)) + ("" if len(pv) == 1 else f"–{max(pv)}")
        L.append("| {id} | {domain} | {kind} | {width} | {sc} | {pv} | {mem} |".format(
            sc=", ".join(b["shared_control"][:6]) or "-", pv=pvs,
            mem=", ".join(b["members"][:5]) + (" …" if b["width"] > 5 else ""),
            **b))

    L += ["", "## Kinds", ""]
    for k, v in Counter(b["kind"] for b in blocks).most_common():
        w = sum(b["width"] for b in blocks if b["kind"] == k)
        L.append(f"- **{k}**: {v} block(s), {w} cones")

    L += ["", "## Detail", ""]
    for b in blocks:
        if b["width"] < 2:
            continue
        L += [f"### {b['id']} — {b['kind']} ({b['domain']}, width {b['width']})",
              "",
              f"- bank: `{b['bank']}`",
              f"- why this kind: {'; '.join(b['kind_evidence'])}",
              f"- shared control: {', '.join(b['shared_control']) or '(none)'}",
              f"- label families: {', '.join(b['label_families']) or '-'}",
              f"- members: {', '.join(b['members'])}", ""]
    open(path, "w").write("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("logic_graph", nargs="?",
                    default=os.path.join(HERE, "LOGIC_GRAPH.json"))
    ap.add_argument("--profile", default=os.path.join(HERE, "CONE_PROFILE.json"))
    ap.add_argument("--signals", default=os.path.join(HERE, "CONE_SIGNALS.json"))
    ap.add_argument("--cone-graph", default=os.path.join(HERE, "CONE_GRAPH.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "CONE_BLOCKS.json"))
    ap.add_argument("--md", default=os.path.join(HERE, "CONE_BLOCKS.md"))
    args = ap.parse_args()

    logic = json.load(open(args.logic_graph))
    profile = json.load(open(args.profile))
    signals_doc = json.load(open(args.signals))
    cone_graph = json.load(open(args.cone_graph))

    blocks, domains = build_blocks(logic, profile, signals_doc, cone_graph)
    floor = signals_doc["stats"]["control_floor"]

    stats = {
        "blocks": len(blocks),
        "domains": dict(Counter(d for d, _, _, _ in domains.values())),
        "domain_confidence": dict(Counter(
            f"{d}/{c}" for d, _, _, c in domains.values())),
        "kinds": dict(Counter(b["kind"] for b in blocks)),
        "cones_by_kind": {k: sum(b["width"] for b in blocks if b["kind"] == k)
                          for k in {b["kind"] for b in blocks}},
        "control_floor": floor,
    }
    json.dump({"stats": stats, "blocks": blocks,
               "domains": {k: {"domain": v[0], "score": v[1],
                               "evidence": v[2], "confidence": v[3]}
                           for k, v in domains.items()}},
              open(args.out, "w"), indent=1)
    write_md(args.md, blocks, domains, floor)

    print(f"{len(blocks)} blocks")
    for k, v in sorted(stats["domains"].items()):
        print(f"  domain {k:<10} {v:>4} cones")
    for k, v in sorted(stats["kinds"].items(), key=lambda kv: -kv[1]):
        print(f"  kind   {k:<10} {v:>4} blocks, "
              f"{stats['cones_by_kind'][k]} cones")
    print(f"Wrote {args.out}")
    print(f"Wrote {args.md}")


if __name__ == "__main__":
    main()
