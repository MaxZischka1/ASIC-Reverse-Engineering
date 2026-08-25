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

# Cone classification and the cone-level dataflow graph -- the top of the
# reconstruction as it stands. Both derive from LOGIC_GRAPH.json.
CONE_CLASSES.json: coneClasses.py LOGIC_GRAPH.json sky130_prims.v cellLibrary.py
	python3 coneClasses.py

CONE_GRAPH.json CONE_GRAPH.md: coneGraph.py CONE_CLASSES.json LOGIC_GRAPH.json
	python3 coneGraph.py

.PHONY: cones
cones: CONE_GRAPH.json

# Descriptive per-cone attributes and the four class views built from them.
CONE_PROFILE.json CONE_PROFILE.md: coneProfile.py LOGIC_GRAPH.json CONE_GRAPH.json
	python3 coneProfile.py

.PHONY: profile
profile: CONE_PROFILE.json

# Signal-side view: shared control lines and cone correlation.
CONE_SIGNALS.json CONE_SIGNALS.md: coneSignals.py LOGIC_GRAPH.json CONE_GRAPH.json CONE_PROFILE.json
	python3 coneSignals.py

.PHONY: signals
signals: CONE_SIGNALS.json

# Control/data domains and typed blocks -- the first level above the cone.
CONE_BLOCKS.json CONE_BLOCKS.md: coneBlocks.py LOGIC_GRAPH.json CONE_GRAPH.json CONE_PROFILE.json CONE_SIGNALS.json
	python3 coneBlocks.py

.PHONY: blocks
blocks: CONE_BLOCKS.json

.PHONY:lint
lint: $(SRCS)
	verilator --lint-only $(SRCS) --top-module $(MODULE)

.PHONY: clean
clean:
	rm -rf .stamp.*;
	rm -rf ./obj_dir_TL
	rm -rf ./$(REDDIR)
	rm -rf  $(VCD)
