## Approach to solution

For this puzzle I used a combinational of two analysis to solve the puzzle. By solving the puzzle I mean finding the correct 121 bit signal required but ALSO understanding how the circuit functions at a logic gate level. First I will go through the GDS parsing (link to topic on that) then the circuit analysis I did from mostly step 5 of the GDS parser,under the Cones, then finally using Z3 to understand the data path combinational blocks aswell as looking into what 121 bit signal sets the success signal high.

# GDS Parsing

From a broad stand point the GDS parsing was a 5 stage process starting from the .gds file to a formatted GDS file to a graph of all cells to a graph of logical cells connected(netlist) to a graph of nodes of combinational blocks and edges being D-flip flops. For this task I used a lot of vibe coding but kept this pipeline as the underlying idea.

 (Talk more about how each of the scripting files works.)

With a json file of the netlist and cones created lastly I made netlistVerilog and conesToVerilog which is just python files walking a .json file which is a graph and creating a verilog file from it. Now that I had a netlist and cones file to verify functionality I made tb_cones.cpp and tb_netlist.cpp. I used a semi-UVM type of testbench made in C++ to test functionality. The hardest part of the testbenches was finding an effecient way to get the waves data from example inputs into the C++ file or just into some formatt I could adjust easily to test. After some verification from the testbenches we now had working verilog files to do the circuit analysis.

# Circuit Analysis

For the circuit analysis I started by looking into the most complex cone. From looking at the verilog file CONE98 is the one to look into. It drives the success output which is the function of circuit along with the output drivers. From here I drew out this cone as you can see below. When looking into this cone I stated by examing the cones based on how much of an influence they have on driving success.

## Control 

## Bit counting

## Bit Checking with Control

## Output buffers

# Using Z3 for larger blocks

## symSolve.py
## decomposeCone.py

# Final Thoughts

# Usage of AI