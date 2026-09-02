# bench/Makefile — build & run the Verilator replay testbenches.
#
#   make -C bench stimulus    parse $(VCD) -> the stimulus block inside tb_*.cpp
#   make -C bench cones       regen ConesVerilog.v, build VconesTop, replay the stimulus
#   make -C bench netlist     regen netlistVerilog.v, build VnetlistVerilog, replay it
#   make -C bench models      regen ../lib/sky130_cells_sim.v
#   make -C bench lint-cones / lint-netlist
#   make -C bench clean
#
# Paths are relative to bench/. The DUTs and cell models are generated from
# out/*.json and are git-ignored; the testbenches carry the recording's stimulus
# inline (one binary string per signal) and are tracked as they stand.

MODELS = ../lib/sky130_cells_sim.v
LEF    = ../lib/sky130_fd_sc_hd_merged.lef
GRAPH  = ../out/NETLIST_GRAPH.json
CONES  = ../out/CONES.json
VCD    ?= ../example_inputs.vcd
CLOCK  ?= clk

VFLAGS = -Wall -Wno-fatal -Wno-MULTITOP -Wno-DECLFILENAME -Wno-PINCONNECTEMPTY \
         -Wno-UNDRIVEN -Wno-UNUSEDSIGNAL
CFLAGS = -std=c++17 -I..

.PHONY: cones netlist try models stimulus lint-cones lint-netlist clean

# ------------------------------------------------------------------- cell models
$(MODELS): ../src/genCellModels.py $(LEF)
	python3 ../src/genCellModels.py --lef $(LEF) --out $(MODELS)
models: $(MODELS)

# --------------------------------------------------------------------- stimulus
# Rewrites tb_cones.cpp and tb_netlist.cpp in place: everything between their
# BEGIN/END GENERATED STIMULUS markers becomes one binary string per signal,
# STIM_W_<sig> chars per cycle. It edits tracked source, so it is never a
# prerequisite of a build -- run it by hand when the recording changes.
stimulus:
	python3 vcdToStimulus.py $(VCD) --clock $(CLOCK) \
	    --inline tb_cones.cpp --inline tb_netlist.cpp --json stimulus.json

# ------------------------------------------------------------------------- cones
ConesVerilog.v: conesToVerilog.py netlistVerilog.py $(CONES) $(GRAPH)
	python3 conesToVerilog.py $(CONES) $(GRAPH) --out $@ --top-module conesTop

obj_dir_cones/VconesTop: ConesVerilog.v $(MODELS) tb_cones.cpp verilatorTB.h
	verilator $(VFLAGS) --trace -cc ConesVerilog.v $(MODELS) \
	    --top-module conesTop --exe ../tb_cones.cpp \
	    --Mdir obj_dir_cones -CFLAGS "$(CFLAGS)"
	$(MAKE) -C obj_dir_cones -f VconesTop.mk VconesTop

cones: obj_dir_cones/VconesTop
	@echo "## REPLAY tb_cones ##"
	@./obj_dir_cones/VconesTop

lint-cones: ConesVerilog.v $(MODELS)
	verilator --lint-only $(VFLAGS) --top-module conesTop ConesVerilog.v $(MODELS)

# --------------------------------------------------- try: one hand-written frame
# tb_try.cpp carries its own hand-written 121-bit frame, not the recording.
obj_dir_try/VconesTop: ConesVerilog.v $(MODELS) tb_try.cpp verilatorTB.h
	verilator $(VFLAGS) --trace -cc ConesVerilog.v $(MODELS) \
	    --top-module conesTop --exe ../tb_try.cpp \
	    --Mdir obj_dir_try -CFLAGS "$(CFLAGS)"
	$(MAKE) -C obj_dir_try -f VconesTop.mk VconesTop

try: obj_dir_try/VconesTop
	@echo "## RUN tb_try ##"
	@./obj_dir_try/VconesTop

# ----------------------------------------------------------------------- netlist
netlistVerilog.v: netlistVerilog.py $(GRAPH)
	python3 netlistVerilog.py $(GRAPH) --out $@ --module-name netlistVerilog

obj_dir_netlist/VnetlistVerilog: netlistVerilog.v $(MODELS) tb_netlist.cpp verilatorTB.h
	verilator $(VFLAGS) --trace -cc netlistVerilog.v $(MODELS) \
	    --top-module netlistVerilog --exe ../tb_netlist.cpp \
	    --Mdir obj_dir_netlist -CFLAGS "$(CFLAGS)"
	$(MAKE) -C obj_dir_netlist -f VnetlistVerilog.mk VnetlistVerilog

netlist: obj_dir_netlist/VnetlistVerilog
	@echo "## REPLAY tb_netlist ##"
	@./obj_dir_netlist/VnetlistVerilog

lint-netlist: netlistVerilog.v $(MODELS)
	verilator --lint-only $(VFLAGS) --top-module netlistVerilog netlistVerilog.v $(MODELS)

# ------------------------------------------------------------------------- clean
# stimulus.h is the pre-inlining separate header; swept away if one is lying around.
clean:
	rm -rf obj_dir_cones obj_dir_netlist obj_dir_try ConesVerilog.v netlistVerilog.v \
	    stimulus.h stimulus.json \
	    waveform_cones.vcd waveform_netlist.vcd waveform_try.vcd
