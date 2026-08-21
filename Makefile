MODULE=puzzle
SRCS = puzzleNetlist.v sky130_prims.v
TB = puzzleTestbench.cpp
VCD = waveform_puzzle.vcd

.PHONY:sim
sim: $(VCD)

.PHONY: verilate
verilate: .stamp.verilate

.PHONY: build
build: obj_dir_TL/V$(MODULE)

.PHONY: waves
waves:  $(VCD)
	@echo
	@echo "## WAVES ##"
	gtkwave  $(VCD)

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


# Buffer/inverter-reduced netlist. `make reduced` runs the SAME testbench
# against it: if the reference frames still pass, the reduction in
# logicGraph.py preserved the design's function.
REDUCED = puzzleReduced.v
REDDIR = obj_dir_reduced

$(REDUCED) LOGIC_GRAPH.json: logicGraph.py MODULE_GRAPH.json sky130_prims.v
	python3 logicGraph.py --verilog $(REDUCED)

.PHONY: logic
logic: LOGIC_GRAPH.json

.PHONY: reduced
reduced: $(REDUCED) sky130_prims.v $(TB)
	@echo
	@echo "## VERILATING (reduced) ##"
	verilator -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL --trace -cc $(REDUCED) sky130_prims.v --top-module $(MODULE) --exe $(TB) --Mdir $(REDDIR) -CFLAGS "-std=c++17"
	@$(MAKE) -C $(REDDIR) -f V$(MODULE).mk V$(MODULE)
	@./$(REDDIR)/V$(MODULE)

.PHONY:lint
lint: $(SRCS)
	verilator --lint-only $(SRCS) --top-module $(MODULE)

.PHONY: clean
clean:
	rm -rf .stamp.*;
	rm -rf ./obj_dir_TL
	rm -rf ./$(REDDIR)
	rm -rf  $(VCD)
