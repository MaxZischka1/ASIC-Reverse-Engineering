#!/usr/bin/env python3
"""genCellModels.py — generate Verilator-friendly behavioral models for every macro
in the standard-cell LEF.

The official sky130 functional models use sequential UDP primitives that Verilator
rejects; this emits plain behavioral Verilog instead: one module per LEF macro, port
list taken from the LEF signal pins (power pins omitted — the netlist doesn't wire
them), function from the documented sky130_fd_sc_hd cell behavior in FAMILY_FUNCS /
SEQ_TEMPLATES below. Timing is not modeled (zero-delay); that is fine for functional
simulation of the recovered netlist.

Every expression is validated against the LEF pin list at generation time: an
identifier that is not a pin of the macro, or a family with no entry, is an error
(or a warning for --lenient), so the models can never silently drift from the
library. Run:

    python3 genCellModels.py                 # writes lib/sky130_cells_sim.v
    python3 genCellModels.py --lef X --out Y
"""

import argparse
import re
import sys

from lefLib import parse_lef, signal_pins

# --------------------------------------------------------------- combinational
# family -> {output_pin: verilog expression over input pins}
FAMILY_FUNCS = {}

for fam in ["buf", "bufbuf", "clkbuf", "clkdlybuf4s15", "clkdlybuf4s18",
            "clkdlybuf4s25", "clkdlybuf4s50", "dlygate4sd1", "dlygate4sd2",
            "dlygate4sd3", "dlymetal6s2s", "dlymetal6s4s", "dlymetal6s6s",
            "probe_p", "probec_p", "lpflow_clkbufkapwr",
            "lpflow_lsbuf_lh_hl_isowell_tap", "lpflow_lsbuf_lh_isowell",
            "lpflow_lsbuf_lh_isowell_tap"]:
    FAMILY_FUNCS[fam] = {"X": "A"}
for fam in ["inv", "clkinv", "clkinvlp", "bufinv", "lpflow_clkinvkapwr"]:
    FAMILY_FUNCS[fam] = {"Y": "~A"}

FAMILY_FUNCS.update({
    "and2":   {"X": "A & B"},
    "and2b":  {"X": "~A_N & B"},
    "and3":   {"X": "A & B & C"},
    "and3b":  {"X": "~A_N & B & C"},
    "and4":   {"X": "A & B & C & D"},
    "and4b":  {"X": "~A_N & B & C & D"},
    "and4bb": {"X": "~A_N & ~B_N & C & D"},
    "nand2":  {"Y": "~(A & B)"},
    "nand2b": {"Y": "~(~A_N & B)"},
    "nand3":  {"Y": "~(A & B & C)"},
    "nand3b": {"Y": "~(~A_N & B & C)"},
    "nand4":  {"Y": "~(A & B & C & D)"},
    "nand4b": {"Y": "~(~A_N & B & C & D)"},
    "nand4bb": {"Y": "~(~A_N & ~B_N & C & D)"},
    "or2":    {"X": "A | B"},
    "or2b":   {"X": "A | ~B_N"},
    "or3":    {"X": "A | B | C"},
    "or3b":   {"X": "A | B | ~C_N"},
    "or4":    {"X": "A | B | C | D"},
    "or4b":   {"X": "A | B | C | ~D_N"},
    "or4bb":  {"X": "A | B | ~C_N | ~D_N"},
    "nor2":   {"Y": "~(A | B)"},
    "nor2b":  {"Y": "~(A | ~B_N)"},
    "nor3":   {"Y": "~(A | B | C)"},
    "nor3b":  {"Y": "~(A | B | ~C_N)"},
    "nor4":   {"Y": "~(A | B | C | D)"},
    "nor4b":  {"Y": "~(A | B | C | ~D_N)"},
    "nor4bb": {"Y": "~(A | B | ~C_N | ~D_N)"},
    "xor2":   {"X": "A ^ B"},
    "xor3":   {"X": "A ^ B ^ C"},
    "xnor2":  {"Y": "~(A ^ B)"},
    "xnor3":  {"X": "~(A ^ B ^ C)"},
    # AOI/OAI: aXY = AND-into-OR, oXY = OR-into-AND; trailing i inverts; b = one
    # inverted input; 2bb2 = first pair inverted.
    "a21o":    {"X": "(A1 & A2) | B1"},
    "a21oi":   {"Y": "~((A1 & A2) | B1)"},
    "a21bo":   {"X": "(A1 & A2) | ~B1_N"},
    "a21boi":  {"Y": "~((A1 & A2) | ~B1_N)"},
    "a22o":    {"X": "(A1 & A2) | (B1 & B2)"},
    "a22oi":   {"Y": "~((A1 & A2) | (B1 & B2))"},
    "a2bb2o":  {"X": "(~A1_N & ~A2_N) | (B1 & B2)"},
    "a2bb2oi": {"Y": "~((~A1_N & ~A2_N) | (B1 & B2))"},
    "a31o":    {"X": "(A1 & A2 & A3) | B1"},
    "a31oi":   {"Y": "~((A1 & A2 & A3) | B1)"},
    "a32o":    {"X": "(A1 & A2 & A3) | (B1 & B2)"},
    "a32oi":   {"Y": "~((A1 & A2 & A3) | (B1 & B2))"},
    "a41o":    {"X": "(A1 & A2 & A3 & A4) | B1"},
    "a41oi":   {"Y": "~((A1 & A2 & A3 & A4) | B1)"},
    "a211o":   {"X": "(A1 & A2) | B1 | C1"},
    "a211oi":  {"Y": "~((A1 & A2) | B1 | C1)"},
    "a221o":   {"X": "(A1 & A2) | (B1 & B2) | C1"},
    "a221oi":  {"Y": "~((A1 & A2) | (B1 & B2) | C1)"},
    "a222oi":  {"Y": "~((A1 & A2) | (B1 & B2) | (C1 & C2))"},
    "a311o":   {"X": "(A1 & A2 & A3) | B1 | C1"},
    "a311oi":  {"Y": "~((A1 & A2 & A3) | B1 | C1)"},
    "a2111o":  {"X": "(A1 & A2) | B1 | C1 | D1"},
    "a2111oi": {"Y": "~((A1 & A2) | B1 | C1 | D1)"},
    "o21a":    {"X": "(A1 | A2) & B1"},
    "o21ai":   {"Y": "~((A1 | A2) & B1)"},
    "o21ba":   {"X": "(A1 | A2) & ~B1_N"},
    "o21bai":  {"Y": "~((A1 | A2) & ~B1_N)"},
    "o22a":    {"X": "(A1 | A2) & (B1 | B2)"},
    "o22ai":   {"Y": "~((A1 | A2) & (B1 | B2))"},
    "o2bb2a":  {"X": "(~A1_N | ~A2_N) & (B1 | B2)"},
    "o2bb2ai": {"Y": "~((~A1_N | ~A2_N) & (B1 | B2))"},
    "o31a":    {"X": "(A1 | A2 | A3) & B1"},
    "o31ai":   {"Y": "~((A1 | A2 | A3) & B1)"},
    "o32a":    {"X": "(A1 | A2 | A3) & (B1 | B2)"},
    "o32ai":   {"Y": "~((A1 | A2 | A3) & (B1 | B2))"},
    "o41a":    {"X": "(A1 | A2 | A3 | A4) & B1"},
    "o41ai":   {"Y": "~((A1 | A2 | A3 | A4) & B1)"},
    "o211a":   {"X": "(A1 | A2) & B1 & C1"},
    "o211ai":  {"Y": "~((A1 | A2) & B1 & C1)"},
    "o221a":   {"X": "(A1 | A2) & (B1 | B2) & C1"},
    "o221ai":  {"Y": "~((A1 | A2) & (B1 | B2) & C1)"},
    "o311a":   {"X": "(A1 | A2 | A3) & B1 & C1"},
    "o311ai":  {"Y": "~((A1 | A2 | A3) & B1 & C1)"},
    "o2111a":  {"X": "(A1 | A2) & B1 & C1 & D1"},
    "o2111ai": {"Y": "~((A1 | A2) & B1 & C1 & D1)"},
    "mux2":    {"X": "S ? A1 : A0"},
    "mux2i":   {"Y": "~(S ? A1 : A0)"},
    "mux4":    {"X": "S1 ? (S0 ? A3 : A2) : (S0 ? A1 : A0)"},
    "maj3":    {"X": "(A & B) | (B & C) | (A & C)"},
    "ha":      {"SUM": "A ^ B", "COUT": "A & B"},
    "fa":      {"SUM": "A ^ B ^ CIN", "COUT": "(A & B) | (CIN & (A ^ B))"},
    "fah":     {"SUM": "A ^ B ^ CI", "COUT": "(A & B) | (CI & (A ^ B))"},
    "fahcin":  {"SUM": "A ^ B ^ CIN", "COUT": "(A & B) | (CIN & (A ^ B))"},
    "fahcon":  {"SUM": "A ^ B ^ CI", "COUT_N": "~((A & B) | (CI & (A ^ B)))"},
    "conb":    {"HI": "1'b1", "LO": "1'b0"},
    "macro_sparecell": {"LO": "1'b0"},
    "einvp":   {"Z": "TE ? ~A : 1'bz"},
    "einvn":   {"Z": "TE_B ? 1'bz : ~A"},
    "ebufn":   {"Z": "TE_B ? 1'bz : A"},
    "lpflow_inputiso0p": {"X": "A & ~SLEEP"},
    "lpflow_inputiso0n": {"X": "A & SLEEP_B"},
    "lpflow_inputiso1p": {"X": "A | SLEEP"},
    "lpflow_inputiso1n": {"X": "A | ~SLEEP_B"},
    "lpflow_isobufsrc":  {"X": "A & ~SLEEP"},
    "lpflow_isobufsrckapwr": {"X": "A & ~SLEEP"},
    # no-output cells: empty modules
    "diode": {}, "ef:fakediode": {}, "ef:decap": {}, "lpflow_bleeder": {},
})

# ----------------------------------------------------------------- sequential
# family -> body template; {out}/{in} pin names are literal (validated below).
# Flops with both SET_B and RESET_B model the real cell's both-asserted state
# (Q and Q_N both high): SET_B wins on Q, RESET_B wins on Q_N.
SEQ_TEMPLATES = {
    "dfxtp": """
    reg q_r;
    always @(posedge CLK) q_r <= D;
    assign Q = q_r;""",
    "dfxbp": """
    reg q_r;
    always @(posedge CLK) q_r <= D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "dfrtp": """
    reg q_r;
    always @(posedge CLK or negedge RESET_B)
        if (!RESET_B) q_r <= 1'b0; else q_r <= D;
    assign Q = q_r;""",
    "dfrbp": """
    reg q_r;
    always @(posedge CLK or negedge RESET_B)
        if (!RESET_B) q_r <= 1'b0; else q_r <= D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "dfrtn": """
    reg q_r;
    always @(negedge CLK_N or negedge RESET_B)
        if (!RESET_B) q_r <= 1'b0; else q_r <= D;
    assign Q = q_r;""",
    "dfstp": """
    reg q_r;
    always @(posedge CLK or negedge SET_B)
        if (!SET_B) q_r <= 1'b1; else q_r <= D;
    assign Q = q_r;""",
    "dfsbp": """
    reg q_r;
    always @(posedge CLK or negedge SET_B)
        if (!SET_B) q_r <= 1'b1; else q_r <= D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "dfbbp": """
    reg q_r, qn_r;
    always @(posedge CLK or negedge SET_B or negedge RESET_B) begin
        if (!SET_B) q_r <= 1'b1;
        else if (!RESET_B) q_r <= 1'b0;
        else q_r <= D;
        if (!RESET_B) qn_r <= 1'b1;
        else if (!SET_B) qn_r <= 1'b0;
        else qn_r <= ~D;
    end
    assign Q = q_r;
    assign Q_N = qn_r;""",
    "dfbbn": """
    reg q_r, qn_r;
    always @(negedge CLK_N or negedge SET_B or negedge RESET_B) begin
        if (!SET_B) q_r <= 1'b1;
        else if (!RESET_B) q_r <= 1'b0;
        else q_r <= D;
        if (!RESET_B) qn_r <= 1'b1;
        else if (!SET_B) qn_r <= 1'b0;
        else qn_r <= ~D;
    end
    assign Q = q_r;
    assign Q_N = qn_r;""",
    "edfxtp": """
    reg q_r;
    always @(posedge CLK) if (DE) q_r <= D;
    assign Q = q_r;""",
    "edfxbp": """
    reg q_r;
    always @(posedge CLK) if (DE) q_r <= D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "sdfxtp": """
    reg q_r;
    always @(posedge CLK) q_r <= SCE ? SCD : D;
    assign Q = q_r;""",
    "sdfxbp": """
    reg q_r;
    always @(posedge CLK) q_r <= SCE ? SCD : D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "sdfrtp": """
    reg q_r;
    always @(posedge CLK or negedge RESET_B)
        if (!RESET_B) q_r <= 1'b0; else q_r <= SCE ? SCD : D;
    assign Q = q_r;""",
    "sdfrbp": """
    reg q_r;
    always @(posedge CLK or negedge RESET_B)
        if (!RESET_B) q_r <= 1'b0; else q_r <= SCE ? SCD : D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "sdfrtn": """
    reg q_r;
    always @(negedge CLK_N or negedge RESET_B)
        if (!RESET_B) q_r <= 1'b0; else q_r <= SCE ? SCD : D;
    assign Q = q_r;""",
    "sdfstp": """
    reg q_r;
    always @(posedge CLK or negedge SET_B)
        if (!SET_B) q_r <= 1'b1; else q_r <= SCE ? SCD : D;
    assign Q = q_r;""",
    "sdfsbp": """
    reg q_r;
    always @(posedge CLK or negedge SET_B)
        if (!SET_B) q_r <= 1'b1; else q_r <= SCE ? SCD : D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "sdfbbp": """
    reg q_r, qn_r;
    always @(posedge CLK or negedge SET_B or negedge RESET_B) begin
        if (!SET_B) q_r <= 1'b1;
        else if (!RESET_B) q_r <= 1'b0;
        else q_r <= SCE ? SCD : D;
        if (!RESET_B) qn_r <= 1'b1;
        else if (!SET_B) qn_r <= 1'b0;
        else qn_r <= ~(SCE ? SCD : D);
    end
    assign Q = q_r;
    assign Q_N = qn_r;""",
    "sdfbbn": """
    reg q_r, qn_r;
    always @(negedge CLK_N or negedge SET_B or negedge RESET_B) begin
        if (!SET_B) q_r <= 1'b1;
        else if (!RESET_B) q_r <= 1'b0;
        else q_r <= SCE ? SCD : D;
        if (!RESET_B) qn_r <= 1'b1;
        else if (!SET_B) qn_r <= 1'b0;
        else qn_r <= ~(SCE ? SCD : D);
    end
    assign Q = q_r;
    assign Q_N = qn_r;""",
    "sedfxtp": """
    reg q_r;
    always @(posedge CLK) if (SCE | DE) q_r <= SCE ? SCD : D;
    assign Q = q_r;""",
    "sedfxbp": """
    reg q_r;
    always @(posedge CLK) if (SCE | DE) q_r <= SCE ? SCD : D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "dlxtp": """
    reg q_r;
    always @* if (GATE) q_r = D;
    assign Q = q_r;""",
    "dlxbp": """
    reg q_r;
    always @* if (GATE) q_r = D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "dlxtn": """
    reg q_r;
    always @* if (!GATE_N) q_r = D;
    assign Q = q_r;""",
    "dlxbn": """
    reg q_r;
    always @* if (!GATE_N) q_r = D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "dlrtp": """
    reg q_r;
    always @* if (!RESET_B) q_r = 1'b0; else if (GATE) q_r = D;
    assign Q = q_r;""",
    "dlrbp": """
    reg q_r;
    always @* if (!RESET_B) q_r = 1'b0; else if (GATE) q_r = D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "dlrtn": """
    reg q_r;
    always @* if (!RESET_B) q_r = 1'b0; else if (!GATE_N) q_r = D;
    assign Q = q_r;""",
    "dlrbn": """
    reg q_r;
    always @* if (!RESET_B) q_r = 1'b0; else if (!GATE_N) q_r = D;
    assign Q = q_r;
    assign Q_N = ~q_r;""",
    "lpflow_inputisolatch": """
    reg q_r;
    always @* if (SLEEP_B) q_r = D;
    assign Q = q_r;""",
    "dlclkp": """
    reg en_r;
    always @* if (!CLK) en_r = GATE;
    assign GCLK = CLK & en_r;""",
    "sdlclkp": """
    reg en_r;
    always @* if (!CLK) en_r = GATE | SCE;
    assign GCLK = CLK & en_r;""",
}

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
VERILOG_KEYWORDS = {"reg", "assign", "always", "posedge", "negedge", "or", "if",
                    "else", "begin", "end"}


def family_of(name):
    base = name.replace("sky130_fd_sc_hd__", "").replace("sky130_ef_sc_hd__", "ef:")
    return re.sub(r"_(\d+|lp|lp2)$", "", base)


def used_idents(text):
    text = re.sub(r"\d+'b[01xz]+", " ", text)     # drop sized literals like 1'b0
    return {t for t in IDENT_RE.findall(text)
            if t not in VERILOG_KEYWORDS and not t.endswith("_r")}


def emit_module(name, macro, problems):
    pins = signal_pins(macro)
    ins = sorted(p.name for p in pins.values() if p.direction != "OUTPUT")
    outs = sorted(p.name for p in pins.values() if p.direction == "OUTPUT")
    fam = family_of(name)

    if not pins:            # fill/decap/tap: no signal pins, empty model
        return "module %s ();\nendmodule" % name

    lines = ["module %s (" % name]
    ports = ["    output %s" % o for o in outs] + ["    input  %s" % i for i in ins]
    lines.append(",\n".join(ports))
    lines.append(");")

    if fam in SEQ_TEMPLATES:
        body = SEQ_TEMPLATES[fam]
        bad = used_idents(body) - set(ins) - set(outs)
        if bad:
            problems.append("%s: template uses unknown pins %r" % (name, sorted(bad)))
        undriven = [o for o in outs if o not in used_idents(body)]
        if undriven:
            problems.append("%s: outputs %r not driven" % (name, undriven))
        lines.append(body.rstrip())
    elif fam in FAMILY_FUNCS:
        funcs = FAMILY_FUNCS[fam]
        for o in outs:
            if o not in funcs:
                problems.append("%s: no function for output %s" % (name, o))
                continue
            bad = used_idents(funcs[o]) - set(ins)
            if bad:
                problems.append("%s: expression for %s uses unknown pins %r"
                                % (name, o, sorted(bad)))
            lines.append("    assign %s = %s;" % (o, funcs[o]))
    else:
        problems.append("%s: family %r has no model" % (name, fam))
        for o in outs:
            lines.append("    assign %s = 1'b0;  // MISSING MODEL" % o)

    lines.append("endmodule")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lef", default="lib/sky130_fd_sc_hd_merged.lef")
    ap.add_argument("--out", default="lib/sky130_cells_sim.v")
    ap.add_argument("--lenient", action="store_true",
                    help="warn instead of failing on unmodeled families")
    args = ap.parse_args(argv)

    macros = parse_lef(args.lef)
    problems = []
    modules = []
    for name in sorted(macros):
        modules.append(emit_module(name, macros[name], problems))

    header = """\
// Generated by genCellModels.py from %s — do not edit by hand.
// Behavioral, zero-delay models for functional simulation with Verilator.
// Power pins are omitted (the extracted netlist does not wire them).
/* verilator lint_off LATCH */
/* verilator lint_off UNUSEDSIGNAL */
""" % args.lef
    with open(args.out, "w") as f:
        f.write(header + "\n" + "\n\n".join(modules) + "\n")

    print("%s: %d modules -> %s" % (args.lef, len(modules), args.out))
    if problems:
        for p in problems:
            print("PROBLEM:", p)
        if not args.lenient:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
