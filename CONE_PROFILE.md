# Cone profiles

101 cones. Attributes are measured; classes are derived from them. See coneProfile.py for what each column means.

## Structural classes (`class_shape`)

61 classes over 101 cones; 10 have more than one member.

- `ea5d264e30a5` x12: u4993.D, u5000.D, u5004.D, u5093.D, u5336.D, u5339.D, u5342.D, u5343.D, u5344.D, u5399.D ...
- `04b9ff331b85` x5: u1383.D, u1385.D, u1808.D, u1810.D, u2036.D
- `63f5839b89a4` x5: u1228.D, u1230.D, u1259.D, u1260.D, u1264.D
- `993c3c28200b` x5: u2542.D, u2574.D, u8095.D, u8214.D, u8219.D
- `aad25464ce04` x5: u2299.D, u2301.D, u8086.D, u8090.D, u8429.D
- `0e9e6480b14d` x4: u1250.D, u1261.D, u1809.D, u2037.D
- `623b9d8b7fbb` x4: u2710.D, u8085.D, u8431.D, u8591.D
- `e9f5be275baf` x4: u2708.D, u8092.D, u8199.D, u8602.D
- `efc65fc94ea5` x4: u1229.D, u1249.D, u1384.D, u2039.D
- `9dbfa5f81c3a` x2: u227.D, u228.D

## Exact function classes (`class_exact`)

72 classes over 101 cones; 7 have more than one member.

- `691e98dfca4da857b6cc0cd6131e6cef` x12: u4993.D, u5000.D, u5004.D, u5093.D, u5336.D, u5339.D, u5342.D, u5343.D, u5344.D, u5399.D ...
- `5541d10a1bc506d79ede2f33fa154907` x5: u1228.D, u1230.D, u1259.D, u1260.D, u1264.D
- `91856b5312a627807519f2dd3406b6ca` x5: u1383.D, u1385.D, u1808.D, u1810.D, u2036.D
- `1318eb8dc47772d9a990c3ce4f15392f` x4: u1229.D, u1249.D, u1384.D, u2039.D
- `89dc13b92bcb870b6f21848c82bea79a` x4: u1250.D, u1261.D, u1809.D, u2037.D
- `b939c51979bd62ad6f0e2ef8d65744b2` x4: u2947.D, u2948.D, u2958.D, u2960.D
- `fcc49afa6f15c7eb8a092a33c8fd92a3` x2: u4659.D, u6109.D

## Profile classes (`class_profile`)

44 classes over 101 cones; 15 have more than one member.

- `port_register|4|2|2|1` x12: u4993.D, u5000.D, u5004.D, u5093.D, u5336.D, u5339.D, u5342.D, u5343.D, u5344.D, u5399.D ...
- `port_register|13|150|14|1` x11: u2542.D, u2574.D, u2709.D, u2710.D, u8085.D, u8095.D, u8214.D, u8219.D, u8431.D, u8591.D ...
- `port_register|9|4|3|1` x8: u1383.D, u1385.D, u1626.D, u1808.D, u1810.D, u2036.D, u2372.D, u6436.D
- `port_register|9|5|3|1` x8: u1228.D, u1230.D, u1258.D, u1259.D, u1260.D, u1264.D, u2293.D, u5634.D
- `port_register|13|149|14|1` x7: u2299.D, u2301.D, u8084.D, u8086.D, u8090.D, u8091.D, u8429.D
- `port_register|13|151|14|1` x4: u2708.D, u8092.D, u8199.D, u8602.D
- `port_register|9|5|4|1` x4: u1229.D, u1249.D, u1384.D, u2039.D
- `port_register|9|6|4|1` x4: u1250.D, u1261.D, u1809.D, u2037.D
- `internal_register|57|37|5|1` x2: u227.D, u228.D
- `internal_register|5|2|2|1` x2: u235.D, u237.D
- `internal_register|5|3|2|1` x2: u234.D, u236.D
- `output_driver|16|42|8|0` x2: port:O[2], port:O[4]
- `port_register|10|6|3|1` x2: u5638.D, u6442.D
- `port_register|12|10|5|1` x2: u2958.D, u2960.D
- `port_register|6|5|3|1` x2: u4661.D, u4838.D

## Block classes (`class_block`)

24 classes over 101 cones; 12 have more than one member.

- `port_register|4|2|True|load_enable` x12: u4993.D, u5000.D, u5004.D, u5093.D, u5336.D, u5339.D, u5342.D, u5343.D, u5344.D, u5399.D ...
- `port_register|13|14|True|load_enable` x11: u2542.D, u2574.D, u2708.D, u2709.D, u8092.D, u8095.D, u8199.D, u8214.D, u8219.D, u8602.D ...
- `port_register|13|14|True|set_dominant_sticky` x11: u2299.D, u2301.D, u2710.D, u8084.D, u8085.D, u8086.D, u8090.D, u8091.D, u8429.D, u8431.D ...
- `port_register|9|3|True|load_enable` x9: u1228.D, u1230.D, u1258.D, u1259.D, u1260.D, u1264.D, u2293.D, u5634.D, u5639.D
- `scc2` x9: u4182.D, u4659.D, u4661.D, u4662.D, u4838.D, u6435.D, u6436.D, u6442.D, u6451.D
- `output_driver|16|8|False|clear_dominant` x8: port:O[0], port:O[1], port:O[2], port:O[3], port:O[4], port:O[5], port:O[6], port:O[7]
- `scc0` x8: u2944.D, u2946.D, u2947.D, u2948.D, u2957.D, u2958.D, u2959.D, u2960.D
- `port_register|9|3|True|set_dominant_sticky` x7: u1383.D, u1385.D, u1626.D, u1808.D, u1810.D, u2036.D, u2372.D
- `port_register|9|4|True|load_enable` x4: u1250.D, u1261.D, u1809.D, u2037.D
- `port_register|9|4|True|set_dominant_sticky` x4: u1229.D, u1249.D, u1384.D, u2039.D
- `scc1` x4: u234.D, u235.D, u236.D, u237.D
- `internal_register|57|5|True|other` x2: u227.D, u228.D

## Coarse classes (`class_coarse`)

3 classes over 101 cones; 3 have more than one member.

- `port_register|1|0` x85: u1228.D, u1229.D, u1230.D, u1249.D, u1250.D, u1258.D, u1259.D, u1260.D, u1261.D, u1264.D ...
- `output_driver|0|1` x9: port:O[0], port:O[1], port:O[2], port:O[3], port:O[4], port:O[5], port:O[6], port:O[7], port:success
- `internal_register|1|0` x7: u226.D, u227.D, u228.D, u234.D, u235.D, u236.D, u237.D

## Roles

- port_register: 85
- output_driver: 9
- internal_register: 7

## Per-cone table

| id | role | fan_in | num_gates | depth | q_fanout | peer_fraction | self_feedback | dist_from_port | dist_to_output | balance | label |
|---|---|---|---|---|---|---|---|---|---|---|---|
| u2708.D | port_register | 13 | 151 | 14 | 4 | 0.25 | True | 0 | 2 | 0.504395 | load_enable(sel=~a, hold=m) |
| u8092.D | port_register | 13 | 151 | 14 | 4 | 0.25 | True | 0 | 2 | 0.501099 | load_enable(sel=~a, hold=m) |
| u8199.D | port_register | 13 | 151 | 14 | 4 | 0.25 | True | 0 | 2 | 0.50061 | load_enable(sel=~a, hold=m) |
| u8602.D | port_register | 13 | 151 | 14 | 4 | 0.25 | True | 0 | 2 | 0.501831 | load_enable(sel=~a, hold=m) |
| u2542.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.503662 | load_enable(sel=~a, hold=m) |
| u2574.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.500732 | load_enable(sel=~a, hold=m) |
| u2709.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=m) |
| u2710.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.504395 | set_dominant_sticky(q=m) |
| u8085.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.50061 | set_dominant_sticky(q=m) |
| u8095.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.500977 | load_enable(sel=~a, hold=m) |
| u8214.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.500854 | load_enable(sel=~a, hold=m) |
| u8219.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.502808 | load_enable(sel=~a, hold=m) |
| u8431.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.501099 | set_dominant_sticky(q=m) |
| u8591.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.501831 | set_dominant_sticky(q=m) |
| u8815.D | port_register | 13 | 150 | 14 | 4 | 0.25 | True | 0 | 2 | 0.512329 | load_enable(sel=~a, hold=m) |
| u2299.D | port_register | 13 | 149 | 14 | 4 | 0.25 | True | 0 | 2 | 0.503662 | set_dominant_sticky(q=m) |
| u2301.D | port_register | 13 | 149 | 14 | 4 | 0.25 | True | 0 | 2 | 0.500732 | set_dominant_sticky(q=m) |
| u8084.D | port_register | 13 | 149 | 14 | 4 | 0.25 | True | 0 | 2 | 0.512329 | set_dominant_sticky(q=m) |
| u8086.D | port_register | 13 | 149 | 14 | 4 | 0.25 | True | 0 | 2 | 0.502808 | set_dominant_sticky(q=m) |
| u8090.D | port_register | 13 | 149 | 14 | 4 | 0.25 | True | 0 | 2 | 0.500854 | set_dominant_sticky(q=m) |
| u8091.D | port_register | 13 | 149 | 14 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=m) |
| u8429.D | port_register | 13 | 149 | 14 | 4 | 0.25 | True | 0 | 2 | 0.500977 | set_dominant_sticky(q=m) |
| port:O[3] | output_driver | 16 | 55 | 8 | 0 |  | False | 1 | 0 | 0.179016 | clear_dominant(q=p) |
| port:O[1] | output_driver | 16 | 47 | 8 | 0 |  | False | 1 | 0 | 0.18689 | clear_dominant(q=p) |
| port:O[0] | output_driver | 16 | 46 | 8 | 0 |  | False | 1 | 0 | 0.202759 | clear_dominant(q=p) |
| port:O[2] | output_driver | 16 | 42 | 8 | 0 |  | False | 1 | 0 | 0.210022 | clear_dominant(q=p) |
| port:O[4] | output_driver | 16 | 42 | 8 | 0 |  | False | 1 | 0 | 0.178894 | clear_dominant(q=p) |
| port:O[6] | output_driver | 16 | 41 | 8 | 0 |  | False | 1 | 0 | 0.265381 | clear_dominant(q=p) |
| port:O[5] | output_driver | 16 | 39 | 8 | 0 |  | False | 1 | 0 | 0.139771 | clear_dominant(q=p) |
| u227.D | internal_register | 57 | 37 | 5 | 9 | 0.111 | True | 1 | 1 |  | structural_only_k57_21b6eb52 |
| u228.D | internal_register | 57 | 37 | 5 | 10 | 0.1 | True | 1 | 1 |  | structural_only_k57_eff9715a |
| port:O[7] | output_driver | 16 | 28 | 8 | 0 |  | False | 1 | 0 | 0.116272 | clear_dominant(q=o) |
| u2957.D | port_register | 15 | 15 | 5 | 7 | 0.143 | True | 0 | 1 | 0.5 | unknown_k15_73f028c0 |
| u2946.D | port_register | 14 | 14 | 5 | 9 | 0.111 | True | 0 | 1 | 0.5 | unknown_k14_2a7841a5 |
| u2959.D | port_register | 15 | 13 | 5 | 6 | 0.167 | True | 0 | 1 | 0.5 | unknown_k15_f89573af |
| u2947.D | port_register | 12 | 11 | 5 | 6 | 0.167 | True | 0 | 1 | 0.5 | unknown_k12_b939c519 |
| u2948.D | port_register | 12 | 10 | 6 | 8 | 0.125 | True | 0 | 1 | 0.5 | unknown_k12_b939c519 |
| u2958.D | port_register | 12 | 10 | 5 | 7 | 0.143 | True | 0 | 1 | 0.5 | unknown_k12_b939c519 |
| u2960.D | port_register | 12 | 10 | 5 | 6 | 0.167 | True | 0 | 1 | 0.5 | unknown_k12_b939c519 |
| u2944.D | port_register | 12 | 9 | 5 | 6 | 0.167 | True | 0 | 1 | 0.5 | unknown_k12_3f61679a |
| u6435.D | port_register | 10 | 8 | 4 | 26 | 0.038 | True | 0 | 3 | 0.499023 | load_enable(sel=a, hold=j) |
| u6197.D | port_register | 9 | 7 | 4 | 13 | 0.077 | True | 0 | 1 | 0.5 | toggle(q=i) |
| u6199.D | port_register | 10 | 7 | 4 | 12 | 0.083 | True | 0 | 1 | 0.5 | toggle(q=j) |
| u6451.D | port_register | 10 | 7 | 3 | 27 | 0.037 | True | 0 | 3 | 0.499023 | load_enable(sel=a, hold=j) |
| u1250.D | port_register | 9 | 6 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1261.D | port_register | 9 | 6 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1809.D | port_register | 9 | 6 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u2037.D | port_register | 9 | 6 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u5337.D | port_register | 12 | 6 | 4 | 3 | 0.333 | True | 0 | 2 | 0.557617 | set_dominant_sticky(q=l) |
| u5638.D | port_register | 10 | 6 | 3 | 3 | 0.333 | True | 0 | 2 | 0.505859 | set_dominant_sticky(q=j) |
| u5639.D | port_register | 9 | 6 | 3 | 3 | 0.333 | True | 0 | 3 | 0.521484 | load_enable(sel=~a, hold=i) |
| u6095.D | port_register | 7 | 6 | 4 | 15 | 0.067 | True | 0 | 1 | 0.5 | toggle(q=g) |
| u6159.D | port_register | 11 | 6 | 4 | 11 | 0.091 | True | 0 | 1 | 0.5 | toggle(q=k) |
| u6442.D | port_register | 10 | 6 | 3 | 27 | 0.037 | True | 0 | 3 | 0.499023 | load_enable(sel=a, hold=j) |
| u1228.D | port_register | 9 | 5 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1229.D | port_register | 9 | 5 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u1230.D | port_register | 9 | 5 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1249.D | port_register | 9 | 5 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u1258.D | port_register | 9 | 5 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1259.D | port_register | 9 | 5 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1260.D | port_register | 9 | 5 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1264.D | port_register | 9 | 5 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u1384.D | port_register | 9 | 5 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u2039.D | port_register | 9 | 5 | 4 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u2293.D | port_register | 9 | 5 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | load_enable(sel=~a, hold=i) |
| u4182.D | port_register | 10 | 5 | 3 | 88 | 0.011 | True | 0 | 2 | 0.500977 | set_dominant_sticky(q=j) |
| u4661.D | port_register | 6 | 5 | 3 | 56 | 0.018 | True | 0 | 3 | 0.484375 | load_enable(sel=~a, hold=f) |
| u4838.D | port_register | 6 | 5 | 3 | 57 | 0.018 | True | 0 | 3 | 0.484375 | load_enable(sel=~a, hold=f) |
| u5634.D | port_register | 9 | 5 | 3 | 3 | 0.333 | True | 0 | 3 | 0.521484 | load_enable(sel=~a, hold=i) |
| u6106.D | port_register | 6 | 5 | 4 | 16 | 0.062 | True | 0 | 1 | 0.5 | toggle(q=f) |
| u6198.D | port_register | 8 | 5 | 4 | 14 | 0.071 | True | 0 | 1 | 0.5 | toggle(q=h) |
| u1383.D | port_register | 9 | 4 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u1385.D | port_register | 9 | 4 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u1626.D | port_register | 9 | 4 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u1808.D | port_register | 9 | 4 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u1810.D | port_register | 9 | 4 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u2036.D | port_register | 9 | 4 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u2372.D | port_register | 9 | 4 | 3 | 4 | 0.25 | True | 0 | 2 | 0.501953 | set_dominant_sticky(q=i) |
| u4662.D | port_register | 6 | 4 | 3 | 57 | 0.018 | True | 0 | 3 | 0.484375 | load_enable(sel=~a, hold=f) |
| u6109.D | port_register | 5 | 4 | 3 | 17 | 0.059 | True | 0 | 1 | 0.5 | toggle(q=e) |
| u6111.D | port_register | 4 | 4 | 3 | 18 | 0.056 | True | 0 | 1 | 0.5 | toggle(q=d) |
| u6436.D | port_register | 9 | 4 | 3 | 27 | 0.037 | True | 0 | 3 | 0.5 | toggle(q=i) |
| u234.D | internal_register | 5 | 3 | 2 | 20 | 0.05 | True | 2 | 1 | 0.28125 | clear_dominant(q=e) |
| u236.D | internal_register | 5 | 3 | 2 | 20 | 0.05 | True | 2 | 1 | 0.28125 | clear_dominant(q=e) |
| u4659.D | port_register | 5 | 3 | 3 | 57 | 0.018 | True | 0 | 3 | 0.5 | toggle(q=e) |
| u235.D | internal_register | 5 | 2 | 2 | 20 | 0.05 | True | 2 | 1 | 0.28125 | clear_dominant(q=e) |
| u237.D | internal_register | 5 | 2 | 2 | 20 | 0.05 | True | 2 | 1 | 0.28125 | clear_dominant(q=e) |
| u4993.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 6 | 0.5 | load_enable(sel=~a, hold=d) |
| u5000.D | port_register | 4 | 2 | 2 | 3 | 0.667 | True | 0 | 3 | 0.5 | load_enable(sel=~a, hold=d) |
| u5004.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 8 | 0.5 | load_enable(sel=~a, hold=d) |
| u5093.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 10 | 0.5 | load_enable(sel=~a, hold=d) |
| u5336.D | port_register | 4 | 2 | 2 | 2 | 0.5 | True | 0 | 3 | 0.5 | load_enable(sel=~a, hold=d) |
| u5339.D | port_register | 4 | 2 | 2 | 3 | 0.667 | True | 0 | 3 | 0.5 | load_enable(sel=~a, hold=d) |
| u5342.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 7 | 0.5 | load_enable(sel=~a, hold=d) |
| u5343.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 4 | 0.5 | load_enable(sel=~a, hold=d) |
| u5344.D | port_register | 4 | 2 | 2 | 3 | 0.667 | True | 0 | 3 | 0.5 | load_enable(sel=~a, hold=d) |
| u5399.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 9 | 0.5 | load_enable(sel=~a, hold=d) |
| u5401.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 11 | 0.5 | load_enable(sel=~a, hold=d) |
| u5569.D | port_register | 4 | 2 | 2 | 2 | 1.0 | True | 0 | 5 | 0.5 | load_enable(sel=~a, hold=d) |
| u226.D | internal_register | 2 | 1 | 1 | 23 | 0.043 | True | 1 | 1 | 0.75 | or2 |
| port:success | output_driver | 1 | 0 | 0 | 0 |  | False | 2 | 0 | 0.5 | buffer |
