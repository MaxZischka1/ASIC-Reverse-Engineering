// tb_try.cpp — drive ONE hand-written 121-bit frame into the DUT and dump the
// waveform. No scoreboard, no recording: pure stimulus. Edit SEQ and the framing
// constants below and rebuild.  Nothing here is puzzle-derived, so this file is
// tracked as-is (unlike tb_cones.cpp / tb_netlist.cpp, whose stimulus is inlined
// from the recording at build time).
//
//   make -C bench try                     build + run, then open waveform_try.vcd
//   ./obj_dir_try/VconesTop +verbose      print O / success each readout cycle
//
// DUT is conesTop; point --top-module / the include at netlistVerilog to try the
// flat netlist instead.

#include <cstdint>
#include <iostream>
#include <string>
#include "verilatorTB.h"
#include <VconesTop.h>
#include <verilated.h>
#include <verilated_vcd_c.h>

// One bit per cycle, SEQ[0] shifted in first. Flip the loop index if your bit
// order is the other way round.
static const char SEQ[] =
    "0000000101011000010000000000001010010000000000001010000001000001"
    "000000100000101000010000000100000010000010010001010000000";
static_assert(sizeof(SEQ) - 1 == 121, "SEQ must be exactly 121 bits");

static const int RESET_CYCLES   = 3;    // rst_n held low
static const int READOUT_CYCLES = 200;  // cycles after enable drops

static bool verbose = false;

int main(int argc, char** argv) {
    for (int i = 1; i < argc; i++)
        if (std::string(argv[i]) == "+verbose") verbose = true;

    VconesTop*     dut = new VconesTop();
    VerilatedVcdC* tfp = new VerilatedVcdC();
    setup(dut, tfp, argc, argv, "waveform_try.vcd");

    // reset
    dut->I = 0; dut->enable = 0;
    reset(dut, tfp, RESET_CYCLES);

    // shift the 121-bit frame in with enable high
    dut->enable = 1;
    for (int c = 0; c < 121; c++) {
        dut->I = (uint8_t)(SEQ[c] - '0');
        dut->eval();
        tick(dut, tfp);
    }

    // readout: enable low, I idle
    dut->I = 0; dut->enable = 0;
    for (int c = 0; c < READOUT_CYCLES; c++) {
        dut->eval();
        if (verbose)
            std::cout << "readout " << c
                      << "  O="       << (unsigned)dut->O
                      << " success="  << (unsigned)dut->success << std::endl;
        tick(dut, tfp);
    }

    std::cout << "tb_try: drove one 121-bit frame + " << READOUT_CYCLES
              << " readout cycles -> waveform_try.vcd\n";
    return finish(dut, tfp);
}
