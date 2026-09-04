# Approach to solution

For this puzzle I used a combination of two analysis to solve the puzzle. By solving the puzzle I mean finding the correct 121 bit signal required but ALSO understanding how the circuit functions at a logic gate level. First I will go through the GDS parsing (link to topic on that) then the circuit analysis I did from mostly step 5 of the GDS parser,under the Cones, then finally using Z3 to understand the data path combinational blocks aswell as looking into what 121 bit signal sets the success signal high. IMPORTANT Note: I want this writeup to focus much more on the circuit analysis and less on the scripting as although it was what provided the solution that involved using external complex computing algorithms and a lot of Claude Code. The scripts helped me make some jumps in the circuit analysis that would have taken significantly longer if I was only doing the circuit analysis.

## GDS Parsing

From a broad stand point the GDS parsing was a 5 stage process starting from the .gds file to a formatted GDS file to a graph of all cells to a graph of logical cells connected(netlist) to a graph of nodes of combinational blocks and edges being D-flip flops. For this task I used a lot of vibe coding but kept this pipeline as the underlying idea.

 (Talk more about how each of the scripting files works.)
### Python Scripts from GDS to winning solution
#### [GDS file to netlist graph](src/klayoutNetlist.py)
The first file to look into is klayoutNetlist. This really does the first 3 steps of the pipeline described above taking a gds file as an input and a netlist as an output. To simplify this step as much as possible I asked Claude to also use the Klayout tool for this task. What this provided was some readable file in return that the python file organizes into formatted information. The information important are instances of cells, this is a graph of sky130 logic cells as nodes and connections between each as edges. So this step allowed us to take a GDS file which does not have a user readable format and turn it into a data structure that is very easy to walk through and understand for later algorithms created.

#### netlist to combinational blocks
So now there is a Netlist Graph, the issue with a netlist graph is the size and organization; the amount of cells is too large to say draw out and if you tried making specific larger blocks from this step you would have to think of some algorithm that says there is a limit to how many logic cells I will search around me before I am just making the entire circuit in this clock. Instead for organization we can just seperate the chips design into every combinational block between two flip flops. What this gives us is an organized set of every computational step in this circuit AND a very definitive approach of achieving this organized set, all you are doing is walking some graph to see all cells between a data input pin of a flip-flop to the output of another flip-flop or a direct input of circuit. For this step I used [coneDecompose.py](src/coneDecompose.py) which as stated before just starts walking back from every sequential element to the FF outputs and chip inputs to provide the fan-in of the cone.

#### Json files to Verilog
With two data structures containing the recovered gds file the next step is turning these into verilog files to test if the algorithms Claude created actually worked. This is when [coneToVerilog.py](bench/conesToVerilog.py) and [netlistVerilog.py](bench/netlistVerilog.py) are used. All it is doing is parsing a json file and does it in two main functions cleaning the json file into only specific information the parser needs and then declaring ports which provided the name and direction of each port.


 Now that I had a netlist and cones file to verify functionality I made tb_cones.cpp and tb_netlist.cpp. I used a semi-UVM type of testbench made in C++ to test functionality. The hardest part of the testbenches was finding an effecient way to get the waves data from example inputs into the C++ file or just into some formatt I could adjust easily to test, I had Claude create vcd parsing files and then at some point just had it put the characters directly in cpp file so I could easly adjust. After some verification from the testbenches we now had working verilog files to do the circuit analysis.

## Circuit Analysis

For the circuit analysis I started by looking into the most complex cone. From looking at the verilog file CONE98 is the one to look into. It drives the success output which is the function of circuit along with the output drivers. From here I drew out this cone as you can see below. When looking into this cone I started by examing the cones based on how much of an influence they have on driving success. 

![Alt text](photosReadME/CONE98.jpg)

### Control 

### Bit counting

### Bit Checking with Control

### Output buffers

## Using Z3 for larger blocks

### symSolve.py
### decomposeCone.py

## Final Thoughts

## Usage of AI