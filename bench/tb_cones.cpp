// tb_cones.cpp — replay the recorded waveform against the cone-assembled netlist.
//
// DUT: conesTop (bench/ConesVerilog.v). Inputs I/enable/rst_n each cycle come
// from the stimulus block inlined below (bench/vcdToStimulus.py on the
// recording); the recorded O and success are the expected outputs. Reports the
// first cycle the DUT diverges.
//
//   make -C bench cones                 summary line only
//   ./obj_dir_cones/VconesTop +verbose  per-cycle detail
//
// Every signal is one binary string covering the whole run -- STIM_W_<sig> chars
// per cycle, MSB first, so STIM_I reads "0100101101001...", STIM_O (8 bits)
// "0101010001001000...", and 'x' marks a cycle the recording left unknown.
// STIM(sig, c) decodes one cycle to a uint64_t, STIM_KNOWN(sig, c) says whether
// that cycle held real bits.
//
// The signals used below (STIM_I / STIM_enable / STIM_rst_n / STIM_O /
// STIM_success) must exist in the recording -- vcdToStimulus.py prints the names
// it found. Rename them here if yours differ; a missing input is a compile
// error, a missing O just skips checking. STIM_O_TEXT holds the recorded 8-bit
// output as plain ASCII, so an output that spells something is readable here.

#include <deque>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include "verilatorTB.h"
#include <VconesTop.h>
#include <verilated.h>
#include <verilated_vcd_c.h>
// ---- BEGIN GENERATED STIMULUS (bench/vcdToStimulus.py) -- do not edit ----
// source: ../example_inputs.vcd   clock: clk   sample: before
//
// One binary string per signal, the whole run back to back: STIM_W_<sig>
// chars per cycle, MSB first, 'x' for a cycle the recording left at x/z.
#define STIM_CYCLES 312
#define STIM_NSIGNALS 6
static const char* const STIM_NAMES  = "clk,rst_n,enable,I,O,success";
static const char* const STIM_WIDTHS = "1,1,1,1,8,1";

// clk  [1 bit]  1 char per cycle, 312 cycles
#define STIM_HAS_clk 1
#define STIM_IX_clk 0
#define STIM_W_clk 1
static const char* const STIM_clk =
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000";

// rst_n  [1 bit]  1 char per cycle, 312 cycles
#define STIM_HAS_rst_n 1
#define STIM_IX_rst_n 1
#define STIM_W_rst_n 1
static const char* const STIM_rst_n =
  "000111111111111111111111111111111111111111111111111111111111111111111111"
  "111111111111111111111111111111111111111111111111111111111111111111111111"
  "111111111111000111111111111111111111111111111111111111111111111111111111"
  "111111111111111111111111111111111111111111111111111111111111111111111111"
  "111111111111111111111111";

// enable  [1 bit]  1 char per cycle, 312 cycles
#define STIM_HAS_enable 1
#define STIM_IX_enable 2
#define STIM_W_enable 1
static const char* const STIM_enable =
  "000011111111111111111111111111111111111111111111111111111111111111111111"
  "111111111111111111111111111111111111111111111111111110000000000000000000"
  "000000000000000011111111111111111111111111111111111111111111111111111111"
  "111111111111111111111111111111111111111111111111111111111111111110000000"
  "000000000000000000000000";

// I  [1 bit]  1 char per cycle, 312 cycles
#define STIM_HAS_I 1
#define STIM_IX_I 3
#define STIM_W_I 1
static const char* const STIM_I = //RULE. EVERY ROW HAS TWO ONES ADJACENT TO NONE.
  "0000"
  "00000101000"
  "00000010100"
  "10000000010"
  "01100000000"
  "00100100000"
  "00000000110"
  "00000010001"
  "00011000000"
  "00000001001"
  "10001000000"
  "01010000000"
  "0000000000000000000"
  "000000000000000011010110000100111100000000010000010000110000111011100001"
  "000011000010010110000001011100001100111000000000100000000001000000000000"
  "000000000000000000000000";

// O  [8 bits]  8 chars per cycle, 312 cycles
#define STIM_HAS_O 1
#define STIM_IX_O 4
#define STIM_W_O 8
static const char* const STIM_O =
  "xxxxxxxx0000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "010101000101001001011001001000000100000101000111010000010100100101001110"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000010101000101001001011001001000000100000101000111"
  "010000010100100101001110000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000";
// the same signal read as ASCII, one byte per cycle
static const char* const STIM_O_TEXT =
  "?\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000TRY AGAIN\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000TRY AGAIN\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000";

// success  [1 bit]  1 char per cycle, 312 cycles
#define STIM_HAS_success 1
#define STIM_IX_success 5
#define STIM_W_success 1
static const char* const STIM_success =
  "x00000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000000000000000000000000000000000000000000000000000"
  "000000000000000000000000";

// STIM(sig, cyc) -> that cycle's value; 'x' bits decode as 0, so pair
// it with STIM_KNOWN(sig, cyc) before comparing against a DUT.
static inline uint64_t stimVal(const char* s, int w, int cyc) {
  if (cyc < 0 || cyc >= STIM_CYCLES) return 0;
  uint64_t v = 0;
  for (int b = 0; b < w; b++)
    v = (v << 1) | (uint64_t)(s[(size_t)cyc * w + b] == '1');
  return v; }

static inline int stimKnown(const char* s, int w, int cyc) {
  if (cyc < 0 || cyc >= STIM_CYCLES) return 0;
  for (int b = 0; b < w; b++)
    if (s[(size_t)cyc * w + b] != '0' && s[(size_t)cyc * w + b] != '1')
      return 0;
  return 1; }

#define STIM(sig, cyc)       stimVal(STIM_##sig, STIM_W_##sig, (cyc))
#define STIM_KNOWN(sig, cyc) stimKnown(STIM_##sig, STIM_W_##sig, (cyc))
// ---- END GENERATED STIMULUS ----

#if !defined(STIM_HAS_I) || !defined(STIM_HAS_enable) || !defined(STIM_HAS_rst_n)
#  error "no stimulus for I/enable/rst_n: run `make -C bench stimulus`, or rename these signals to match the recording"
#endif

static bool verbose = false;

// ---------------------------------------------------------------- transactions
class TxIn {
public:
    int      cycle;
    uint64_t I, enable, rst_n;
};

class TxOut {
public:
    uint64_t O, success;
};

// -------------------------------------------------- stimulus -> input queue
std::deque<TxIn*> TxGenQueue() {
    std::deque<TxIn*> seq;
    for (int c = 0; c < STIM_CYCLES; c++) {
        TxIn* tx = new TxIn();
        tx->cycle  = c;
        tx->I      = STIM(I, c);
        tx->enable = STIM(enable, c);
        tx->rst_n  = STIM(rst_n, c);
        seq.push_back(tx);
    }
    return seq;
}

// --------------------------------------------------------------------- driver
class TxDrive {
    VconesTop* dut;
public:
    TxDrive(VconesTop* dut) : dut(dut) {}
    void drive(TxIn* tx) {
        dut->I      = tx->I;
        dut->enable = tx->enable;
        dut->rst_n  = tx->rst_n;
        delete tx;
    }
};

// ----------------------------------------------------------------- scoreboard
class Scb {
    std::deque<TxIn*> in_q;
    int first_div = -1, mismatches = 0, compared = 0;
public:
    void writeIn(TxIn* tx) { in_q.push_back(tx); }

    void writeOut(TxOut* tx) {
        if (in_q.empty()) { std::cout << "Error: TxIn queue empty\n"; exit(1); }
        TxIn* in = in_q.front(); in_q.pop_front();
        int c = in->cycle;

#ifdef STIM_HAS_O
        if (STIM_KNOWN(O, c)) {
            compared++;
            uint64_t eO = STIM(O, c);
#ifdef STIM_HAS_success
            bool     sKnown = STIM_KNOWN(success, c);
            uint64_t eS     = STIM(success, c);
#else
            bool     sKnown = false;
            uint64_t eS     = 0;
#endif
            if (eO != tx->O || (sKnown && eS != tx->success)) {
                mismatches++;
                error_count++;
                if (first_div < 0) first_div = c;
                if (verbose)
                    std::cout << "MISMATCH cyc " << c
                              << "  exp O=" << eO
                              << " success=" << (int)eS
                              << "  got O=" << tx->O
                              << " success=" << tx->success << std::endl;
            } else if (verbose) {
                std::cout << "ok  cyc " << c << "  O=" << tx->O << std::endl;
            }
        }
#else
        (void)c;                       // recording has no O to compare against
#endif
        delete in;
        delete tx;
    }

    void report() {
        if (compared == 0)
            std::cout << "REPLAY: no known recorded O to compare against\n";
        else if (mismatches == 0)
            std::cout << "REPLAY: DUT matches the recording on all "
                      << compared << " compared cycles\n";
        else
            std::cout << "REPLAY: first divergence at cycle " << first_div
                      << " (" << mismatches << " of " << compared
                      << " compared cycles differ)\n";
    }
    ~Scb() { for (TxIn* p : in_q) delete p; }
};

// ------------------------------------------------------------------- monitors
class MonIn {
    VconesTop* dut; Scb* scb; int c = 0;
public:
    MonIn(VconesTop* dut, Scb* scb) : dut(dut), scb(scb) {}
    void monitor() {
        TxIn* tx = new TxIn();
        tx->cycle  = c++;
        tx->I      = dut->I;
        tx->enable = dut->enable;
        tx->rst_n  = dut->rst_n;
        scb->writeIn(tx);
    }
};

class MonOut {
    VconesTop* dut; Scb* scb;
public:
    MonOut(VconesTop* dut, Scb* scb) : dut(dut), scb(scb) {}
    void monitor() {
        TxOut* tx = new TxOut();
        tx->O       = (uint64_t)dut->O;
        tx->success = (uint64_t)dut->success;
        scb->writeOut(tx);
    }
};

// ------------------------------------------------------------------------ main
int main(int argc, char** argv) {
    for (int i = 1; i < argc; i++)
        if (std::string(argv[i]) == "+verbose") verbose = true;

    VconesTop*     dut = new VconesTop();
    VerilatedVcdC* tfp = new VerilatedVcdC();
    setup(dut, tfp, argc, argv, "waveform_cones.vcd");

    TxDrive drv(dut);
    Scb     scb;
    MonIn   monIn(dut, &scb);
    MonOut  monOut(dut, &scb);

    std::deque<TxIn*> seq = TxGenQueue();
    for (int c = 0; !seq.empty(); c++) {
        drv.drive(seq.front()); seq.pop_front();   // drive
        dut->eval();                               // eval
        monIn.monitor();                           // check (pre-edge)
        monOut.monitor();
        tick(dut, tfp);                            // tick
    }

    scb.report();
    return finish(dut, tfp);
}
