#!/usr/bin/env python3
"""vcdToStimulus.py — a recorded VCD waveform -> stimulus inlined into the TBs.

You run this by hand on the recording; the testbenches then replay its inputs
and check the DUT outputs against its recorded outputs.

    python3 bench/vcdToStimulus.py example_inputs.vcd --clock clk \\
        --inline bench/tb_cones.cpp --inline bench/tb_netlist.cpp \\
        --json bench/stimulus.json

Each --inline testbench is rewritten in place, with the block between the

    // ---- BEGIN GENERATED STIMULUS ... // ---- END GENERATED STIMULUS

markers replaced by the recording's data, so tb_cones.cpp / tb_netlist.cpp carry
their own stimulus and compile as they stand -- no separate generated copy.

Every variable is sampled once per rising edge of --clock, at its value just
before the edge (drive / eval / check / tick). Bit-blasted buses (`O[0]`..`O[7]`)
are reassembled into one signal `O`. Widths up to 64 bits are kept whole; wider
signals keep only their low 64 bits (the full bit string is in the JSON).

Every signal becomes one binary string covering the whole run: STIM_W_<sig> chars
per cycle, MSB first, back to back, so a 1-bit signal reads "0100101101001..."
and an 8-bit one "0101010001001000...". A cycle the recording left at x/z is
written as all-'x' and reports itself unknown.

Emitted C++ (matches bench/tb_*.cpp):
    STIM_CYCLES, STIM_NSIGNALS, STIM_NAMES, STIM_WIDTHS  (comma-joined strings)
    STIM_<sig>        the whole run as one binary string, STIM_W_<sig> bits per
                      cycle, MSB first, 'x' where the recording had x/z
    STIM_<sig>_TEXT   8-bit signals only: the same bytes as ASCII, one char per
                      cycle (octal-escaped where not printable, '?' on x/z), so a
                      recorded output that spells words is readable in the source
    STIM(sig, cyc)          that cycle's value, decoded to uint64_t
    STIM_KNOWN(sig, cyc)    1 if every bit of that cycle is 0/1, 0 if any is x
    STIM_HAS_<sig>, STIM_IX_<sig>, STIM_W_<sig>
"""

import argparse
import json
import re
import sys
from collections import OrderedDict

BIT_RE = re.compile(r"^(.*?)\[(\d+)\]$")
RANGE_RE = re.compile(r"^\[(\d+):(\d+)\]$")


def leaf(name):
    return name.rsplit(".", 1)[-1]


def parse_header(tokens):
    """-> (id_name: code->name, id_width: code->int). Consumes through
    $enddefinitions."""
    id_name, id_width = {}, {}
    scope = []
    for t in tokens:
        if t == "$scope":
            next(tokens)                       # scope type
            scope.append(next(tokens))
            for x in tokens:
                if x == "$end":
                    break
        elif t == "$upscope":
            for x in tokens:
                if x == "$end":
                    break
            if scope:
                scope.pop()
        elif t == "$var":
            next(tokens)                       # var type (wire/reg/...)
            width = int(next(tokens))
            code = next(tokens)
            name = next(tokens)
            rest = []
            for x in tokens:
                if x == "$end":
                    break
                rest.append(x)
            full = name
            if rest and RANGE_RE.match(rest[0]):
                pass                            # whole-bus var; keep bare name
            elif rest and re.match(r"^\[\d+\]$", rest[0]):
                full = name + rest[0]           # single bit of a bus
            id_name.setdefault(code, ".".join(scope + [full]) if scope else full)
            id_width[code] = width
        elif t in ("$timescale", "$date", "$version", "$comment"):
            for x in tokens:
                if x == "$end":
                    break
        elif t == "$enddefinitions":
            for x in tokens:
                if x == "$end":
                    break
            return id_name, id_width
    raise SystemExit("vcdToStimulus: no $enddefinitions in header")


def sample_rows(tokens, id_name, id_width, clock_leaf, sample):
    """Walk the value section and snapshot every signal once per rising clock
    edge. Changes are batched per timestamp and applied atomically, so the
    snapshot is the state strictly before (default) or strictly after that
    timestamp's changes -- never a mix from mid-timestamp token order, which VCD
    does not define. A rising edge is 0 -> 1 only (an initial x -> 1 is not one).
    """
    cur = {c: ("x" * id_width[c] if id_width[c] > 1 else "x") for c in id_name}
    clk_codes = [c for c, n in id_name.items() if leaf(n) == clock_leaf]
    if not clk_codes:
        raise SystemExit("vcdToStimulus: clock %r not among signals: %s"
                         % (clock_leaf, sorted({leaf(n) for n in id_name.values()})))
    clk = clk_codes[0]
    rows = []
    batch = []                                  # (code, value) for the open timestamp

    def flush():
        new_clk = cur[clk]
        for code, val in batch:
            if code == clk:
                new_clk = val
        rising = (cur[clk] == "0" and new_clk == "1")
        if rising and sample == "before":
            rows.append(dict(cur))
        for code, val in batch:
            if code in cur:
                cur[code] = val
        if rising and sample == "after":
            rows.append(dict(cur))
        batch.clear()

    for t in tokens:
        if not t or t in ("$dumpvars", "$dumpall", "$dumpon", "$dumpoff", "$end"):
            continue
        if t[0] == "#":                          # timestamp boundary
            flush()
            continue
        if t[0] in "bB":
            bits = t[1:]
            batch.append((next(tokens), bits))
            continue
        if t[0] in "rR":
            next(tokens)                        # real value: ignored
            continue
        batch.append((t[1:], t[0]))             # scalar: "<value><code>"
    flush()
    return rows


def to_int_known(bitstr):
    """('101' | '0') -> (int, known-bool). x/z anywhere -> known False."""
    s = bitstr.lower().lstrip("b")
    if not s:
        return 0, False
    if any(ch in "xz" for ch in s):
        return 0, False
    return int(s, 2) & ((1 << 64) - 1), True


def collapse(rows, id_name):
    """Reassemble O[0..7] -> O. Returns (signal_names, width, values, known)
    where values[cycle] is {sig: int}, known[cycle] is {sig: bool}."""
    # map every declared name to a (base, bit-or-None)
    members = OrderedDict()                     # base -> set of bit indices (or {None})
    for code, name in id_name.items():
        ln = leaf(name)
        m = BIT_RE.match(ln)
        if m:
            members.setdefault(m.group(1), set()).add(int(m.group(2)))
        else:
            members.setdefault(ln, set()).add(None)
    code_of_leaf = {leaf(n): c for c, n in id_name.items()}

    sigs, widths = [], {}
    for base, bits in members.items():
        sigs.append(base)
        if bits == {None}:
            widths[base] = None                 # take width from the var itself
        else:
            widths[base] = max(bits) + 1

    vals, knowns = [], []
    for row in rows:
        v, k = {}, {}
        for base in sigs:
            bits = members[base]
            if bits == {None}:
                code = code_of_leaf[base]
                iv, ik = to_int_known(row[code])
            else:
                iv, ik = 0, True
                for b in bits:
                    code = code_of_leaf["%s[%d]" % (base, b)]
                    bv, bk = to_int_known(row[code])
                    if not bk:
                        ik = False
                    elif bv:
                        iv |= (1 << b)
            v[base] = iv
            k[base] = ik
        vals.append(v)
        knowns.append(k)
    return sigs, widths, vals, knowns




BEGIN_MARK = "// ---- BEGIN GENERATED STIMULUS"
END_MARK = "// ---- END GENERATED STIMULUS"
IDENT_BAD = re.compile(r"[^A-Za-z0-9_]")


def idents(sigs):
    """Signal names -> unique C identifiers (STIM_<ident>). Order follows sigs."""
    out, seen = [], set()
    for s in sigs:
        base = IDENT_BAD.sub("_", s)
        if not base or base[0].isdigit():
            base = "_" + base
        name, n = base, 2
        while name in seen:
            name, n = "%s_%d" % (base, n), n + 1
        seen.add(name)
        out.append(name)
    return out


def widths_of(sigs, widths, id_width, id_name):
    code_of_leaf = {leaf(nm): c for c, nm in id_name.items()}
    return [widths[s] if widths[s] is not None else id_width[code_of_leaf[s]]
            for s in sigs]


LINEW = 72                                      # bit chars per source line


def bit_string(col, known, w, indent="  "):
    """Per-cycle values -> C string literal lines: `w` bits per cycle, MSB first,
    all-'x' for a cycle the recording left unknown."""
    s = "".join(("x" * w) if not k else
                "".join("1" if (v >> b) & 1 else "0" for b in range(w - 1, -1, -1))
                for v, k in zip(col, known))
    per = max(w, (LINEW // w) * w)               # never split a cycle across lines
    return [indent + '"%s"' % s[i:i + per] for i in range(0, len(s), per)] or \
           [indent + '""']


def text_literal(col, known):
    """An 8-bit signal's per-cycle bytes -> one C string literal. Octal escapes
    (never ambiguous, unlike \\x) for anything not plainly printable; '?' where
    the recording had x/z."""
    out = []
    for v, k in zip(col, known):
        if not k:
            out.append("?")
        elif v == 0x22 or v == 0x5c:            # " and backslash
            out.append("\\" + chr(v))
        elif 0x20 <= v < 0x7f and v != 0x3f:    # printable, ? kept out (trigraphs)
            out.append(chr(v))
        else:
            out.append("\\%03o" % v)
    return '"%s"' % "".join(out)


def stimulus_block(sigs, widths, vals, knowns, id_width, id_name, meta):
    """The generated C++ block: one binary string per signal (`STIM_W_<sig>` bits
    per cycle, MSB first, 'x' where the recording had x/z), plus an ASCII
    rendering of every 8-bit signal."""
    n, cyc = len(sigs), len(vals)
    ids = idents(sigs)
    wtab = widths_of(sigs, widths, id_width, id_name)
    bits = [min(w, 64) for w in wtab]           # values are masked to 64 bits

    L = [BEGIN_MARK + " (bench/vcdToStimulus.py) -- do not edit ----",
         "// source: %s   clock: %s   sample: %s" % meta,
         "//",
         "// One binary string per signal, the whole run back to back: STIM_W_<sig>",
         "// chars per cycle, MSB first, 'x' for a cycle the recording left at x/z.",
         "#define STIM_CYCLES %d" % cyc,
         "#define STIM_NSIGNALS %d" % n,
         'static const char* const STIM_NAMES  = "%s";' % ",".join(sigs),
         'static const char* const STIM_WIDTHS = "%s";' % ",".join(map(str, wtab)),
         ""]

    for i, (s_, ident, w, bw) in enumerate(zip(sigs, ids, wtab, bits)):
        col = [vals[c][s_] for c in range(cyc)]
        kol = [knowns[c][s_] for c in range(cyc)]
        L.append("// %s  [%d bit%s]  %d char%s per cycle, %d cycles%s"
                 % (s_, w, "" if w == 1 else "s", bw, "" if bw == 1 else "s", cyc,
                    "  (low 64 bits only)" if bw != w else ""))
        L.append("#define STIM_HAS_%s 1" % ident)
        L.append("#define STIM_IX_%s %d" % (ident, i))
        L.append("#define STIM_W_%s %d" % (ident, bw))
        L.append("static const char* const STIM_%s =" % ident)
        L.extend(bit_string(col, kol, bw))
        L[-1] += ";"
        if w == 8:
            L.append("// the same signal read as ASCII, one byte per cycle")
            L.append("static const char* const STIM_%s_TEXT =" % ident)
            L.append("  " + text_literal(col, kol) + ";")
        L.append("")

    L.append("// STIM(sig, cyc) -> that cycle's value; 'x' bits decode as 0, so pair")
    L.append("// it with STIM_KNOWN(sig, cyc) before comparing against a DUT.")
    L.append("static inline uint64_t stimVal(const char* s, int w, int cyc) {")
    L.append("  if (cyc < 0 || cyc >= STIM_CYCLES) return 0;")
    L.append("  uint64_t v = 0;")
    L.append("  for (int b = 0; b < w; b++)")
    L.append("    v = (v << 1) | (uint64_t)(s[(size_t)cyc * w + b] == '1');")
    L.append("  return v; }")
    L.append("")
    L.append("static inline int stimKnown(const char* s, int w, int cyc) {")
    L.append("  if (cyc < 0 || cyc >= STIM_CYCLES) return 0;")
    L.append("  for (int b = 0; b < w; b++)")
    L.append("    if (s[(size_t)cyc * w + b] != '0' && s[(size_t)cyc * w + b] != '1')")
    L.append("      return 0;")
    L.append("  return 1; }")
    L.append("")
    L.append("#define STIM(sig, cyc)       stimVal(STIM_##sig, STIM_W_##sig, (cyc))")
    L.append("#define STIM_KNOWN(sig, cyc) stimKnown(STIM_##sig, STIM_W_##sig, (cyc))")
    L.append(END_MARK + " ----")
    return "\n".join(L)


def inline_into(src, block):
    """Rewrite `src` in place with the marked block replaced by `block`."""
    with open(src) as f:
        lines = f.read().splitlines()
    begin = end = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(BEGIN_MARK):
            begin = i
        elif ln.lstrip().startswith(END_MARK):
            end = i
            break
    if begin is None or end is None or end < begin:
        raise SystemExit("vcdToStimulus: %s has no %s / %s marker pair"
                         % (src, BEGIN_MARK.strip("/ "), END_MARK.strip("/ ")))
    with open(src, "w") as f:
        f.write("\n".join(lines[:begin] + [block] + lines[end + 1:]) + "\n")
    return src


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("vcd")
    ap.add_argument("--clock", default="clk", help="clock signal leaf name")
    ap.add_argument("--edge", choices=["pos"], default="pos")
    ap.add_argument("--sample", choices=["before", "after"], default="before",
                    help="snapshot state before (default) or after each edge")
    ap.add_argument("--inline", dest="inline", action="append", default=[],
                    metavar="TB.cpp",
                    help="testbench to expand in place (repeatable)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    with open(args.vcd) as f:
        toks = iter(f.read().split())
    id_name, id_width = parse_header(toks)
    rows = sample_rows(toks, id_name, id_width, args.clock, args.sample)
    sigs, widths, vals, knowns = collapse(rows, id_name)

    print("%s: %d signals, %d clock edges"
          % (args.vcd, len(sigs), len(rows)))
    print("  signals: %s" % ", ".join(sigs))

    if not args.inline and not args.json:
        print("  (nothing written: pass --inline TB.cpp and/or --json FILE)")
    if args.inline:
        block = stimulus_block(sigs, widths, vals, knowns, id_width, id_name,
                               (args.vcd, args.clock, args.sample))
        for src in args.inline:
            print("  -> %s" % inline_into(src, block))
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"signals": sigs, "cycles": len(rows),
                       "value": [[v[s] for s in sigs] for v in vals],
                       "known": [[int(k[s]) for s in sigs] for k in knowns]},
                      f)
        print("  -> %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
