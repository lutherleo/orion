**vlb** (dev (pre-cutoff benchmark, proposed keys, small N); keys files: VLB's own ground truth; fix sites: proposed (derived by eval.vlb, unsigned); run 1)

| Arm | scored | missing | L1 | L2 | mean File F1 (CWE) | silent on patched | pair-correct | tokens | CLI $ | $ per L2 | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| plain-sonnet5 | 5 | 0 | 3 | 3 | 0.533 | 5 / 5 | 3 / 5 | 9784921 | 6.40 | 2.13 | 2360.7 |
| plain-opus5 | 5 | 0 | 3 | 3 | 0.533 | 5 / 5 | 3 / 5 | 17537831 | 25.19 | 8.40 | 4012.6 |
| orion-sonnet5 | 5 | 0 | 3 | 2 | 0.333 | 5 / 5 | 3 / 5 | 39889589 | 32.58 | 16.29 | 5524.0 |
| orion-sonnet5:leads | 5 | 0 | 3 | 2 | 0.333 | 5 / 5 | 3 / 5 | (same run) | (same run) | - | - |
| orion-opus5 | 5 | 0 | 4 | 3 | 0.533 | 5 / 5 | 4 / 5 | 68414280 | 153.77 | 51.26 | 10262.9 |
| orion-opus5:leads | 5 | 0 | 3 | 3 | 0.533 | 3 / 5 | 2 / 5 | (same run) | (same run) | - | - |

CLI $ is the cost `claude -p` reports (notional on a subscription login). `:leads` rows score the same Orion run before its verifier. Missing = no ok run on the vulnerable snapshot (excluded, not counted as a miss).
