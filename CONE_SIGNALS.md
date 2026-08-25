# Cone signals

Signals are leaf nets seen from the consumer side. Roles come from set relations against the banks in CONE_PROFILE.json, not from thresholds. See coneSignals.py for definitions.

## Roles

- data: 58
- shared_control: 27
- local: 9

## Control signals

Nets that cover at least one whole bank. `exclusive` means the net is read by that bank and nothing else -- a dedicated control line.

| name | net | source | role | fanout | banks covered | exclusive | groups | stable | select |
|---|---|---|---|---|---|---|---|---|---|
| gctl_4182 | 6443 | 4182:sky130_fd_sc_hd__dfrtp_2 | shared_control | 88 | 9 | 0 | 57 | NO | yes |
| enable | 7167 | enable | shared_control | 85 | 9 | 0 | 56 | NO |  |
| I | 6485 | I | shared_control | 58 | 6 | 0 | 44 | NO |  |
| gctl_4662 | 3015 | 4662:sky130_fd_sc_hd__dfrtp_2 | shared_control | 57 | 7 | 0 | 43 | NO | yes |
| gctl_4838 | 6110 | 4838:sky130_fd_sc_hd__dfrtp_2 | shared_control | 57 | 7 | 0 | 43 | NO | yes |
| gctl_4659 | 6705 | 4659:sky130_fd_sc_hd__dfrtp_2 | shared_control | 57 | 7 | 0 | 43 | NO |  |
| gctl_4661 | 6592 | 4661:sky130_fd_sc_hd__dfrtp_2 | shared_control | 56 | 6 | 0 | 42 | NO |  |
| gctl_6451 | 4764 | 6451:sky130_fd_sc_hd__dfrtp_2 | shared_control | 27 | 2 | 0 | 27 | yes | yes |
| gctl_6442 | 8149 | 6442:sky130_fd_sc_hd__dfrtp_2 | shared_control | 27 | 2 | 0 | 27 | yes |  |
| gctl_6436 | 8220 | 6436:sky130_fd_sc_hd__dfrtp_2 | shared_control | 27 | 2 | 0 | 27 | yes |  |
| gctl_6435 | 8184 | 6435:sky130_fd_sc_hd__dfrtp_2 | shared_control | 26 | 2 | 0 | 26 | yes |  |
| gctl_226 | 6254 | 226:sky130_fd_sc_hd__dfrtp_2 | shared_control | 23 | 3 | 0 | 18 | yes |  |
| gctl_237 | 6197 | 237:sky130_fd_sc_hd__dfxtp_2 | shared_control | 20 | 3 | 0 | 17 | yes |  |
| gctl_234 | 6245 | 234:sky130_fd_sc_hd__dfxtp_2 | shared_control | 20 | 3 | 0 | 17 | yes |  |
| gctl_235 | 6335 | 235:sky130_fd_sc_hd__dfxtp_2 | shared_control | 20 | 3 | 0 | 17 | yes |  |
| gctl_236 | 7691 | 236:sky130_fd_sc_hd__dfxtp_2 | shared_control | 20 | 3 | 0 | 17 | yes |  |
| gctl_6111 | 7458 | 6111:sky130_fd_sc_hd__dfrtp_2 | shared_control | 18 | 1 | 0 | 16 | yes |  |
| gctl_6109 | 7501 | 6109:sky130_fd_sc_hd__dfrtp_2 | shared_control | 17 | 1 | 0 | 15 | yes |  |
| gctl_6106 | 7509 | 6106:sky130_fd_sc_hd__dfrtp_2 | shared_control | 16 | 1 | 0 | 14 | yes |  |
| gctl_6095 | 7520 | 6095:sky130_fd_sc_hd__dfrtp_2 | shared_control | 15 | 1 | 0 | 13 | yes |  |
| gctl_6198 | 7522 | 6198:sky130_fd_sc_hd__dfrtp_2 | shared_control | 14 | 1 | 0 | 12 | yes |  |
| gctl_6197 | 7451 | 6197:sky130_fd_sc_hd__dfrtp_2 | shared_control | 13 | 1 | 0 | 11 | yes |  |
| gctl_6199 | 7532 | 6199:sky130_fd_sc_hd__dfrtp_2 | shared_control | 12 | 1 | 0 | 10 | yes |  |
| gctl_6159 | 7535 | 6159:sky130_fd_sc_hd__dfrtp_2 | shared_control | 11 | 1 | 0 | 9 | yes |  |
| gctl_228 | 6398 | 228:sky130_fd_sc_hd__dfrtp_2 | shared_control | 10 | 1 | 0 | 9 | yes |  |
| gctl_227 | 6239 | 227:sky130_fd_sc_hd__dfrtp_2 | shared_control | 9 | 1 | 0 | 8 | yes |  |
| gctl_2946 | 6790 | 2946:sky130_fd_sc_hd__dfrtp_2 | shared_control | 9 | 1 | 0 | 6 | NO |  |

## Broadcast signals

| name | source | fanout | banks spanned | groups | stable |
|---|---|---|---|---|---|

## Banks: shared versus private inputs

### `port_register|4|2|True|load_enable` — 12 members

- members: u4993.D, u5000.D, u5004.D, u5093.D, u5336.D, u5339.D, u5342.D, u5343.D, u5344.D, u5399.D, u5401.D, u5569.D
- **2 leaves common to every member** (15 in union)
- shared: gctl_4182, enable

- private per member: 2 leaf(s)

### `port_register|13|14|True|set_dominant_sticky` — 11 members

- members: u2299.D, u2301.D, u2710.D, u8084.D, u8085.D, u8086.D, u8090.D, u8091.D, u8429.D, u8431.D, u8591.D
- **11 leaves common to every member** (33 in union)
- shared: gctl_4662, gctl_6451, gctl_4838, gctl_4182, I, gctl_4661, gctl_4659, enable, gctl_6442, gctl_6435, gctl_6436

- private per member: 2 leaf(s)

### `port_register|13|14|True|load_enable` — 11 members

- members: u2542.D, u2574.D, u2708.D, u2709.D, u8092.D, u8095.D, u8199.D, u8214.D, u8219.D, u8602.D, u8815.D
- **11 leaves common to every member** (33 in union)
- shared: gctl_4662, gctl_6451, gctl_4838, gctl_4182, I, gctl_4661, gctl_4659, enable, gctl_6442, gctl_6435, gctl_6436

- private per member: 2 leaf(s)

### `port_register|9|3|True|load_enable` — 9 members

- members: u1228.D, u1230.D, u1258.D, u1259.D, u1260.D, u1264.D, u2293.D, u5634.D, u5639.D
- **7 leaves common to every member** (23 in union)
- shared: gctl_4662, gctl_4838, gctl_4182, I, gctl_4661, gctl_4659, enable

- private per member: 2 leaf(s)

### `scc2` — 9 members

- members: u4182.D, u4659.D, u4661.D, u4662.D, u4838.D, u6435.D, u6436.D, u6442.D, u6451.D
- **5 leaves common to every member** (10 in union)
- shared: gctl_4662, gctl_4838, gctl_4182, gctl_4659, enable

- private per member: 0–5 leaf(s)

### `output_driver|16|8|False|clear_dominant` — 8 members

- members: port:O[0], port:O[1], port:O[2], port:O[3], port:O[4], port:O[5], port:O[6], port:O[7]
- **15 leaves common to every member** (23 in union)
- shared: gctl_237, gctl_227, gctl_234, gctl_226, gctl_235, gctl_228, gctl_6197, gctl_6111, gctl_6109, gctl_6106, gctl_6095, gctl_6198, gctl_6199, gctl_6159, gctl_236

- private per member: 1 leaf(s)

### `scc0` — 8 members

- members: u2944.D, u2946.D, u2947.D, u2948.D, u2957.D, u2958.D, u2959.D, u2960.D
- **8 leaves common to every member** (16 in union)
- shared: gctl_237, gctl_234, gctl_226, gctl_235, gctl_4182, gctl_2946, enable, gctl_236

- private per member: 4–7 leaf(s)

### `port_register|9|3|True|set_dominant_sticky` — 7 members

- members: u1383.D, u1385.D, u1626.D, u1808.D, u1810.D, u2036.D, u2372.D
- **7 leaves common to every member** (21 in union)
- shared: gctl_4662, gctl_4838, gctl_4182, I, gctl_4661, gctl_4659, enable

- private per member: 2 leaf(s)

### `port_register|9|4|True|set_dominant_sticky` — 4 members

- members: u1229.D, u1249.D, u1384.D, u2039.D
- **7 leaves common to every member** (15 in union)
- shared: gctl_4662, gctl_4838, gctl_4182, I, gctl_4661, gctl_4659, enable

- private per member: 2 leaf(s)

### `port_register|9|4|True|load_enable` — 4 members

- members: u1250.D, u1261.D, u1809.D, u2037.D
- **7 leaves common to every member** (15 in union)
- shared: gctl_4662, gctl_4838, gctl_4182, I, gctl_4661, gctl_4659, enable

- private per member: 2 leaf(s)

### `scc1` — 4 members

- members: u234.D, u235.D, u236.D, u237.D
- **5 leaves common to every member** (5 in union)
- shared: gctl_237, gctl_234, gctl_226, gctl_235, gctl_236

- private per member: 0 leaf(s)

## Cone correlation (Jaccard ≥ 0.6)

819 pairs, 6 clusters.

- **56 cones**: u1228.D, u1229.D, u1230.D, u1249.D, u1250.D, u1258.D, u1259.D, u1260.D, u1261.D, u1264.D, u1383.D, u1384.D, u1385.D, u1626.D ...
- **20 cones**: u4993.D, u5000.D, u5004.D, u5093.D, u5336.D, u5339.D, u5342.D, u5343.D, u5344.D, u5399.D, u5401.D, u5569.D, u6095.D, u6106.D ...
- **8 cones**: port:O[0], port:O[1], port:O[2], port:O[3], port:O[4], port:O[5], port:O[6], port:O[7]
- **8 cones**: u2944.D, u2946.D, u2947.D, u2948.D, u2957.D, u2958.D, u2959.D, u2960.D
- **4 cones**: u234.D, u235.D, u236.D, u237.D
- **2 cones**: u227.D, u228.D

### Strongest pairs

| jaccard | a | b | shared leaves |
|---|---|---|---|
| 1.0 | u8591.D | u8602.D | 13 |
| 1.0 | u8095.D | u8429.D | 13 |
| 1.0 | u8092.D | u8431.D | 13 |
| 1.0 | u8090.D | u8214.D | 13 |
| 1.0 | u8086.D | u8219.D | 13 |
| 1.0 | u8085.D | u8199.D | 13 |
| 1.0 | u8084.D | u8815.D | 13 |
| 1.0 | u6442.D | u6451.D | 10 |
| 1.0 | u6435.D | u6451.D | 10 |
| 1.0 | u6435.D | u6442.D | 10 |
| 1.0 | u5634.D | u5639.D | 9 |
| 1.0 | u4662.D | u4838.D | 6 |
| 1.0 | u4661.D | u4838.D | 6 |
| 1.0 | u4661.D | u4662.D | 6 |
| 1.0 | u4182.D | u6451.D | 10 |
| 1.0 | u4182.D | u6442.D | 10 |
| 1.0 | u4182.D | u6435.D | 10 |
| 1.0 | u2709.D | u8091.D | 13 |
| 1.0 | u2708.D | u2710.D | 13 |
| 1.0 | u236.D | u237.D | 5 |
| 1.0 | u235.D | u237.D | 5 |
| 1.0 | u235.D | u236.D | 5 |
| 1.0 | u234.D | u237.D | 5 |
| 1.0 | u234.D | u236.D | 5 |
| 1.0 | u234.D | u235.D | 5 |
