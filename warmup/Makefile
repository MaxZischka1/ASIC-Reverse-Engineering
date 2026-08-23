# The warmup reference design, driven like the real puzzle's Makefile so the
# two share target names. Two recovered netlists are simulated by the SAME
# testbench, exactly as `make` / `make reduced` do at the top level:
#
#   make         -- genVerilog.py's flat netlist   (adder_demo.v, top adder_demo)
#
# adderTestbench.cpp reaches its top through -DTOP_TYPE, which defaults to
# Vadder_demo.
MODULE = adder_demo
SRCS = adder_demo.v sky130_prims.v
TB = adderTestbench.cpp
VCD = waveform_adderDemo.vcd

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

.PHONY: lint
lint: $(SRCS)
	verilator --lint-only $(SRCS) --top-module $(MODULE)

.PHONY: clean
clean:
	rm -rf .stamp.*
	rm -rf ./obj_dir_TL
	rm -rf $(VCD)
