
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <iostream>
#include <string>
#include <vector>
#include <Vpuzzle.h>
#include <verilated.h>
#include <verilated_vcd_c.h>

int main_time = 0;
int error_count = 0;

// Frame shape, in clock cycles. See PROTOCOL above.
static const int RESET_CYCLES   = 3;
static const int IDLE_CYCLES    = 1;
static const int INPUT_BITS     = 121;
static const int READOUT_CYCLES = 35;

// The two frames recorded in example_inputs.vcd, with the message the real
// design answered each of them with. Neither one is the winning input --
// the puzzle README says as much, and success stays low for both.
struct RefFrame {
    const char *name;
    const char *bits;      // INPUT_BITS chars, in shift order
    const char *expect;    // non-zero O bytes over the readout window
};

static const RefFrame REFERENCE_FRAMES[] = {    // Place bits from *.vcd
    {"example_inputs.vcd frame 1",
     "0010101000000010110000101001100000000010000001110110000100101100"
     "001110011000000010110000001011100000000010000011001110000",
     "TRY AGAIN"},
    {"example_inputs.vcd frame 2",
     "1101011000010011110000000001000001000011000011101110000100001100"
     "001001011000000101110000110011100000000010000000000100000",
     "TRY AGAIN"},
};

class PuzzleIn {
    public:
        bool rst_n, enable, I;
};

class PuzzleOut {
    public:
        uint8_t O;
        bool success;
};

// Expand a 121-bit input string into the full cycle-by-cycle stimulus for
// one frame. clk is not part of the transaction -- tick() owns it.
std::deque<PuzzleIn*> PuzzleFrame(const std::string &bits){
    std::deque<PuzzleIn*> seq;

    for(int i = 0; i < RESET_CYCLES; i++){
        PuzzleIn *tx = new PuzzleIn();
        tx->rst_n = 0; tx->enable = 0; tx->I = 0;
        seq.push_back(tx);
    }
    for(int i = 0; i < IDLE_CYCLES; i++){
        PuzzleIn *tx = new PuzzleIn();
        tx->rst_n = 1; tx->enable = 0; tx->I = 0;
        seq.push_back(tx);
    }
    for(int i = 0; i < INPUT_BITS; i++){
        PuzzleIn *tx = new PuzzleIn();
        tx->rst_n = 1; tx->enable = 1; tx->I = (bits[i] == '1');
        seq.push_back(tx);
    }
    for(int i = 0; i < READOUT_CYCLES; i++){
        PuzzleIn *tx = new PuzzleIn();
        tx->rst_n = 1; tx->enable = 0; tx->I = 0;
        seq.push_back(tx);
    }
    return seq;
};

class TxDriver{
    private:
        Vpuzzle *dut;
    public:
        TxDriver(Vpuzzle *dut){
            this->dut = dut;
        }
        void drive(PuzzleIn *tx){
            dut->rst_n  = tx->rst_n;
            dut->enable = tx->enable;
            dut->I      = tx->I;
            delete tx;
        }
};

// Collects what the design said, rather than predicting it: the message
// assembled from the readout window, and whether success ever asserted.
class PuzzleScb{
    private:
        std::deque<PuzzleIn*> in_q;
        std::string message;
        bool successSeen = false;
    public:
        void writeIn(PuzzleIn *tx){
            in_q.push_back(tx);
        }
        void writeOut(PuzzleOut *tx){
            if(in_q.empty()){
                std::cout << "Error: scoreboard input queue empty" << std::endl;
                exit(1);
            }
            PuzzleIn *in = in_q.front();
            in_q.pop_front();

            // Characters only come out during readout; O is a shift/compare
            // node the rest of the time and would otherwise add noise.
            if(!in->enable && in->rst_n && tx->O != 0){
                message.push_back(static_cast<char>(tx->O));
            }
            if(tx->success){
                successSeen = true;
            }

            delete in;
            delete tx;
        }
        const std::string &getMessage() const { return message; }
        bool getSuccessSeen() const { return successSeen; }
        void reset(){
            in_q.clear();
            message.clear();
            successSeen = false;
        }
};

class MonIn{
    private:
        Vpuzzle *dut;
        PuzzleScb *scb;
    public:
        MonIn(Vpuzzle *dut, PuzzleScb *scb){
            this->dut = dut;
            this->scb = scb;
        }
        void monitor(){
            PuzzleIn *tx = new PuzzleIn;
            tx->rst_n  = dut->rst_n;
            tx->enable = dut->enable;
            tx->I      = dut->I;
            scb->writeIn(tx);
        }
};

class MonOut{
    private:
        Vpuzzle *dut;
        PuzzleScb *scb;
    public:
        MonOut(Vpuzzle *dut, PuzzleScb *scb){
            this->dut = dut;
            this->scb = scb;
        }
        void monitor(){
            PuzzleOut *tx = new PuzzleOut;
            tx->O       = dut->O;
            tx->success = dut->success;
            scb->writeOut(tx);
        }
};

template <typename T>
void tick(T *tb, VerilatedVcdC *tfp){
    tb->clk = 0;
    tb->eval();
    tfp->dump(main_time);
    main_time += 5;

    tb->clk = 1;
    tb->eval();
    tfp->dump(main_time);
    main_time += 5;
}

// Run one frame end to end and leave the result in the scoreboard.
void runFrame(const std::string &bits, Vpuzzle *tb, VerilatedVcdC *tfp,
              TxDriver *drv, PuzzleScb *scb, MonIn *monIn, MonOut *monOut){
    scb->reset();
    std::deque<PuzzleIn*> seq = PuzzleFrame(bits);
    while(!seq.empty()){
        PuzzleIn *tx = seq.front();
        seq.pop_front();
        drv->drive(tx);
        tick(tb, tfp);
        monIn->monitor();
        monOut->monitor();
    }
}

int main(int argc, char** argv){
    Verilated::commandArgs(argc, argv);
    Verilated::traceEverOn(true);

    Vpuzzle *tb = new Vpuzzle;
    VerilatedVcdC *tfp = new VerilatedVcdC;
    tb->trace(tfp, 99);
    tfp->open("waveform_puzzle.vcd");

    TxDriver *drv = new TxDriver(tb);
    PuzzleScb *scb = new PuzzleScb();
    MonIn *monIn = new MonIn(tb, scb);
    MonOut *monOut = new MonOut(tb, scb);

    // A bit string on the command line means "try this input"; with no
    // argument we replay the reference frames and check them.
    std::string custom;
    for(int i = 1; i < argc; i++){
        std::string a = argv[i];
        if(a.size() == INPUT_BITS &&
           a.find_first_not_of("01") == std::string::npos){
            custom = a;
        }
    }

    if(!custom.empty()){
        std::cout << "Driving custom input frame" << std::endl;
        runFrame(custom, tb, tfp, drv, scb, monIn, monOut);
        std::cout << "  O message : \"" << scb->getMessage() << "\"" << std::endl;
        std::cout << "  success   : " << (scb->getSuccessSeen() ? 1 : 0) << std::endl;
        if(!scb->getSuccessSeen()){
            std::cout << "  (success never asserted -- not the winning input)"
                      << std::endl;
        }
    } else {
        const int nframes = sizeof(REFERENCE_FRAMES)/sizeof(REFERENCE_FRAMES[0]);
        for(int f = 0; f < nframes; f++){
            const RefFrame &ref = REFERENCE_FRAMES[f];
            std::string bits = ref.bits;
            if((int)bits.size() != INPUT_BITS){
                std::cout << "Error: " << ref.name << " has " << bits.size()
                          << " bits, expected " << INPUT_BITS << std::endl;
                error_count++;
                continue;
            }
            runFrame(bits, tb, tfp, drv, scb, monIn, monOut);

            const std::string &got = scb->getMessage();
            bool ok = (got == ref.expect) && !scb->getSuccessSeen();
            if(ok){
                std::cout << "TestBench Success.  " << ref.name
                          << " -> \"" << got << "\", success=0" << std::endl;
            } else {
                std::cout << "TestBench Error     " << ref.name << std::endl;
                std::cout << "  Expected O message: \"" << ref.expect
                          << "\"  success: 0" << std::endl;
                std::cout << "  Real     O message: \"" << got
                          << "\"  success: " << (scb->getSuccessSeen() ? 1 : 0)
                          << std::endl;
                error_count++;
            }
        }
    }

    delete monIn;
    delete monOut;
    delete scb;
    delete drv;
    tb->final();
    tfp->close();
    if(error_count == 0){
        printf("PassedTB\n");
    } else {
        printf("Failed with %d errors\n", error_count);
    }
    delete tb;
    delete tfp;
    return (error_count == 0) ? EXIT_SUCCESS : EXIT_FAILURE;
}
