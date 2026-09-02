#pragma once
// verilatorTB.h — tiny shared harness for the bench/ Verilator testbenches.
//
// House style: `new` the DUT and the VCD dumper, hand both to setup(), advance
// with tick(), end with finish(). Sim time and a running error tally are
// globals so a scoreboard can bump them from anywhere.
//
// Every DUT here (netlistVerilog, conesTop) has the same interface:
//   inputs   I, clk, enable, rst_n     (1 bit each)
//   outputs  O [7:0],  success         (8 / 1 bit)

#include <cstdint>
#include <cstdio>
#include <verilated.h>
#include <verilated_vcd_c.h>

inline uint64_t main_time  = 0;   // half-cycle VCD steps elapsed
inline int      error_count = 0;  // bumped by scoreboards

// Verilator calls this for $time / %t inside the model.
inline double sc_time_stamp() { return (double)main_time; }

template <class DUT>
void setup(DUT* dut, VerilatedVcdC* tfp, int argc, char** argv,
           const char* vcd_name) {
    Verilated::commandArgs(argc, argv);
    Verilated::traceEverOn(true);
    dut->trace(tfp, 99);
    tfp->open(vcd_name);
    dut->clk = 0;
    dut->eval();
    tfp->dump(main_time++);
}

// One clock period: settle low, rising edge, (caller samples after this).
// Two dumps = the period's two phases, so the caller's loop must not dump
// between drive and tick(); a third timestamp lands in the high phase (tick
// leaves clk high) and skews the duty cycle to 2/3.
template <class DUT>
void tick(DUT* dut, VerilatedVcdC* tfp) {
    dut->clk = 0; dut->eval(); tfp->dump(main_time++);
    dut->clk = 1; dut->eval(); tfp->dump(main_time++);
}

// Hold the active-low reset for `cycles` clocks.
template <class DUT>
void reset(DUT* dut, VerilatedVcdC* tfp, int cycles) {
    dut->rst_n = 0;
    for (int i = 0; i < cycles; i++) tick(dut, tfp);
    dut->rst_n = 1;
}

template <class DUT>
int finish(DUT* dut, VerilatedVcdC* tfp) {
    dut->eval();
    tfp->dump(main_time++);
    tfp->close();
    dut->final();
    int rc = (error_count == 0) ? 0 : 1;
    printf("%s: %d error(s) over %llu half-cycles\n",
           rc ? "TB FAIL" : "TB PASS", error_count,
           (unsigned long long)main_time);
    delete dut;
    delete tfp;
    return rc;
}
