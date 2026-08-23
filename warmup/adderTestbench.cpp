#include <deque>
#include <cstdint>
#include <cmath>
#include <iostream>
#include <stdlib.h>
#include <random>
// Drives the recovered warmup netlist: genVerilog.py's flat reconstruction
// (top `adder_demo`). The top is reached through TOP_TYPE, defaulting to
// Vadder_demo, so a second netlist exposing the same ports (A, B, clk, en,
// rst_n, S) can be checked against this same testbench by redefining it.
#ifndef TOP_TYPE
#define TOP_TYPE Vadder_demo
#endif
#define TB_STR(x) #x
#define TB_XSTR(x) TB_STR(x)
#include TB_XSTR(TOP_TYPE.h)
#include <verilated.h>
#include <verilated_vcd_c.h>


int main_time = 0;
int error_count = 0;

class AdderIn{
    public:
        bool A,B,rst_n,clk,en;
};

class AdderOut{
    public:
        bool S;
};

AdderIn* AdderGen(){
    AdderIn *tx = new AdderIn();
    std::random_device rd;
    std::mt19937 gen(rd());
    std::uniform_int_distribution<int> distrib1(0, 1);
    tx->A = distrib1(gen);
    tx->B = distrib1(gen);
    tx->clk = distrib1(gen);
    tx->en = distrib1(gen);
    tx->rst_n = distrib1(gen);
    return tx;
};

// Directed sequence: one reset cycle, then 9 en=1 cycles that shift A =
// 1,1,1,1,1,0,0,0,0 (MSB first) into the scoreboard's 9-bit shift register
// with B held at 0, so shiftOutA lands on 0b111110000 == 496 and sum == 496.
std::deque<AdderIn*> Adder496Seq(){
    std::deque<AdderIn*> seq;

    AdderIn *rst = new AdderIn();
    rst->A = 0; rst->B = 0; rst->en = 0; rst->rst_n = 0; rst->clk = 0;
    seq.push_back(rst);

    static const bool aBits[12] = {1,1,1,1,0,0,0,1,0,0,0,0};
    static const bool bBits[12] = {1,1,1,1,1,1,1,1,0,0,0,0};
    for(int i = 0; i < 12; i++){
        AdderIn *tx = new AdderIn();
        tx->A = aBits[i];
        tx->B = bBits[i];
        tx->en = 1;
        tx->rst_n = 1;
        tx->clk = 0;
        seq.push_back(tx);
    }
    return seq;
};

class TxDriver{
    private:
        TOP_TYPE *dut;
    public:
        TxDriver(TOP_TYPE *dut){
            this->dut = dut;
        }
        void drive(AdderIn *tx){
            dut->A = tx->A;
            dut->B = tx->B;
            dut->en = tx->en;
            dut->rst_n = tx->rst_n;
            dut->clk = tx->clk;
            delete tx;

        }
};


class AdderScb{
    private:
        std::deque<AdderIn*> in_q;
        uint8_t shiftOutA = 0;
        uint8_t shiftOutB = 0;
        bool prevS = false;
    public:
        void writeIn(AdderIn *tx){
            in_q.push_back(tx);
        }
        void writeOut(AdderOut *tx){
            if(in_q.empty()){
                std::cout << "Error SBMTxIN Empty" << std::endl;
                exit(1);
            }
            AdderIn *in;
            in = in_q.front();
            in_q.pop_front();

            int sum = shiftOutA + shiftOutB;
            bool S;
            if(!in->rst_n){
                shiftOutA = 0;
                shiftOutB = 0;
                S = 0;
            } else if(in->en){
                shiftOutA = ((shiftOutA << 1)|in->A);//bitmask only bottom 9 bits.
                shiftOutB = ((shiftOutB << 1)|in->B);
                sum = shiftOutA + shiftOutB;
                S = (sum==496);
            } else {
                S = prevS;
            }
            prevS = S;

        if(S==tx->S)
            std::cout << "TestBench Success.     Output S:" <<tx->S<< std::endl;
        else{
            std::cout <<"TestBench Error" <<std::endl;
            std::cout <<"Expected  S: "<<S<<"Real   S:  "<< tx->S  <<std::endl;
            std::cout <<"Inputs A:  "<<in->A<<" B:   "<<in->B<<"   en:   "<<in->en<<"    reset:  "<<in->rst_n<<std::endl;
            std::cout <<"Sum  "<<sum<<std::endl;
            error_count++;
        }
        delete in;
        delete tx;
        }
    };




class MonIn{
    private:
        TOP_TYPE *dut;
        AdderScb *scb;
    public:
        MonIn(TOP_TYPE *dut,  AdderScb *scb){
            this->dut = dut;
            this->scb = scb;
        }

        void monitor(){
            AdderIn *tx = new AdderIn;
            tx->en = dut->en;
            tx->A = dut->A;
            tx->B = dut->B;
            tx->rst_n = dut->rst_n;
            tx->clk = dut->clk;
            scb->writeIn(tx);
        }
};

class MonOut{
    private:
        TOP_TYPE *dut;
        AdderScb *scb;
    public: 
        MonOut(TOP_TYPE *dut, AdderScb *scb){
            this->dut = dut;
            this->scb = scb;
        }
        void monitor(){
            AdderOut *tx = new AdderOut;
            tx->S = dut->S;
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



int main(int argc, char** argv){
    TOP_TYPE *tb = new TOP_TYPE;
    VerilatedVcdC *tfp = new VerilatedVcdC;
    Verilated::commandArgs(argc, argv);
    Verilated::traceEverOn(true);
    tb->trace(tfp,99);
    tfp->open("waveform_adderDemo.vcd");
    AdderIn *tx;
    int max_time = 10000;
    
    TxDriver *drv = new TxDriver(tb);
    AdderScb *scb = new AdderScb();
    MonIn *monIn = new MonIn(tb,scb);
    MonOut *monOut = new MonOut(tb,scb);

    std::deque<AdderIn*> directed = Adder496Seq();

    while(!directed.empty()){
        tx = directed.front();
        directed.pop_front();
        drv->drive(tx);
        tick(tb,tfp);
        monIn->monitor();
        monOut->monitor();
    }
    delete monIn;
    delete monOut;
    delete scb;
    delete drv;
    tb->final();
    tfp->close();
    if(error_count==0){
        printf("PassedTB\n");
    }else{
        printf("Failed with %d errors\n", error_count);
    }
    delete tb;
    delete tfp;
    return (error_count == 0) ? EXIT_SUCCESS : EXIT_FAILURE;
    //make makefile tmwr
    
}



