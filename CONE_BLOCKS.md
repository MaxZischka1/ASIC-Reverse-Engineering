# Blocks

Two domains, then a kind per block. Domain is a scored decision (see coneBlocks.py); the evidence for every cone is in CONE_BLOCKS.json. The fanout cut is coneSignals' derived control floor (56), not a literal.

## Domains

- **data**: 77 cones
- **control**: 24 cones

## Blocks

| id | domain | kind | width | shared control | private/member | members |
|---|---|---|---|---|---|---|
| B00 | data | shifter | 12 | enable, gctl_4182 | 2 | u4993.D, u5000.D, u5004.D, u5093.D, u5336.D … |
| B01 | data | matcher | 11 | I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662 | 2 | u2299.D, u2301.D, u2710.D, u8084.D, u8085.D … |
| B02 | data | register | 11 | I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662 | 2 | u2542.D, u2574.D, u2708.D, u2709.D, u8092.D … |
| B03 | data | register | 9 | I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662 | 2 | u1228.D, u1230.D, u1258.D, u1259.D, u1260.D … |
| B04 | control | feedback | 9 | enable, gctl_4182, gctl_4659, gctl_4662, gctl_4838 | 0–5 | u4182.D, u4659.D, u4661.D, u4662.D, u4838.D … |
| B05 | data | decoder | 8 | gctl_226, gctl_227, gctl_228, gctl_234, gctl_235, gctl_236 | 1 | port:O[0], port:O[1], port:O[2], port:O[3], port:O[4] … |
| B06 | data | feedback | 8 | enable, gctl_226, gctl_234, gctl_235, gctl_236, gctl_237 | 4–7 | u2944.D, u2946.D, u2947.D, u2948.D, u2957.D … |
| B07 | data | matcher | 7 | I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662 | 2 | u1383.D, u1385.D, u1626.D, u1808.D, u1810.D … |
| B08 | data | matcher | 4 | I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662 | 2 | u1229.D, u1249.D, u1384.D, u2039.D |
| B09 | data | register | 4 | I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662 | 2 | u1250.D, u1261.D, u1809.D, u2037.D |
| B10 | control | clearable | 4 | gctl_226, gctl_234, gctl_235, gctl_236, gctl_237 | 0 | u234.D, u235.D, u236.D, u237.D |
| B11 | control | unknown | 2 | d_1228, d_1229, d_1230, d_1249, d_1250, d_1258 | 1 | u227.D, u228.D |
| B12 | data | buffer | 1 | gctl_228 | 0 | port:success |
| B13 | control | gate | 1 | gctl_226, gctl_4182 | 0 | u226.D |
| B14 | data | matcher | 1 | I, d_5000, d_5336, d_5337, d_5339, d_5344 | 0 | u5337.D |
| B15 | data | matcher | 1 | I, d_5634, d_5638, d_5639, enable, gctl_4182 | 0 | u5638.D |
| B16 | control | toggle | 1 | I, enable, gctl_4182, gctl_6095, gctl_6106, gctl_6109 | 0 | u6095.D |
| B17 | control | toggle | 1 | I, enable, gctl_4182, gctl_6106, gctl_6109, gctl_6111 | 0 | u6106.D |
| B18 | control | toggle | 1 | I, enable, gctl_4182, gctl_6109, gctl_6111 | 0 | u6109.D |
| B19 | control | toggle | 1 | I, enable, gctl_4182, gctl_6111 | 0 | u6111.D |
| B20 | control | toggle | 1 | I, enable, gctl_4182, gctl_6095, gctl_6106, gctl_6109 | 0 | u6159.D |
| B21 | control | toggle | 1 | I, enable, gctl_4182, gctl_6095, gctl_6106, gctl_6109 | 0 | u6197.D |
| B22 | control | toggle | 1 | I, enable, gctl_4182, gctl_6095, gctl_6106, gctl_6109 | 0 | u6198.D |
| B23 | control | toggle | 1 | I, enable, gctl_4182, gctl_6095, gctl_6106, gctl_6109 | 0 | u6199.D |

## Kinds

- **toggle**: 8 block(s), 8 cones
- **matcher**: 5 block(s), 24 cones
- **register**: 3 block(s), 24 cones
- **feedback**: 2 block(s), 17 cones
- **shifter**: 1 block(s), 12 cones
- **decoder**: 1 block(s), 8 cones
- **clearable**: 1 block(s), 4 cones
- **unknown**: 1 block(s), 2 cones
- **buffer**: 1 block(s), 1 cones
- **gate**: 1 block(s), 1 cones

## Detail

### B00 — shifter (data, width 12)

- bank: `port_register|4|2|True|load_enable`
- why this kind: members form a path of 11 links
- shared control: enable, gctl_4182
- label families: load_enable
- members: u4993.D, u5000.D, u5004.D, u5093.D, u5336.D, u5339.D, u5342.D, u5343.D, u5344.D, u5399.D, u5401.D, u5569.D

### B01 — matcher (data, width 11)

- bank: `port_register|13|14|True|set_dominant_sticky`
- why this kind: sticky set on an AND of literals
- shared control: I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662, gctl_4838, gctl_6435, gctl_6436, gctl_6442, gctl_6451
- label families: set_dominant_sticky
- members: u2299.D, u2301.D, u2710.D, u8084.D, u8085.D, u8086.D, u8090.D, u8091.D, u8429.D, u8431.D, u8591.D

### B02 — register (data, width 11)

- bank: `port_register|13|14|True|load_enable`
- why this kind: hold path present, data arrives from outside the block
- shared control: I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662, gctl_4838, gctl_6435, gctl_6436, gctl_6442, gctl_6451
- label families: load_enable
- members: u2542.D, u2574.D, u2708.D, u2709.D, u8092.D, u8095.D, u8199.D, u8214.D, u8219.D, u8602.D, u8815.D

### B03 — register (data, width 9)

- bank: `port_register|9|3|True|load_enable`
- why this kind: hold path present, data arrives from outside the block; 2 intra-block links, not a clean path
- shared control: I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662, gctl_4838
- label families: load_enable
- members: u1228.D, u1230.D, u1258.D, u1259.D, u1260.D, u1264.D, u2293.D, u5634.D, u5639.D

### B04 — feedback (control, width 9)

- bank: `scc2`
- why this kind: 9 cones in one strongly-connected component; mutually recursive state, not a chain
- shared control: enable, gctl_4182, gctl_4659, gctl_4662, gctl_4838
- label families: load_enable, set_dominant_sticky, toggle
- members: u4182.D, u4659.D, u4661.D, u4662.D, u4838.D, u6435.D, u6436.D, u6442.D, u6451.D

### B05 — decoder (data, width 8)

- bank: `output_driver|16|8|False|clear_dominant`
- why this kind: every member drives a primary output
- shared control: gctl_226, gctl_227, gctl_228, gctl_234, gctl_235, gctl_236, gctl_237, gctl_6095, gctl_6106, gctl_6109, gctl_6111, gctl_6159, gctl_6197, gctl_6198, gctl_6199
- label families: clear_dominant
- members: port:O[0], port:O[1], port:O[2], port:O[3], port:O[4], port:O[5], port:O[6], port:O[7]

### B06 — feedback (data, width 8)

- bank: `scc0`
- why this kind: 8 cones in one strongly-connected component; mutually recursive state, not a chain
- shared control: enable, gctl_226, gctl_234, gctl_235, gctl_236, gctl_237, gctl_2946, gctl_4182
- label families: other
- members: u2944.D, u2946.D, u2947.D, u2948.D, u2957.D, u2958.D, u2959.D, u2960.D

### B07 — matcher (data, width 7)

- bank: `port_register|9|3|True|set_dominant_sticky`
- why this kind: sticky set on an AND of literals
- shared control: I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662, gctl_4838
- label families: set_dominant_sticky
- members: u1383.D, u1385.D, u1626.D, u1808.D, u1810.D, u2036.D, u2372.D

### B08 — matcher (data, width 4)

- bank: `port_register|9|4|True|set_dominant_sticky`
- why this kind: sticky set on an AND of literals
- shared control: I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662, gctl_4838
- label families: set_dominant_sticky
- members: u1229.D, u1249.D, u1384.D, u2039.D

### B09 — register (data, width 4)

- bank: `port_register|9|4|True|load_enable`
- why this kind: hold path present, data arrives from outside the block
- shared control: I, enable, gctl_4182, gctl_4659, gctl_4661, gctl_4662, gctl_4838
- label families: load_enable
- members: u1250.D, u1261.D, u1809.D, u2037.D

### B10 — clearable (control, width 4)

- bank: `scc1`
- why this kind: dominant clear on a held value
- shared control: gctl_226, gctl_234, gctl_235, gctl_236, gctl_237
- label families: clear_dominant
- members: u234.D, u235.D, u236.D, u237.D

### B11 — unknown (control, width 2)

- bank: `internal_register|57|5|True|other`
- why this kind: no rule fired
- shared control: d_1228, d_1229, d_1230, d_1249, d_1250, d_1258, d_1259, d_1260, d_1261, d_1264, d_1383, d_1384, d_1385, d_1626, d_1808, d_1809, d_1810, d_2036, d_2037, d_2039, d_2293, d_2299, d_2301, d_2372, d_2542, d_2574, d_2708, d_2709, d_2710, d_5337, d_5638, d_8084, d_8085, d_8086, d_8090, d_8091, d_8092, d_8095, d_8199, d_8214, d_8219, d_8429, d_8431, d_8591, d_8602, d_8815, gctl_226, gctl_4182, gctl_6095, gctl_6106, gctl_6109, gctl_6111, gctl_6159, gctl_6197, gctl_6198, gctl_6199
- label families: other
- members: u227.D, u228.D
