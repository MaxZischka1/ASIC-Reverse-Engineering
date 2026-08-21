# The warmup reference design, driven like the real puzzle's Makefile so the
# two share target names. Two recovered netlists are simulated by the SAME
# testbench, exactly as `make` / `make reduced` do at the top level:
#
#   make         -- genVerilog.py's flat netlist   (adder_demo.v, top adder_demo)
#   make blocks  -- genBlockVerilog.py's named one (blockMatch.v,  top blockMatch)
#
# Both tops expose the same ports (A, B, clk, en, rst_n, S); adderTestbench.cpp
# picks between them on -DTOP_TYPE. Agreement between the two is what says
# blockMatch.py's naming preserved the design's function.
MODULE = adder_demo
SRCS = adder_demo.v sky130_prims.v
TB = adderTestbench.cpp
VCD = waveform_adderDemo.vcd

BLOCKMOD = blockMatch
BLOCKSRCS = blockMatch.v sky130_prims.v
BLOCKDIR = obj_dir_blocks

.PHONY: sim
sim: $(VCD)

.PHONY: verilate
verilate: .stamp.verilate

.PHONY: build
build: obj_dir_TL/V$(MODULE)

.PHONY: waves
waves: $(VCD)
	@echo
	@echo "## WAVES ##"
	gtkwave $(VCD)

$(VCD): ./obj_dir_TL/V$(MODULE)
	@echo
	@echo "##SIMULATE##"
	@./obj_dir_TL/V$(MODULE)

./obj_dir_TL/V$(MODULE): .stamp.verilate
	@echo
	@echo "## BUILDSIM ##"
	@$(MAKE) -C obj_dir_TL -f V$(MODULE).mk V$(MODULE)

.stamp.verilate: $(SRCS) $(TB)
	@echo
	@echo "## VERILATING ##"
	verilator -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL --trace -cc $(SRCS) --top-module $(MODULE) --exe $(TB) --Mdir obj_dir_TL -CFLAGS "-std=c++17"
	@touch .stamp.verilate

# The named, hierarchical reconstruction under the same testbench.
.PHONY: blocks
blocks: $(BLOCKSRCS) $(TB)
	@echo
	@echo "## VERILATING (blocks) ##"
	verilator -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL --trace -cc $(BLOCKSRCS) --top-module $(BLOCKMOD) --exe $(TB) --Mdir $(BLOCKDIR) -CFLAGS "-std=c++17 -DTOP_TYPE=V$(BLOCKMOD)"
	@$(MAKE) -C $(BLOCKDIR) -f V$(BLOCKMOD).mk V$(BLOCKMOD)
	@./$(BLOCKDIR)/V$(BLOCKMOD)

.PHONY: lint
lint: $(SRCS)
	verilator --lint-only $(SRCS) --top-module $(MODULE)

.PHONY: clean
clean:
	rm -rf .stamp.*
	rm -rf ./obj_dir_TL
	rm -rf ./$(BLOCKDIR)
	rm -rf $(VCD)
