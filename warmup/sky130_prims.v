
module sky130_fd_sc_hd__nand2_2 (
    input  A,
    input  B,
    output Y
);
    assign Y = ~(A & B);
endmodule

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

module sky130_fd_sc_hd__or2_2 (
    input  A,
    input  B,
    output X
);
    assign X = A | B;
endmodule

module sky130_fd_sc_hd__nor2_2 (
    input  A,
    input  B,
    output Y
);
    assign Y = ~(A | B);
endmodule

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

module sky130_fd_sc_hd__clkbuf_16 (
    input  A,
    output X
);
    assign X = A;
endmodule

module sky130_fd_sc_hd__mux2_1 (
    input  A0,
    input  A1,
    input  S,
    output X
);
    assign X = S ? A1 : A0;
endmodule

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

// AND2-OR1: X = (A1 & A2) | B1
module sky130_fd_sc_hd__a21o_2 (
    input  A1,
    input  A2,
    input  B1,
    output X
);
    assign X = (A1 & A2) | B1;
endmodule

// AND3-OR1: X = (A1 & A2 & A3) | B1
module sky130_fd_sc_hd__a31o_2 (
    input  A1,
    input  A2,
    input  A3,
    input  B1,
    output X
);
    assign X = (A1 & A2 & A3) | B1;
endmodule

// AND2-OR1 with the OR's second leg presented pre-inverted:
// X = (A1 & A2) | ~B1_N
module sky130_fd_sc_hd__a21bo_2 (
    input  A1,
    input  A2,
    input  B1_N,
    output X
);
    assign X = (A1 & A2) | (~B1_N);
endmodule

// Same as a21bo but with the final output inverted:
// Y = ~((A1 & A2) | ~B1_N)
module sky130_fd_sc_hd__a21boi_2 (
    input  A1,
    input  A2,
    input  B1_N,
    output Y
);
    assign Y = ~((A1 & A2) | (~B1_N));
endmodule

// OR2-AND1-INVERT with the AND's second leg presented pre-inverted:
// Y = ~((A1 | A2) & ~B1_N)
module sky130_fd_sc_hd__o21bai_2 (
    input  A1,
    input  A2,
    input  B1_N,
    output Y
);
    assign Y = ~((A1 | A2) & (~B1_N));
endmodule
