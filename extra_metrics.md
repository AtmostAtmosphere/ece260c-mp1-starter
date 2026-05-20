# MP1 non-PPA QoR summary

## Clustering effectiveness & clock network

| Design | Orig FFs | Post FFs | MBFFs | Merged % | MBFF masters | Empty pins | Clk buf (b->c) | Sinks (b->c) |
|---|---|---|---|---|---|---|---|---|
| gcd_v1 | 35 | 35 | 0 | 0.0% | - | 0 | 9->9 | 35->35 |
| ibex_v1 | 1937 | 774 | 485 | 85.1% | H2V2X_1:405,V2X_1:80 | 132 | 117->65 | 1939->776 |
| ibex_v2 | 1937 | 780 | 480 | 84.5% | H2V2X_1:412,V2X_1:68 | 147 | 116->60 | 1939->782 |
| jpeg_v1 | 4384 | 4342 | 18 | 1.4% | H2V2X_1:15,V2X_1:3 | 6 | 373->374 | 4384->4342 |
| jpeg_v2 | 4384 | 4342 | 19 | 1.4% | H2V2X_1:15,V2X_1:4 | 7 | 374->373 | 4384->4342 |
| riscv32i_v1 | 1056 | 1034 | 10 | 3.0% | H2V2X_1:7,V2X_1:3 | 2 | 78->77 | 1056->1034 |
| riscv32i_v2 | 1056 | 1033 | 9 | 3.0% | H2V2X_1:9 | 4 | 78->77 | 1056->1033 |

## Displacement & logical equivalence

| Design | Disp total um (b->c) | Disp max um | Disp mean um | Half-conn D/Q | Missing comb | Added comb | Equivalence |
|---|---|---|---|---|---|---|---|
| gcd_v1 | 1417->1417 | 22.45 | 3.4 | 0 | 0 | 0 | OK |
| ibex_v1 | 66055->76445 | 243.12 | 4.27 | 0 | 1 | 0 | **FLAG** |
| ibex_v2 | 72005->106366 | 245.99 | 5.85 | 0 | 1 | 0 | **FLAG** |
| jpeg_v1 | 171930->171982 | 12.49 | 2.22 | 0 | 0 | 0 | OK |
| jpeg_v2 | 172527->172709 | 14.09 | 2.24 | 0 | 0 | 0 | OK |
| riscv32i_v1 | 27044->27076 | 12.9 | 2.89 | 0 | 0 | 0 | OK |
| riscv32i_v2 | 28126->28331 | 16.16 | 3.04 | 0 | 0 | 0 | OK |

_Half-connected D/Q pairs and missing combinational cells must be 0 for a valid submission; added combinational cells are expected repair buffers._
