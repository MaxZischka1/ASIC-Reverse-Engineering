#!/usr/bin/env python3
"""Synthetic test for genCellModels.py output: simulate the generated models with
Verilator and compare against independently written C++ expectations.

- Combinational: 22 representative cells (every structural family shape: plain,
  b/bb inverted-input variants, AOI/OAI, mux, maj, adders, constants) wired to one
  8-bit input vector; all 256 combinations checked exhaustively. The C++ oracle
  expressions are written here by hand from the sky130 documentation — not derived
  from the generator's table — so a transcription error in either place fails.
- Sequential: a scripted scenario on dfxtp/dfrtp/dfstp/dfbbp/edfxtp/sdfxtp/dlxtp/
  dlclkp covering clocked capture, async reset/set (including both-asserted),
  enable/scan priority, latch transparency, and clock gating.

Run:  python3 tests/testCellModels.py   (needs verilator; regenerates the models)
"""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COMB_V = """
module comb_top (input wire [7:0] v, output wire [21:0] o);
  sky130_fd_sc_hd__inv_1     c0 (.A(v[0]), .Y(o[0]));
  sky130_fd_sc_hd__nand2_1   c1 (.A(v[0]), .B(v[1]), .Y(o[1]));
  sky130_fd_sc_hd__nand4bb_1 c2 (.A_N(v[0]), .B_N(v[1]), .C(v[2]), .D(v[3]), .Y(o[2]));
  sky130_fd_sc_hd__nor2b_1   c3 (.A(v[0]), .B_N(v[1]), .Y(o[3]));
  sky130_fd_sc_hd__xor3_1    c4 (.A(v[0]), .B(v[1]), .C(v[2]), .X(o[4]));
  sky130_fd_sc_hd__xnor2_1   c5 (.A(v[0]), .B(v[1]), .Y(o[5]));
  sky130_fd_sc_hd__a21oi_1   c6 (.A1(v[0]), .A2(v[1]), .B1(v[2]), .Y(o[6]));
  sky130_fd_sc_hd__a221o_1   c7 (.A1(v[0]), .A2(v[1]), .B1(v[2]), .B2(v[3]),
                                 .C1(v[4]), .X(o[7]));
  sky130_fd_sc_hd__a2bb2oi_1 c8 (.A1_N(v[0]), .A2_N(v[1]), .B1(v[2]), .B2(v[3]),
                                 .Y(o[8]));
  sky130_fd_sc_hd__o21ai_0   c9 (.A1(v[0]), .A2(v[1]), .B1(v[2]), .Y(o[9]));
  sky130_fd_sc_hd__o2bb2a_1  c10 (.A1_N(v[0]), .A2_N(v[1]), .B1(v[2]), .B2(v[3]),
                                  .X(o[10]));
  sky130_fd_sc_hd__o41a_1    c11 (.A1(v[0]), .A2(v[1]), .A3(v[2]), .A4(v[3]),
                                  .B1(v[4]), .X(o[11]));
  sky130_fd_sc_hd__mux2_1    c12 (.A0(v[0]), .A1(v[1]), .S(v[2]), .X(o[12]));
  sky130_fd_sc_hd__mux2i_1   c13 (.A0(v[0]), .A1(v[1]), .S(v[2]), .Y(o[13]));
  sky130_fd_sc_hd__mux4_2    c14 (.A0(v[0]), .A1(v[1]), .A2(v[2]), .A3(v[3]),
                                  .S0(v[4]), .S1(v[5]), .X(o[14]));
  sky130_fd_sc_hd__maj3_1    c15 (.A(v[0]), .B(v[1]), .C(v[2]), .X(o[15]));
  sky130_fd_sc_hd__ha_1      c16 (.A(v[0]), .B(v[1]), .SUM(o[16]), .COUT(o[17]));
  sky130_fd_sc_hd__fa_1      c17 (.A(v[0]), .B(v[1]), .CIN(v[2]),
                                  .SUM(o[18]), .COUT(o[19]));
  sky130_fd_sc_hd__conb_1    c18 (.HI(o[20]), .LO(o[21]));
endmodule
"""

COMB_CPP = r"""
#include <cstdio>
#include "Vcomb_top.h"
int b(int v, int i) { return (v >> i) & 1; }
int main() {
    Vcomb_top* dut = new Vcomb_top;
    int fails = 0;
    for (int v = 0; v < 256; v++) {
        dut->v = v;
        dut->eval();
        int e[22];
        int v0=b(v,0), v1=b(v,1), v2=b(v,2), v3=b(v,3), v4=b(v,4), v5=b(v,5);
        e[0]  = !v0;
        e[1]  = !(v0 && v1);
        e[2]  = !(!v0 && !v1 && v2 && v3);
        e[3]  = !(v0 || !v1);
        e[4]  = v0 ^ v1 ^ v2;
        e[5]  = !(v0 ^ v1);
        e[6]  = !((v0 && v1) || v2);
        e[7]  = (v0 && v1) || (v2 && v3) || v4;
        e[8]  = !((!v0 && !v1) || (v2 && v3));
        e[9]  = !((v0 || v1) && v2);
        e[10] = (!v0 || !v1) && (v2 || v3);
        e[11] = (v0 || v1 || v2 || v3) && v4;
        e[12] = v2 ? v1 : v0;
        e[13] = !(v2 ? v1 : v0);
        e[14] = v5 ? (v4 ? v3 : v2) : (v4 ? v1 : v0);
        e[15] = (v0 && v1) || (v1 && v2) || (v0 && v2);
        e[16] = v0 ^ v1;
        e[17] = v0 && v1;
        e[18] = v0 ^ v1 ^ v2;
        e[19] = (v0 && v1) || (v2 && (v0 ^ v1));
        e[20] = 1;
        e[21] = 0;
        for (int i = 0; i < 22; i++)
            if (((dut->o >> i) & 1) != e[i]) {
                printf("MISMATCH v=%02x output %d: got %d expected %d\n",
                       v, i, (int)((dut->o >> i) & 1), e[i]);
                fails++;
            }
    }
    dut->final();
    delete dut;
    if (!fails) printf("comb OK: 256 vectors x 22 outputs\n");
    return fails ? 1 : 0;
}
"""

SEQ_V = """
module seq_top (input wire clk, input wire rst_b, input wire set_b,
                input wire d, input wire de, input wire sce, input wire scd,
                input wire gate,
                output wire q_df, output wire q_dfr, output wire q_dfs,
                output wire q_dfbb, output wire qn_dfbb,
                output wire q_edf, output wire q_sdf, output wire q_dl,
                output wire gclk);
  sky130_fd_sc_hd__dfxtp_1  s0 (.CLK(clk), .D(d), .Q(q_df));
  sky130_fd_sc_hd__dfrtp_1  s1 (.CLK(clk), .D(d), .RESET_B(rst_b), .Q(q_dfr));
  sky130_fd_sc_hd__dfstp_1  s2 (.CLK(clk), .D(d), .SET_B(set_b), .Q(q_dfs));
  sky130_fd_sc_hd__dfbbp_1  s3 (.CLK(clk), .D(d), .RESET_B(rst_b), .SET_B(set_b),
                                .Q(q_dfbb), .Q_N(qn_dfbb));
  sky130_fd_sc_hd__edfxtp_1 s4 (.CLK(clk), .D(d), .DE(de), .Q(q_edf));
  sky130_fd_sc_hd__sdfxtp_1 s5 (.CLK(clk), .D(d), .SCD(scd), .SCE(sce), .Q(q_sdf));
  sky130_fd_sc_hd__dlxtp_1  s6 (.GATE(gate), .D(d), .Q(q_dl));
  sky130_fd_sc_hd__dlclkp_1 s7 (.CLK(clk), .GATE(de), .GCLK(gclk));
endmodule
"""

SEQ_CPP = r"""
#include <cstdio>
#include "Vseq_top.h"
Vseq_top* dut;
int fails = 0;
void expect(const char* what, int got, int want) {
    if (got != want) { printf("MISMATCH %s: got %d expected %d\n", what, got, want); fails++; }
}
void tick() { dut->clk = 0; dut->eval(); dut->clk = 1; dut->eval(); }
int main() {
    dut = new Vseq_top;
    // init: everything inactive, latch closed
    dut->rst_b = 1; dut->set_b = 1; dut->de = 0; dut->sce = 0;
    dut->scd = 0; dut->gate = 0; dut->d = 0; dut->clk = 0; dut->eval();

    dut->d = 1; tick();                       // clock in 1
    expect("dfxtp captures 1", dut->q_df, 1);
    expect("dfrtp captures 1", dut->q_dfr, 1);
    expect("edfxtp holds (DE=0)", dut->q_edf, 0);
    expect("sdfxtp captures D", dut->q_sdf, 1);

    dut->d = 0; tick();                       // clock in 0
    expect("dfxtp captures 0", dut->q_df, 0);

    dut->rst_b = 0; dut->eval();              // async reset, no clock edge
    expect("dfrtp async reset", dut->q_dfr, 0);
    expect("dfbbp reset: Q=0", dut->q_dfbb, 0);
    expect("dfbbp reset: Q_N=1", dut->qn_dfbb, 1);
    dut->set_b = 0; dut->eval();              // both asserted
    expect("dfbbp both: Q=1", dut->q_dfbb, 1);
    expect("dfbbp both: Q_N=1", dut->qn_dfbb, 1);
    dut->set_b = 1; dut->rst_b = 1; dut->eval();

    dut->set_b = 0; dut->eval();              // async set
    expect("dfstp async set", dut->q_dfs, 1);
    dut->set_b = 1; dut->eval();

    dut->d = 1; dut->de = 1; tick();          // enable flop takes D
    expect("edfxtp captures (DE=1)", dut->q_edf, 1);
    dut->de = 0;

    dut->sce = 1; dut->scd = 0; dut->d = 1; tick();   // scan wins over D
    expect("sdfxtp takes SCD", dut->q_sdf, 0);
    dut->sce = 0;

    dut->d = 1; dut->gate = 1; dut->eval();   // latch transparent
    expect("dlxtp transparent", dut->q_dl, 1);
    dut->d = 0; dut->eval();
    expect("dlxtp follows D", dut->q_dl, 0);
    dut->gate = 0; dut->d = 1; dut->eval();   // latch closed: holds
    expect("dlxtp holds", dut->q_dl, 0);

    // clock gate: enable sampled while clk low
    dut->clk = 0; dut->de = 1; dut->eval();
    dut->clk = 1; dut->eval();
    expect("dlclkp passes clk", dut->gclk, 1);
    dut->clk = 0; dut->de = 0; dut->eval();
    dut->clk = 1; dut->eval();
    expect("dlclkp gates clk", dut->gclk, 0);
    dut->clk = 0; dut->de = 1; dut->eval();   // enable change while clk high is held
    dut->clk = 1; dut->eval(); dut->de = 0; dut->eval();
    expect("dlclkp latches enable", dut->gclk, 1);

    dut->final();
    delete dut;
    if (!fails) printf("seq OK\n");
    return fails ? 1 : 0;
}
"""


def run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("command failed: %s\n%s%s" % (" ".join(cmd), r.stdout, r.stderr))
    return r.stdout


def build_and_run(tag, verilog, cpp, workdir):
    vfile = os.path.join(workdir, tag + ".v")
    cfile = os.path.join(workdir, tag + "_main.cpp")
    with open(vfile, "w") as f:
        f.write(verilog)
    with open(cfile, "w") as f:
        f.write(cpp)
    run(["verilator", "--cc", "--exe", "--build", "-j", "0", "-o", tag + "_sim",
         os.path.join(ROOT, "lib", "sky130_cells_sim.v"), vfile, cfile,
         "--top-module", tag, "--Mdir", os.path.join(workdir, "obj_" + tag)],
        workdir)
    out = run([os.path.join(workdir, "obj_" + tag, tag + "_sim")], workdir)
    sys.stdout.write(out)
    assert "OK" in out


if __name__ == "__main__":
    out = subprocess.run([sys.executable, os.path.join(ROOT, "src", "genCellModels.py")],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    with tempfile.TemporaryDirectory() as d:
        build_and_run("comb_top", COMB_V, COMB_CPP, d)
        build_and_run("seq_top", SEQ_V, SEQ_CPP, d)
    print("all checks passed")
