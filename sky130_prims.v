// Behavioural models for every sky130_fd_sc_hd cell this design uses.
//
// This file is the single source of truth for what a cell IS: moduleGraph.py
// reads it for pin directions (which pin drives a net, which reads it), and
// blockMatch.py derives its whole cell library from it -- pin names, pin
// order, Boolean function, and, by testing the function, which inputs are
// interchangeable. Nothing downstream hardcodes a pinout, so a cell added
// here is a cell the rest of the pipeline immediately understands.
//
// Drive strength (the _1/_2/_4/_8/_16 suffix) is a physical property with no
// bearing on function, so cells are keyed by their base name everywhere
// downstream; one module per base name is enough. The extra clkbuf sizes at
// the bottom are here only so a generated netlist that instantiates them by
// full name still elaborates.
//
// Pin naming follows the library's own convention: a trailing _N marks a leg
// presented pre-inverted (A_N, B1_N), "b" in a cell name marks which leg that
// is, and a trailing "i" marks an inverted output (Y rather than X).

// ---------------------------------------------------------------------------
// Buffers, inverters, and physical-only cells
// ---------------------------------------------------------------------------

module sky130_fd_sc_hd__inv_2 (
    input  A,
    output Y
);
    assign Y = ~A;
endmodule

module sky130_fd_sc_hd__buf_2 (
    input  A,
    output X
);
    assign X = A;
endmodule

module sky130_fd_sc_hd__clkbuf_8 (
    input  A,
    output X
);
    assign X = A;
endmodule

// Constant generator: ties for logic 1 and logic 0.
module sky130_fd_sc_hd__conb_1 (
    output HI,
    output LO
);
    assign HI = 1'b1;
    assign LO = 1'b0;
endmodule

// Antenna diode: a real cell with a real pin, but no logic function and no
// output. It appears in the netlist and must be modelled, or its pin looks
// like an undriven net.
module sky130_fd_sc_hd__diode_2 (
    input DIODE
);
endmodule

// ---------------------------------------------------------------------------
// AND / NAND
// ---------------------------------------------------------------------------

module sky130_fd_sc_hd__and2_2 (
    input  A,
    input  B,
    output X
);
    assign X = A & B;
endmodule

module sky130_fd_sc_hd__and3_2 (
    input  A,
    input  B,
    input  C,
    output X
);
    assign X = A & B & C;
endmodule

module sky130_fd_sc_hd__and4_2 (
    input  A,
    input  B,
    input  C,
    input  D,
    output X
);
    assign X = A & B & C & D;
endmodule

// AND with the first input presented pre-inverted (the "b" leg).
module sky130_fd_sc_hd__and2b_2 (
    input  A_N,
    input  B,
    output X
);
    assign X = (~A_N) & B;
endmodule

module sky130_fd_sc_hd__and3b_2 (
    input  A_N,
    input  B,
    input  C,
    output X
);
    assign X = (~A_N) & B & C;
endmodule

module sky130_fd_sc_hd__and4b_2 (
    input  A_N,
    input  B,
    input  C,
    input  D,
    output X
);
    assign X = (~A_N) & B & C & D;
endmodule

// AND4 with the first two inputs presented pre-inverted (the "bb" legs).
module sky130_fd_sc_hd__and4bb_2 (
    input  A_N,
    input  B_N,
    input  C,
    input  D,
    output X
);
    assign X = (~A_N) & (~B_N) & C & D;
endmodule

module sky130_fd_sc_hd__nand2_2 (
    input  A,
    input  B,
    output Y
);
    assign Y = ~(A & B);
endmodule

module sky130_fd_sc_hd__nand3_2 (
    input  A,
    input  B,
    input  C,
    output Y
);
    assign Y = ~(A & B & C);
endmodule

module sky130_fd_sc_hd__nand4_2 (
    input  A,
    input  B,
    input  C,
    input  D,
    output Y
);
    assign Y = ~(A & B & C & D);
endmodule

module sky130_fd_sc_hd__nand2b_2 (
    input  A_N,
    input  B,
    output Y
);
    assign Y = ~((~A_N) & B);
endmodule

module sky130_fd_sc_hd__nand3b_2 (
    input  A_N,
    input  B,
    input  C,
    output Y
);
    assign Y = ~((~A_N) & B & C);
endmodule

// ---------------------------------------------------------------------------
// OR / NOR
// ---------------------------------------------------------------------------

module sky130_fd_sc_hd__or2_2 (
    input  A,
    input  B,
    output X
);
    assign X = A | B;
endmodule

module sky130_fd_sc_hd__or3_2 (
    input  A,
    input  B,
    input  C,
    output X
);
    assign X = A | B | C;
endmodule

module sky130_fd_sc_hd__or4_2 (
    input  A,
    input  B,
    input  C,
    input  D,
    output X
);
    assign X = A | B | C | D;
endmodule

// OR with the last input presented pre-inverted.
module sky130_fd_sc_hd__or3b_2 (
    input  A,
    input  B,
    input  C_N,
    output X
);
    assign X = A | B | (~C_N);
endmodule

module sky130_fd_sc_hd__or4b_2 (
    input  A,
    input  B,
    input  C,
    input  D_N,
    output X
);
    assign X = A | B | C | (~D_N);
endmodule

module sky130_fd_sc_hd__or4bb_2 (
    input  A,
    input  B,
    input  C_N,
    input  D_N,
    output X
);
    assign X = A | B | (~C_N) | (~D_N);
endmodule

module sky130_fd_sc_hd__nor2_2 (
    input  A,
    input  B,
    output Y
);
    assign Y = ~(A | B);
endmodule

module sky130_fd_sc_hd__nor3_2 (
    input  A,
    input  B,
    input  C,
    output Y
);
    assign Y = ~(A | B | C);
endmodule

module sky130_fd_sc_hd__nor4_2 (
    input  A,
    input  B,
    input  C,
    input  D,
    output Y
);
    assign Y = ~(A | B | C | D);
endmodule

module sky130_fd_sc_hd__nor3b_2 (
    input  A,
    input  B,
    input  C_N,
    output Y
);
    assign Y = ~(A | B | (~C_N));
endmodule

module sky130_fd_sc_hd__nor4b_2 (
    input  A,
    input  B,
    input  C,
    input  D_N,
    output Y
);
    assign Y = ~(A | B | C | (~D_N));
endmodule

// ---------------------------------------------------------------------------
// XOR / XNOR
// ---------------------------------------------------------------------------

module sky130_fd_sc_hd__xor2_2 (
    input  A,
    input  B,
    output X
);
    assign X = A ^ B;
endmodule

module sky130_fd_sc_hd__xnor2_2 (
    input  A,
    input  B,
    output Y
);
    assign Y = ~(A ^ B);
endmodule

// ---------------------------------------------------------------------------
// AND-OR compounds (aNMxx): AND terms first, OR'd together.
// The digits give the width of each AND term in order, "b" marks a leg
// presented pre-inverted, and a trailing "i" inverts the output.
// ---------------------------------------------------------------------------

// X = (A1 & A2) | B1
module sky130_fd_sc_hd__a21o_2 (
    input  A1,
    input  A2,
    input  B1,
    output X
);
    assign X = (A1 & A2) | B1;
endmodule

// Y = ~((A1 & A2) | B1)
module sky130_fd_sc_hd__a21oi_2 (
    input  A1,
    input  A2,
    input  B1,
    output Y
);
    assign Y = ~((A1 & A2) | B1);
endmodule

// X = (A1 & A2) | ~B1_N
module sky130_fd_sc_hd__a21bo_2 (
    input  A1,
    input  A2,
    input  B1_N,
    output X
);
    assign X = (A1 & A2) | (~B1_N);
endmodule

// Y = ~((A1 & A2) | ~B1_N)
module sky130_fd_sc_hd__a21boi_2 (
    input  A1,
    input  A2,
    input  B1_N,
    output Y
);
    assign Y = ~((A1 & A2) | (~B1_N));
endmodule

// X = (A1 & A2) | B1 | C1
module sky130_fd_sc_hd__a211o_2 (
    input  A1,
    input  A2,
    input  B1,
    input  C1,
    output X
);
    assign X = (A1 & A2) | B1 | C1;
endmodule

// Y = ~((A1 & A2) | B1 | C1)
module sky130_fd_sc_hd__a211oi_2 (
    input  A1,
    input  A2,
    input  B1,
    input  C1,
    output Y
);
    assign Y = ~((A1 & A2) | B1 | C1);
endmodule

// Y = ~((A1 & A2) | B1 | C1 | D1)
module sky130_fd_sc_hd__a2111oi_2 (
    input  A1,
    input  A2,
    input  B1,
    input  C1,
    input  D1,
    output Y
);
    assign Y = ~((A1 & A2) | B1 | C1 | D1);
endmodule

// X = (A1 & A2) | (B1 & B2)
module sky130_fd_sc_hd__a22o_2 (
    input  A1,
    input  A2,
    input  B1,
    input  B2,
    output X
);
    assign X = (A1 & A2) | (B1 & B2);
endmodule

// Y = ~((A1 & A2) | (B1 & B2))
module sky130_fd_sc_hd__a22oi_2 (
    input  A1,
    input  A2,
    input  B1,
    input  B2,
    output Y
);
    assign Y = ~((A1 & A2) | (B1 & B2));
endmodule

// X = (A1 & A2) | (B1 & B2) | C1
module sky130_fd_sc_hd__a221o_2 (
    input  A1,
    input  A2,
    input  B1,
    input  B2,
    input  C1,
    output X
);
    assign X = (A1 & A2) | (B1 & B2) | C1;
endmodule

// Y = ~((A1 & A2) | (B1 & B2) | C1)
module sky130_fd_sc_hd__a221oi_2 (
    input  A1,
    input  A2,
    input  B1,
    input  B2,
    input  C1,
    output Y
);
    assign Y = ~((A1 & A2) | (B1 & B2) | C1);
endmodule

// X = (A1 & A2 & A3) | B1
module sky130_fd_sc_hd__a31o_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    output X
);
    assign X = (A1 & A2 & A3) | B1;
endmodule

// Y = ~((A1 & A2 & A3) | B1)
module sky130_fd_sc_hd__a31oi_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    output Y
);
    assign Y = ~((A1 & A2 & A3) | B1);
endmodule

// X = (A1 & A2 & A3) | B1 | C1
module sky130_fd_sc_hd__a311o_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    input  C1,
    output X
);
    assign X = (A1 & A2 & A3) | B1 | C1;
endmodule

// X = (A1 & A2 & A3) | (B1 & B2)
module sky130_fd_sc_hd__a32o_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    input  B2,
    output X
);
    assign X = (A1 & A2 & A3) | (B1 & B2);
endmodule

// Y = ~((A1 & A2 & A3 & A4) | B1)
module sky130_fd_sc_hd__a41oi_2 (
    input  A1,
    input  A2,
    input  A3,
    input  A4,
    input  B1,
    output Y
);
    assign Y = ~((A1 & A2 & A3 & A4) | B1);
endmodule

// ---------------------------------------------------------------------------
// OR-AND compounds (oNMxx): OR terms first, AND'd together.
// ---------------------------------------------------------------------------

// X = (A1 | A2) & B1
module sky130_fd_sc_hd__o21a_2 (
    input  A1,
    input  A2,
    input  B1,
    output X
);
    assign X = (A1 | A2) & B1;
endmodule

// Y = ~((A1 | A2) & B1)
module sky130_fd_sc_hd__o21ai_2 (
    input  A1,
    input  A2,
    input  B1,
    output Y
);
    assign Y = ~((A1 | A2) & B1);
endmodule

// X = (A1 | A2) & ~B1_N
module sky130_fd_sc_hd__o21ba_2 (
    input  A1,
    input  A2,
    input  B1_N,
    output X
);
    assign X = (A1 | A2) & (~B1_N);
endmodule

// Y = ~((A1 | A2) & ~B1_N)
module sky130_fd_sc_hd__o21bai_2 (
    input  A1,
    input  A2,
    input  B1_N,
    output Y
);
    assign Y = ~((A1 | A2) & (~B1_N));
endmodule

// X = (A1 | A2) & B1 & C1
module sky130_fd_sc_hd__o211a_2 (
    input  A1,
    input  A2,
    input  B1,
    input  C1,
    output X
);
    assign X = (A1 | A2) & B1 & C1;
endmodule

// Y = ~((A1 | A2) & B1 & C1)
module sky130_fd_sc_hd__o211ai_2 (
    input  A1,
    input  A2,
    input  B1,
    input  C1,
    output Y
);
    assign Y = ~((A1 | A2) & B1 & C1);
endmodule

// X = (A1 | A2) & (B1 | B2)
module sky130_fd_sc_hd__o22a_2 (
    input  A1,
    input  A2,
    input  B1,
    input  B2,
    output X
);
    assign X = (A1 | A2) & (B1 | B2);
endmodule

// Y = ~((A1 | A2) & (B1 | B2))
module sky130_fd_sc_hd__o22ai_2 (
    input  A1,
    input  A2,
    input  B1,
    input  B2,
    output Y
);
    assign Y = ~((A1 | A2) & (B1 | B2));
endmodule

// X = (A1 | A2) & (B1 | B2) & C1
module sky130_fd_sc_hd__o221a_2 (
    input  A1,
    input  A2,
    input  B1,
    input  B2,
    input  C1,
    output X
);
    assign X = (A1 | A2) & (B1 | B2) & C1;
endmodule

// Both legs of the first OR presented pre-inverted:
// X = (~A1_N | ~A2_N) & (B1 | B2)
module sky130_fd_sc_hd__o2bb2a_2 (
    input  A1_N,
    input  A2_N,
    input  B1,
    input  B2,
    output X
);
    assign X = ((~A1_N) | (~A2_N)) & (B1 | B2);
endmodule

// X = (A1 | A2 | A3) & B1
module sky130_fd_sc_hd__o31a_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    output X
);
    assign X = (A1 | A2 | A3) & B1;
endmodule

// Y = ~((A1 | A2 | A3) & B1)
module sky130_fd_sc_hd__o31ai_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    output Y
);
    assign Y = ~((A1 | A2 | A3) & B1);
endmodule

// X = (A1 | A2 | A3) & B1 & C1
module sky130_fd_sc_hd__o311a_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    input  C1,
    output X
);
    assign X = (A1 | A2 | A3) & B1 & C1;
endmodule

// X = (A1 | A2 | A3) & (B1 | B2)
module sky130_fd_sc_hd__o32a_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    input  B2,
    output X
);
    assign X = (A1 | A2 | A3) & (B1 | B2);
endmodule

// Y = ~((A1 | A2 | A3) & (B1 | B2))
module sky130_fd_sc_hd__o32ai_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    input  B2,
    output Y
);
    assign Y = ~((A1 | A2 | A3) & (B1 | B2));
endmodule

// ---------------------------------------------------------------------------
// Multiplexer
// ---------------------------------------------------------------------------

module sky130_fd_sc_hd__mux2_1 (
    input  A0,
    input  A1,
    input  S,
    output X
);
    assign X = S ? A1 : A0;
endmodule

// ---------------------------------------------------------------------------
// Flip-flops
// ---------------------------------------------------------------------------

// D flip-flop, posedge CLK, asynchronous active-low reset.
module sky130_fd_sc_hd__dfrtp_2 (
    input  CLK,
    input  D,
    input  RESET_B,
    output reg Q
);
    always @(posedge CLK or negedge RESET_B) begin
        if (!RESET_B)
            Q <= 1'b0;
        else
            Q <= D;
    end
endmodule

// D flip-flop, posedge CLK, asynchronous active-low set.
module sky130_fd_sc_hd__dfstp_2 (
    input  CLK,
    input  D,
    input  SET_B,
    output reg Q
);
    always @(posedge CLK or negedge SET_B) begin
        if (!SET_B)
            Q <= 1'b1;
        else
            Q <= D;
    end
endmodule

// D flip-flop, posedge CLK, no set or reset.
module sky130_fd_sc_hd__dfxtp_2 (
    input  CLK,
    input  D,
    output reg Q
);
    always @(posedge CLK) begin
        Q <= D;
    end
endmodule

// ---------------------------------------------------------------------------
// Remaining drive-strength variants. Function is identical to the base cell
// above; these exist so a netlist instantiating them by full name elaborates.
// ---------------------------------------------------------------------------

module sky130_fd_sc_hd__clkbuf_4 (
    input  A,
    output X
);
    assign X = A;
endmodule

module sky130_fd_sc_hd__clkbuf_16 (
    input  A,
    output X
);
    assign X = A;
endmodule
