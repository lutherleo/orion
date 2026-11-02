**bountybench** (dev (pre-cutoff benchmark, proposed keys, small N); keys proposed; run 1)

| Arm | scored | missing | L1 | L2 | L2 (keys not flagged) | findings | tokens | CLI $ | $ per L2 | wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| plain-sonnet5 | 5 | 0 | 2 | 1 | 1 | 23 | 9747888 | 4.89 | 4.89 | 1811.2 |
| plain-opus5 | 5 | 0 | 4 | 3 | 3 | 37 | 21725429 | 21.75 | 7.25 | 2989.0 |
| orion-sonnet5 | 5 | 0 | 3 | 1 | 1 | 29 | 26146001 | 20.04 | 20.04 | 3860.3 |
| orion-sonnet5:leads | 5 | 0 | 4 | 1 | 1 | 36 | (same run) | (same run) | - | - |
| orion-opus5 | 5 | 0 | 4 | 3 | 3 | 50 | 42265533 | 93.09 | 31.03 | 6149.3 |
| orion-opus5:leads | 5 | 0 | 4 | 3 | 3 | 99 | (same run) | (same run) | - | - |

CLI $ is the cost `claude -p` reports (notional on a subscription login). `:leads` rows score the same Orion run before its verifier. Missing = no ok run on the vulnerable snapshot (excluded, not counted as a miss).
