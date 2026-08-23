# Solution

## Notes before reading: I followed the rules of the puzzle not having the puzzle files fed into the AI but used AI HEAVILY. I don't usually vibe code especially with hardware design but I didn't have much experience with GDS files and LVS parsing so I thought this would be a cool way to try a bit of vibecoding. 

## Approach: The puzzle gives you two clues to solve your problem the GDS file and the waveform. The waveform really only provided two clues for the solution: 

## Waveform hint/ Easter Egg
The number of clock cycles from enable being set to being disabled is 121 and the output is the string TRY AGAIN. At first I tried examining the values as 7 or 8 bit values and it stopped works LSB or MSB first by character 3. The repeated long 0 signal followed by a 1 with the space incouraged to still think this was an ASCII value. The 121 clock cycles came back when the AI was building the larger blocks of the circuit and created the 11 count sequencer, with 121 clock cycles the only way it could be ASCII values is if every bit is 11 I tried this and got ASCII values spelling out "The night s" and "ky awaits  ". To be honest this was the most problem I found with the puzzle and most after this was prompting the AI in the right direction to come up with a solution.

## GDS File Parsing