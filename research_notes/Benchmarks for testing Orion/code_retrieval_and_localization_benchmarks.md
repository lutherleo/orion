# Repository-level code retrieval, code localization and long-context code-understanding benchmarks (candidates for testing whether Orion's code graph helps a model)

Scope note for the report writer: compiled 2026-10-31. Facts come from arXiv abstracts pulled through the arXiv API
(export.arxiv.org, fetched this session), arXiv HTML full texts, Hugging Face dataset cards and API metadata,
official READMEs and leaderboards. Anything not confirmed in a primary source is in "Gaps" or marked "not stated" /
"not verified". "Fit" ratings are my inferences, not published facts. Orion facts (Joern -> Neo4j CPG, read-only
Cypher + semantic_search tools, Joern frontends for Java, JS/TS, Python, Go, C/C++) come from the assignment and the
project's own `C:/Users/h/Downloads/orion/CLAUDE.md`.

---

## Key Question 1: Which benchmarks exist, and what does each measure? (catalog with per-entry fields)

### Takeaway
There are roughly six families: (A) execution-scored issue resolution (SWE-bench and its descendants), (B) pure
localization benchmarks scored by Acc@k / F1 against gold-patch locations (LocBench, MULocBench, SWE-Explore,
IssueLoc-Bench, MM-IssueLoc, Long Code Arena bug localization, plus localization subsets derived from SWE-bench),
(C) repository-level completion/generation (RepoBench, CrossCodeEval, RepoEval, CoderEval, EvoCodeBench, DevEval,
REPOCOD...), (D) long-context and repository QA (RepoQA, LongCodeBench, LoCoBench, SWE-QA, StackRepoQA, SWE-Flux),
(E) code retrieval corpora (CodeSearchNet, CoSQA, CoIR, CORE-Bench, CLARC, CoREB), and (F) security localization,
which is Orion's home domain and is new in 2026 (VLoc-Bench, VulnGym), plus non-repo controls (BigCodeBench, ClassEval,
LiveCodeBench). For Orion, the families that score retrieval/localization directly without needing an execution
sandbox (B, F, parts of D) are the cheapest and most direct test of "does the graph help".

### Cited Findings

#### A. Issue-resolution family (execution-scored; localization can be derived from the gold patch)

| Benchmark | Year / maintainer | Size, languages | Repo available? Scoring | Dates / status | License (HF card) | Source |
|---|---|---|---|---|---|---|
| SWE-bench (Full) | 2023 (ICLR 2024), Princeton (Jimenez et al.) | 2,294 problems from 12 popular Python repos; task = edit the codebase to resolve an issue; best model at release (Claude 2) solved 1.96% | Full codebase + issue; % resolved by running tests; leaderboard at swebench.com | Static | HF card lists no license | [arXiv 2310.06770](https://arxiv.org/abs/2310.06770); [swebench.com](https://www.swebench.com/); [HF](https://huggingface.co/datasets/princeton-nlp/SWE-bench) |
| SWE-bench Lite | Princeton | 300 real GitHub issues (subset of Full) | Same as Full | Static | not stated on HF card | [AutoCodeRover arXiv 2404.05427](https://arxiv.org/abs/2404.05427); [HF](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite) |
| SWE-bench Lite-S | 2024, Agentless authors | Lite minus problems with exact ground-truth patch in the issue or insufficient/misleading descriptions | Same | Static | not stated | [Agentless arXiv 2407.01489](https://arxiv.org/abs/2407.01489) |
| SWE-bench Verified | OpenAI-curated subset | 500 tasks | Same | **Deprecated by OpenAI 2026-02-23** (stopped reporting; recommends SWE-bench Pro); Epoch verdict "Flawed": OpenAI audited 27.6% of tasks, 59.4% of audited had flawed tests (floor 16.4% of all tasks); Epoch lists 100% of tasks and solutions public (as of 2026-09-03) | not stated on HF card | [OpenAI post](https://openai.com/en/index/why-we-no-longer-evaluate-swe-bench-verified); [Epoch review](https://epoch.ai/benchmarks/swe-bench-verified/review); 500-task size: [arXiv 2610.33958](https://arxiv.org/abs/2610.33958) |
| SWE-bench Multimodal | 2024, Princeton | 617 tasks from 17 JavaScript libraries, every task has an image in issue or tests; SWE-agent resolved 12% vs 6% next best at release | Full repo; % resolved | Static | not stated | [arXiv 2410.03859](https://arxiv.org/abs/2410.03859) |
| SWE-bench Multilingual | SWE-bench team | 300 tasks, 41 repos, 9 languages (Ruby, Rust, PHP, Java, Go, C, JS, TS, C++) | Full repo; % resolved | PRs 2017 to 2025 (preview `created_at` Feb 2017 to Mar 2025); HF last modified 2026-08-17 | MIT | [HF card](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual) |
| SWE-bench-Live (Python) | 2025 (NeurIPS 2025), Microsoft | Initial 1,319 tasks from issues created since 2024, 93 repos, a Docker image per task; +50 verified issues added per month to the `full` split; `lite` and `verified` splits frozen | Full repo; % resolved | Live, monthly; HF last modified 2026-09-04 | MIT | [arXiv 2505.23419](https://arxiv.org/abs/2505.23419); [README](https://raw.githubusercontent.com/microsoft/SWE-bench-Live/main/README.md); [HF](https://huggingface.co/datasets/SWE-bench-Live/SWE-bench-Live) |
| SWE-bench-Live/MultiLang | Microsoft (RepoLaunch, NeurIPS 2026) | Released 2026-01-01 (languages listed then: C/C++, C#, Java, TS/JS, Go, Rust); as of 2026-08-21: 1,077 tasks, 431 repos, 8 languages, each language split >100 tasks | Full repo; % resolved; trajectories required for submission | Live | MIT (project) | [README](https://raw.githubusercontent.com/microsoft/SWE-bench-Live/main/README.md); RepoLaunch [arXiv 2603.05026](https://arxiv.org/abs/2603.05026) |
| SWE-bench-Live/Windows | Microsoft | Released 2026-03-08; as of 2026-08-21: 66 tasks, 48 repos, 9 languages, PowerShell actions | Full repo | Live | MIT (project) | [README](https://raw.githubusercontent.com/microsoft/SWE-bench-Live/main/README.md) |
| SWE-Bench Pro | 2025, Scale AI | 1,865 problems, 41 actively maintained repos; public set (11 repos) open, held-out (12 repos) and commercial (18 proprietary repos) private; long-horizon, multi-file patches, human-verified | Public set full repo; % resolved | Public set 731 instances; languages discussed: Go, Python, JS, TS; current public top: Muse Spark 1.1 61.50 +/- 3.10, gpt-5.4 (xHigh) 59.10, Muse Spark 55.00, claude-opus-4-6 (thinking) 51.90, gemini-3.1-pro 46.10 (mini-swe-agent harness; uncapped 250-turn limit unless greyed) | HF card lists no license; last modified 2026-09-22 | [arXiv 2509.16941](https://arxiv.org/abs/2509.16941); [leaderboard](https://labs.scale.com/leaderboard/swe_bench_pro_public); [HF](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) |
| Multi-SWE-bench | 2025, ByteDance Seed | 1,632 instances (from 2,456 candidates, 68 annotators) in Java, TS, JS, Go, Rust, C, C++; 33 repos (Rust 10, JS 6, C++ 5, C 3, Go 3, Java 3, TS 3); also Multi-SWE-RL with 4,723 training instances | Full repo; % resolved; leaderboard multi-swe-bench.github.io | Static; a "Flash" subset exists (used in 2026 localization work) | CC0 "subject to any IP rights owned by Bytedance"; HF last modified 2026-07-08 | [arXiv 2504.02605](https://arxiv.org/abs/2504.02605); [HF](https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-bench); Flash: [arXiv 2610.05099](https://arxiv.org/abs/2610.05099) |
| SWE-bench-java | 2024 | Java version of SWE-bench; count not stated in abstract; "work in progress" | Full repo, Docker eval | Likely superseded by Multi-SWE-bench (inference; see Inferences) | not stated | [arXiv 2408.14354](https://arxiv.org/abs/2408.14354) |
| SWE-PolyBench | 2025, Amazon | 2,110 instances, 21 repos: Java 165, JS 1,017, TS 729, Python 199; bug fixes, features, refactoring; stratified SWE-PolyBench500 (125/language); **defines file-level and CST-node-level (class/function) retrieval precision/recall** | Full repo; pass rate + retrieval metrics | Static; PR date range not stated; a "SWE-PolyBench Verified" subset is used by Prometheus (2025) | MIT | [arXiv 2504.08703](https://arxiv.org/abs/2504.08703); [HTML](https://arxiv.org/html/2504.08703v3); [HF](https://huggingface.co/datasets/AmazonScience/SWE-PolyBench); Verified: [arXiv 2507.19942](https://arxiv.org/abs/2507.19942) |
| SWE-Gym | 2024 (ICML 2025) | 2,438 Python tasks with runtime envs; a TRAINING environment; fine-tuned agents + verifiers reached 32.0% Verified / 26.0% Lite | Full repo + env | Training resource, not a test set | MIT | [arXiv 2412.21139](https://arxiv.org/abs/2412.21139); [HF](https://huggingface.co/datasets/SWE-Gym/SWE-Gym) |
| SWE-rebench | 2025 (NeurIPS 2025), Nebius | >21,000 interactive Python tasks (RL training) + a continuously refreshed decontaminated leaderboard; finds some models' Verified scores may be inflated by contamination | Full repo + env | Continuous | CC-BY-4.0 | [arXiv 2505.20411](https://arxiv.org/abs/2505.20411); [swe-rebench.com](https://swe-rebench.com); [HF](https://huggingface.co/datasets/nebius/SWE-rebench) |
| SWE-smith | 2025 | 50k synthetic task instances from 128 repos (training); SWE-agent-LM-32B 40.2% Verified | Full repo + env | Training | not stated | [arXiv 2504.21798](https://arxiv.org/abs/2504.21798) |
| K Prize (Konwinski Prize) | 2024-2025, Andy Konwinski / Kaggle | Round 1 tested only on GitHub issues flagged after the 2025-03-12 submission deadline; winner scored 7.5% (9 of 120 issues); 616 teams | Full repo; resolved | Contamination-free by construction; later rounds not found | not stated | [konwinski.com round-one post](https://andykonwinski.com/2025/03/12/kprize-next-steps.html); [Yahoo/TechCrunch coverage](https://finance.yahoo.com/news/ai-coding-challenge-just-published-000000906.html) |
| SWE-Dev | 2025 | 14,000 train + 500 test feature-development tasks with runnable envs; best single-turn 22.51% Pass@1 (hard), OpenHands 56.44% | Full repo + env | Static | not stated | [arXiv 2505.16975](https://arxiv.org/abs/2505.16975) |
| FEA-Bench | 2025 (ACL 2025) | Feature implementation from PRs of 83 repos, with unit tests | Full repo | Static | not stated | [arXiv 2503.06680](https://arxiv.org/abs/2503.06680) |

#### B. Code / issue localization benchmarks (no execution needed to score)

| Benchmark | Year / maintainer | Size, languages | Context / scoring | Dates | Source |
|---|---|---|---|---|---|
| Loc-Bench (LocBench) | 2025 (ACL 2025), Yale Gerstein lab (LocAgent) | 560 issues: bug report 242, feature request 150, security 29, performance 139; Python only; excludes patches touching >5 Python files or >10 functions | Full repo; Acc@k at file / module / function level (all gold locations must be in top-k); NDCG@1/3/5/10 in appendix | Bug reports created after Oct 2024; authors provide tooling to add new examples | [arXiv 2503.09089](https://arxiv.org/abs/2503.09089); [HTML](https://arxiv.org/html/2503.09089v2); [repo](https://github.com/gersteinlab/LocAgent); [HF Loc-Bench_V1](https://huggingface.co/datasets/czlll/Loc-Bench_V1) |
| SWE-bench Lite / Verified localization (derived) | used by LocAgent, SweRank, CoSIL, OrcaLoca, CuraLoc, A^2Agent, 2610.33958 | 300 / 500 tasks; gold = files/functions edited by the reference patch | Acc@k, Func@1, Func Acc@10, file Acc@5, F1 | Contaminated (see KQ4) | [arXiv 2505.07849](https://arxiv.org/abs/2505.07849); [arXiv 2610.33958](https://arxiv.org/abs/2610.33958) |
| MULocBench | 2025 | 1,100 issues from 46 Python projects; includes non-code locations (commits, comments, configs, docs) | File-level Acc@5 and F1 below 40% for all evaluated methods | not stated | [arXiv 2509.25242](https://arxiv.org/abs/2509.25242); [HF](https://huggingface.co/datasets/somethingone/MULocBench) |
| Long Code Arena: bug localization | 2024, JetBrains Research | One of 6 LCA tasks; HF subsets java 5.04K, kt 1.24K, py 8.68K rows (~14.96K total); java split dev 2.52K / train 2.47K / test 50; fields include `base_sha`, `head_sha`, `diff`, `changed_files`, issue title/body | Repo identified by owner/name + base SHA; metrics not shown on card | Dates not shown (sample timestamps broken, 1970) | [arXiv 2406.11612](https://arxiv.org/abs/2406.11612); [HF dataset](https://huggingface.co/datasets/JetBrains-Research/lca-bug-localization) (Apache-2.0); [leaderboard space](https://huggingface.co/spaces/JetBrains-Research/long-code-arena) |
| SWE-Explore | 2026-06 | 848 issues, 10 languages, 203 repos; explorer returns ranked code regions under a fixed line budget; line-level ground truth distilled from independent successful agent trajectories | Coverage, ranking, context-efficiency metrics; "agentic explorers form a clear tier above classical retrieval"; line-level coverage is the differentiator | 2026 | [arXiv 2606.07297](https://arxiv.org/abs/2606.07297) |
| IssueLoc-Bench | 2026-08 | 499 SWE-bench-Verified-derived tasks + 500 tasks from 153 other repos; read-only interactive interface | Early candidate discovery, top-3 gold-file coverage (Hit@3), strict file-set recovery, F1, time, tokens; cheaper explorers keep ~78-94% of reference Hit@3 at 84-95% fewer tokens | 2026 | [arXiv 2608.29675](https://arxiv.org/abs/2608.29675) |
| MM-IssueLoc | 2026-07 | 652 issue-PR instances, 23 languages, with images (7 image categories) | File Acc@5, function Acc@10; best agent 38.96 file Acc@5 / 22.45 function Acc@10; best retriever 33.86 function Acc@10 | 2026 | [arXiv 2607.15205](https://arxiv.org/abs/2607.15205) |
| PLocBench | 2026 (used by SemNav) | not stated in abstract | File Hit@10 | 2026 | [arXiv 2609.31176](https://arxiv.org/abs/2609.31176) |
| SweLoc (training set) | 2025 (ICLR 2026), Salesforce (SweRank) | From top 11,000 PyPI packages, filtered to 3,387 repos, 67,341 initial (PR, codebase) pairs; excludes SWE-bench and LocBench repos | Training data for retrievers/rerankers | Date range not stated | [arXiv 2505.07849](https://arxiv.org/abs/2505.07849); [HTML](https://arxiv.org/html/2505.07849v2) |

#### C. Repository-level code completion / generation

| Benchmark | Year / maintainer | Size, languages | Context / scoring | Notes | Source |
|---|---|---|---|---|---|
| RepoBench | 2023 (ICLR 2024) | Python and Java; RepoBench-R (retrieve the relevant cross-file snippet), -C (next-line completion with cross-file + in-file context), -P (pipeline) | Retrieval accuracy; completion EM/ES | HF v1.1 license "cc"; exact sizes not verified here | [arXiv 2306.03091](https://arxiv.org/abs/2306.03091); [repo](https://github.com/Leolty/repobench); [HF](https://huggingface.co/datasets/tianyang/repobench_python_v1.1) |
| CrossCodeEval | 2023 (NeurIPS 2023 D&B), Amazon | Python, Java, TypeScript, C#; examples chosen by static analysis so they strictly need cross-file context; Python subset 2,665 samples (as used by RepoGraph) | EM, ES, identifier EM/F1; retrieval settings BM25 / UniXcoder / OpenAI Ada; raw repo data "available by emailing the authors" | Repo Apache-2.0 | [arXiv 2310.11248](https://arxiv.org/abs/2310.11248); [repo](https://github.com/amazon-science/cceval); Python count: [RepoGraph HTML](https://arxiv.org/html/2410.14684v2) |
| RepoEval (RepoCoder) | 2023 (EMNLP 2023), Microsoft | Line, API-invocation and function-body completion on recent real repos | EM/ES (line, API), unit-test pass (function); RepoCoder +>10% over in-file baseline | Sizes not verified here | [arXiv 2303.12570](https://arxiv.org/abs/2303.12570); [repo](https://github.com/microsoft/CodeT/tree/main/RepoCoder) |
| CoderEval | 2023 | 230 Python + 230 Java tasks, six levels of context dependency; execution platform | Pass@k | Non-standalone functions much harder | [arXiv 2302.00288](https://arxiv.org/abs/2302.00288) |
| EvoCodeBench | 2024 | EvoCodeBench-2403: 275 samples, 25 repos; evolving, auto-update pipeline | Pass@k and Recall@k (of reference dependencies); gpt-4 best Pass@1 20.73% | Evolving (only 2403 release verified) | [arXiv 2404.00599](https://arxiv.org/abs/2404.00599); [repo](https://github.com/seketeam/EvoCodeBench) |
| DevEval | 2024 (ACL 2024) | 1,874 samples, 117 repos, 10 domains; annotated reference dependencies | Pass@k (gpt-4-turbo best 53.04%), dependency recall | | [arXiv 2405.19856](https://arxiv.org/abs/2405.19856) |
| REPOCOD | 2024 (v5 2026-10-20) | 980 whole-function tasks from 11 Python projects, 50.8% need repo-level context, 314 dev-written tests per instance on average | Pass@1; no model >30%; RAG beat giving target-function dependencies as context | | [arXiv 2410.21647](https://arxiv.org/abs/2410.21647) |
| RepoExec | 2024 (NAACL 2025) | Repo-level generation with developer-specified dependencies; Dependency Invocation Rate metric | Pass@k + DIR | | [arXiv 2406.11927](https://arxiv.org/abs/2406.11927) |
| M2RC-Eval | 2024 | Repo-level completion in 18 languages, AST-based bucket/semantic labels | EM/ES | | [arXiv 2410.21157](https://arxiv.org/abs/2410.21157) |
| ExecRepoBench | 2024 | 1.2K samples from active Python repos, AST-masked units | Executable | | [arXiv 2412.11990](https://arxiv.org/abs/2412.11990) |
| ReccEval | 2024 (ACL 2024, DraCo) | Large Python completion dataset with diverse targets | EM, identifier F1 | | [arXiv 2405.19782](https://arxiv.org/abs/2405.19782) |
| ClassEval | 2023 | 100 hand-made Python class-level tasks | Pass@k | Non-repo control; MIT | [arXiv 2308.01861](https://arxiv.org/abs/2308.01861); [HF](https://huggingface.co/datasets/FudanSELab/ClassEval) |
| BigCodeBench | 2024 (ICLR 2025) | 1,140 tasks, 139 libraries, avg 5.6 tests, 99% branch coverage; best ~60% vs human 97% | Pass@1 | Non-repo control; Apache-2.0 | [arXiv 2406.15877](https://arxiv.org/abs/2406.15877); [HF](https://huggingface.co/datasets/bigcode/bigcodebench) |
| LiveCodeBench | 2024 | Contest problems collected continuously from LeetCode/AtCoder/CodeForces (initially 400 problems, May 2023 to May 2024) | Pass@1 etc. | Non-repo, time-split control | [arXiv 2403.07974](https://arxiv.org/abs/2403.07974) |

#### D. Long-context and repository question answering

| Benchmark | Year | Size / task | Scoring / notes | Source |
|---|---|---|---|---|
| RepoQA (Searching Needle Function) | 2024 | 500 tasks from 50 repos in 5 languages: find a function from its NL description inside a long repo context | Retrieval accuracy; models may understand code better without comments | [arXiv 2406.06025](https://arxiv.org/abs/2406.06025) |
| Long Code Arena | 2024, JetBrains | 6 tasks needing project-wide context: library-based codegen, CI build repair, project-level completion, commit message generation, bug localization, module summarization | Per-task; HF leaderboard | [arXiv 2406.11612](https://arxiv.org/abs/2406.11612) |
| LongCodeBench (LongCodeQA + LongSWE-Bench) | 2025 | Comprehension and repair at up to 1M tokens, from GitHub issues | Claude 3.5 Sonnet 29% -> 3% and Qwen2.5 70.2% -> 40% as context grows; Apache-2.0 | [arXiv 2505.07897](https://arxiv.org/abs/2505.07897); [HF](https://huggingface.co/datasets/Steefano/LCB) |
| LoCoBench | 2025, Salesforce | 8,000 *generated* scenarios, 10 languages, 10K-1M tokens, 8 categories incl. architectural understanding, bug investigation, security analysis; 17 metrics | Synthetic codebases (not real repos) | [arXiv 2509.09614](https://arxiv.org/abs/2509.09614) |
| SWE-QA | 2025 (ACL 2026 Findings) | 576 QA pairs from 11 repos (from 77,100 crawled issues); intention understanding, cross-file reasoning, multi-hop dependency analysis; SWE-QA-Agent baseline | Evaluated under "various context augmentation strategies" | [arXiv 2509.14635](https://arxiv.org/abs/2509.14635); [repo](https://github.com/peng-weihan/SWE-QA-Bench) |
| StackRepoQA | 2026-03 | 1,318 real developer questions + accepted answers from 134 Java projects | Compares file-level RAG vs graph-based (structural dependency) retrieval; high scores often come from verbatim reproduction of Stack Overflow answers | [arXiv 2603.26567](https://arxiv.org/abs/2603.26567) |
| SWE-Flux | 2026-09 | 480 execution-grounded questions over 12 Python repos (control flow, loops, state, **dataflow**, exceptions, invariants); gold answers harvested from instrumented test runs; perturbation creates fresh variants | Best model 37%; weakest on dataflow and inter-procedural execution | [arXiv 2609.28449](https://arxiv.org/abs/2609.28449) |
| CodeQueries | 2022 | Semantic queries over Python derived from CodeQL queries; file-level context; answers are code spans + supporting facts; single- and multi-hop | Span-extraction; GPT-3.5 and CuBERT achieve limited success | [arXiv 2209.08372](https://arxiv.org/abs/2209.08372) |
| MemDev-Bench | 2026-10 | 555 base tasks + 4,869 executable historical tasks; tests memory systems for code agents | Memory mainly improves fine-grained localization and editing | [arXiv 2610.27524](https://arxiv.org/abs/2610.27524) |

#### E. Code retrieval corpora

| Benchmark | Year | Size | Repo-level? | Source |
|---|---|---|---|---|
| CodeSearchNet | 2019, GitHub/Microsoft | ~6M functions in Go, Java, JS, PHP, Python, Ruby; 2M with docstrings; challenge = 99 NL queries, ~4k expert relevance labels | No (function corpus) | [arXiv 1909.09436](https://arxiv.org/abs/1909.09436) |
| CoSQA | 2021 (ACL 2021), Microsoft | 20,604 human-labelled web query / code pairs | No | [arXiv 2105.13239](https://arxiv.org/abs/2105.13239) |
| CoIR | 2024 (ACL 2025) | 10 datasets, 8 retrieval tasks, 7 domains; MTEB/BEIR schema | No | [arXiv 2407.02883](https://arxiv.org/abs/2407.02883); [repo](https://github.com/CoIR-team/coir) |
| CORE-Bench | 2026 (EMNLP 2026) | >180K queries, 106K broader-context labels; three levels: code understanding, issue-to-edit localization, broader context retrieval; built partly from SWE-bench-series instances; embedding models drop sharply vs classic code search | Partly (repository state) | [arXiv 2606.11864](https://arxiv.org/abs/2606.11864) |
| CLARC | 2026 (ICLR 2026) | C/C++ code search, 1,245 eval + 5,472 train pairs; anonymized identifiers and Assembly/WASM settings expose lexical reliance | No | [arXiv 2603.04484](https://arxiv.org/abs/2603.04484); [HF](https://huggingface.co/datasets/ClarcTeam/CLARC) |
| CoREB | 2026-05 | Contamination-limited retrieval+rerank benchmark, counterfactually rewritten LiveCodeBench problems in 5 languages, timed releases, graded relevance; short keyword queries collapse every model to near-zero nDCG@10 | No | [arXiv 2605.04615](https://arxiv.org/abs/2605.04615) |
| CodeRAG-Bench | 2024 | Basic, open-domain and repository-level codegen tasks + 5 document sources (competition solutions, tutorials, library docs, StackOverflow, GitHub repos); retrievers struggle with low lexical overlap | Partly | [arXiv 2406.14497](https://arxiv.org/abs/2406.14497) |

#### F. Classic fault localization and security localization (Orion-adjacent)

| Benchmark | Facts | Source |
|---|---|---|
| Defects4J | v3.0.1; 854 active bugs (+10 deprecated) from real Java projects; reproducible with triggering tests | [README](https://raw.githubusercontent.com/rjust/defects4j/master/README.md) |
| BugsInPy | Database of real Python bugs for controlled testing/debugging studies (count not verified here) | [repo](https://github.com/soarsmu/BugsInPy) |
| AutoFL results | LLM navigates the repo via function calls; 798 real Java + Python bugs; method-level acc@1 up to +233.3% over baselines | [arXiv 2308.05487](https://arxiv.org/abs/2308.05487) |
| AgentFL results | Defects4J v1.2.0: 157 of 395 bugs localized at Top-1; $0.074 and 97 s per bug | [arXiv 2403.16362](https://arxiv.org/abs/2403.16362) |
| VLoc-Bench (Cisco Foundation AI) | 500 real vulnerabilities, 290 repos, 6 ecosystems (Go 43%, Maven 21%, npm 18%, pip 10%, Rust 8%, Composer 1 task), 147 CWEs; vulnerable + patched snapshot per task; agent gets only the CWE description + read-only terminal (15 commands, 20 turns); Phase A File F1, Phase B true-negative rate on patched snapshot; best File F1 0.229 (GPT-5.5 xhigh, P 0.310 / R 0.221) but its patched-snapshot TNR only 0.279; 38.4% of tasks localized by no model; static tools: Semgrep F1 0.086, Semgrep-CWE 0.052, **CodeQL 0.023**, Horusec 0.020; advisories 2016-2026 incl. 21 from 2026; **no code-graph or retrieval tool was given to agents** | [arXiv 2609.15939](https://arxiv.org/abs/2609.15939); [HTML](https://arxiv.org/html/2609.15939v1) |
| VulnGym | 184 GitHub advisories, 408 vulnerability entries, 23 repos; each entry annotated with line-level entry points, critical operations and vulnerability traces; end-to-end detection + 3 oracle subtasks diagnosing localization and evidence construction | [arXiv 2608.02001](https://arxiv.org/abs/2608.02001) |
| SecRepoBench | 318 secure-completion tasks in 27 C/C++ repos, 15 CWEs; agents beat standalone LLMs | [arXiv 2504.21205](https://arxiv.org/abs/2504.21205) |

#### Superseded / renamed items
- arXiv 2504.10046 ("CodeRAG", bigraph retrieval) is now titled **GraphCodeAgent: Dual Graph-Guided LLM Agent** (v2, 2025-11-18), evaluated on DevEval and CoderEval — [arXiv 2504.10046](https://arxiv.org/abs/2504.10046).
- SWE-bench-Live's Python-only "NIPS paper version" lives on a `python-only` branch; MultiLang/Windows are the current line — [README](https://raw.githubusercontent.com/microsoft/SWE-bench-Live/main/README.md).
- SWE-bench Verified: OpenAI stopped reporting it in Feb 2026; Epoch rates it "Flawed" — [OpenAI](https://openai.com/en/index/why-we-no-longer-evaluate-swe-bench-verified); [Epoch](https://epoch.ai/benchmarks/swe-bench-verified/review).
- SWE-agent is listed as "legacy" in the SWE-bench family (mini-SWE-agent is the current harness) — [swebench.com](https://www.swebench.com/).

### Inferences
- Fit ratings for an Orion-style graph tool (my judgment; H = high, M = medium, L = low):

| Benchmark | Fit | Why |
|---|---|---|
| VLoc-Bench | **H** | Security localization from a CWE description, i.e. Orion's own task; ~92% of tasks (Go+Maven+npm+pip) are in Joern-supported ecosystems; patched snapshot gives a false-alarm arm like Orion-LM's patched-twin design; nobody has tried graph tools on it; static tools (CodeQL F1 0.023) set a very low bar. |
| VulnGym | **H** | Line-level entry points / critical operations / traces map onto Orion's `:EntryPoint` -> FLOWS_TO -> sink paths; tests evidence construction, which is Orion's "grounded claim" premise. |
| LocBench | **H** | Graph schema of LocAgent (file/class/function; contain/import/invoke/inherit) is a subset of Orion's CPG; Python; no execution; but post-Oct-2024 data is now pre-cutoff. |
| SWE-bench Lite/Verified localization | H for comparability, L for validity | Every graph localizer reports here, so it is the only place to compare to LocAgent/CoSIL/OrcaLoca/SweRank/CuraLoc; contamination makes absolute numbers suspect. |
| SWE-PolyBench (500) | **H** | Java/JS/TS/Python are all Joern frontends; built-in file and CST-node retrieval precision/recall. |
| SWE-bench-Live (Python, MultiLang) | **H** | Fresh tasks, base commits, multi-language; derive localization gold from patches; post-May-2026 slices possible. |
| Multi-SWE-bench | M-H | Java, TS, JS, Go, C, C++ covered by Joern; Rust (10 of 33 repos) is not. |
| Long Code Arena bug localization | M-H | Python + Java with base SHAs; Kotlin support in Orion not verified. |
| SWE-Explore, IssueLoc-Bench, MULocBench, MM-IssueLoc | M | Pure exploration/localization; MULocBench's non-code files and MM-IssueLoc's images are outside a CPG. |
| RepoQA | M | Direct test of `semantic_search` vs long context; graph adds little to a needle-function lookup. |
| SWE-QA, StackRepoQA, SWE-Flux | M | Multi-hop and dataflow questions are graph-shaped; QA scoring is LLM-judged or answer-matched; SWE-Flux targets dataflow, the CPG's strength. |
| CodeQueries | M | CodeQL-style semantic queries are the closest analogue to Cypher-over-CPG, but context is a single file. |
| CrossCodeEval, RepoBench, RepoEval, GraphCoder/DraCo sets | M | Graph-RAG has published wins, but Orion has no completion pipeline; CrossCodeEval repos need an email request. |
| DevEval, EvoCodeBench, CoderEval, REPOCOD | M-L | Retrieval helps (EvoCodeBench Recall@k measures it) but scoring needs per-repo execution. |
| Defects4J / BugsInPy | M | Classic FL with method-level gold; old and very likely memorized. |
| CodeSearchNet, CoSQA, CoIR, CLARC, CoREB, CORE-Bench | L (CORE-Bench M) | Function corpora test the embedding model, not the graph. |
| BigCodeBench, ClassEval, LiveCodeBench | Control | No repo: a graph tool should give zero gain; useful as a negative control. |

- SWE-bench-java's Java tasks and its maintainers' Docker approach appear to have been folded into Multi-SWE-bench (both are multi-language extensions of SWE-bench; the HF card for Multi-SWE-bench lists 3 Java repos); I did not find an explicit supersession statement.

### Gaps
- Exact sizes for CrossCodeEval (beyond Python 2,665), RepoBench, RepoEval: PDF tables could not be parsed this session (no PDF library); see each paper's Table 1.
- BugsInPy bug count, CodeQueries query/sample counts, RepoQA's 5 language names, Multi-SWE-bench mini/flash sizes, PLocBench size: not confirmed in a primary source here.
- Licenses: SWE-bench, Lite, Verified, Multimodal, Pro and Loc-Bench HF cards list no license; repository licenses were not checked.
- SWE-bench Verified's current top score: the OpenAI post (via search summary) says SOTA went from 74.9% to 80.9% in the 6 months before Feb 2026; I could not load the live leaderboard numbers. The OpenAI page returned HTTP 403 to direct fetch, so its details here come from a search summary and from Epoch.
- K Prize rounds after round 1: not found.

---

## Key Question 2: Where is there published evidence that a code graph / graph retrieval beats plain retrieval or no retrieval, and by how much?

### Takeaway
Graph methods show consistent but usually modest gains: +2.0 to +2.7 points absolute resolve rate on SWE-bench Lite
when RepoGraph is bolted onto four different systems (32.8% average relative gain, driven by a weak RAG baseline),
large gains for localization (LocAgent 77.37 vs 70.07 function Acc@10 over OpenHands on Lite; KGCompass function-level
accuracy 56.0%), and large gains on completion (CodexGraph GPT-4o CrossCodeEval EM 27.90 vs 21.20 BM25 vs 10.80 no
RAG). But strong non-graph retrievers match or beat graph agents at localization (SweRank's 32B reranker: function
Acc@10 88.69 vs LocAgent 77.37 on Lite), weak models can do worse with a graph query language than with nothing
(CodexGraph + Qwen2-72B), the October 2026 depth-stratified study finds graph-tool gains peak at 1.7-4B models
(+17-21 points over grep) and shrink to +4-5 points at 32B, mostly from name lookup at 0-1 hops, and a separate study
finds better fault localization does not reliably improve repair.

### Cited Findings

#### Graph approaches vs baselines (exact published numbers)

- **CodexGraph (Neo4j + Cypher, Python only; schema MODULE/CLASS/METHOD/FUNCTION/FIELD/GLOBAL_VARIABLE nodes, CONTAINS/INHERITS/HAS_METHOD/HAS_FIELD/USES edges; no call edges)** — [HTML v3](https://arxiv.org/html/2408.03910v3); [arXiv 2408.03910](https://arxiv.org/abs/2408.03910); [demo](https://github.com/modelscope/modelscope-agent/tree/master/apps/codexgraph_agent)
  - GPT-4o: CrossCodeEval Lite (Python) EM No-RAG 10.80 / BM25 21.20 / AutoCodeRover 21.20 / **CodexGraph 27.90**; ES 59.36 / 66.18 / 61.92 / 67.98; SWE-bench Lite resolved BM25 3.11 / AutoCodeRover 22.96 / **CodexGraph 22.96**; EvoCodeBench Pass@1 No-RAG 27.83 / ACR 28.78 / **CodexGraph 36.02**.
  - DeepSeek-Coder-V2: CCEval EM 11.70 / 21.90 / 14.90 / 20.20 (BM25 wins); SWE-bench Lite ACR 15.56 vs CodexGraph 12.06; EvoCodeBench Pass@1 25.47 / - / 20.28 / 27.62.
  - Qwen2-72B: CCEval EM No-RAG 8.20 vs **CodexGraph 5.00** (worse than no retrieval); SWE-bench Lite 1.95 vs ACR 9.34.
  - Stated limitations: Python only; query output length not controllable (higher token cost than baselines); sometimes loops without producing executable queries; weaker LLMs struggle to write correct queries; some Sympy SWE-bench samples excluded because indexing ran out of memory.
- **RepoGraph (tree-sitter line-level def/ref graph, invoke + contain edges, k-hop ego graphs; Python only)** — [HTML](https://arxiv.org/html/2410.14684v2); [arXiv 2410.14684](https://arxiv.org/abs/2410.14684) (ICLR 2025)
  - SWE-bench Lite resolve: RAG (GPT-4) 2.67 -> 5.33; Agentless (GPT-4o) 27.33 -> 29.67; AutoCodeRover (GPT-4) 19.00 -> 21.33; SWE-agent (GPT-4o) 18.33 -> 20.33; with Claude-3.5-Sonnet: Agentless 27.67 -> 30.33, SWE-agent 23.00 -> 25.33. Cost rises (e.g. Agentless $0.34 -> $0.39 per task).
  - Localization (file / function / line) gains of +4.4 to +5.6 (file), +2.0 to +3.4 (function), +1.6 to +2.7 (line), e.g. Agentless 68.7 -> 74.3 file, 51.0 -> 54.0 function.
  - CrossCodeEval Python (2,665 samples): GPT-4o code EM 10.5 -> 28.7, identifier EM 16.8 -> 36.0; DeepSeek-Coder 10.2 -> 19.7.
  - Average graph: 1,419.3 nodes, 26,392.1 edges per SWE-bench repo.
- **LocAgent (heterogeneous graph: directory/file/class/function; contain/import/invoke/inherit; tools SearchEntity / TraverseGraph / RetrieveEntity; Python only)** — [HTML](https://arxiv.org/html/2503.09089v2); [arXiv 2503.09089](https://arxiv.org/abs/2503.09089)
  - SWE-bench Lite file Acc@5 / module Acc@10 / function Acc@10: BM25 61.68 / 52.92 / 36.86; CodeRankEmbed 84.67 / 78.83 / 58.76; Agentless (Claude-3.5) 79.56 / 68.98 / 58.76; MoatlessTools 86.13 / 76.28 / 64.96; SWE-agent 90.15 / 78.10 / 64.60; OpenHands 90.15 / 83.58 / 70.07; **LocAgent Claude-3.5 94.16 / 87.59 / 77.37; LocAgent Qwen2.5-32B fine-tuned 92.70 / 87.23 / 77.01**.
  - Loc-Bench: CodeRankEmbed 74.29 / 63.21 / 43.39; Agentless 67.50 / 53.39 / 42.68; OpenHands 79.82 / 68.93 / 59.11; **LocAgent Claude-3.5 83.39 / 70.89 / 59.29**.
  - Claims ~86% cost reduction with the fine-tuned 32B model and +12% downstream resolution at Pass@10.
- **CoSIL (call graphs built on the fly; no training or indexing)** — Top-1 localization 43.3% (Lite) and 44.6% (Verified) with Qwen2.5-Coder-32B, "average outperforming SOTA by 96.04%"; plugged into Agentless, resolution +2.98% to +30.5% — [arXiv 2503.22424](https://arxiv.org/abs/2503.22424) (ASE 2025).
- **OrcaLoca** — 65.33% function match rate on SWE-bench Lite (open-source SOTA at the time); +6.33 points resolved for an open-source framework — [arXiv 2502.00350](https://arxiv.org/abs/2502.00350).
- **KGCompass (repository-aware KG linking issues/PRs to files/classes/functions; narrows to 20 candidate functions)** — SWE-bench Lite 58.3% resolved with a single repair model, function-level fault-location accuracy 56.0%, $0.2 per repair; 89.7% of the bugs it localized had no explicit location hint and were found by multi-hop traversal; relative resolve gains over pure-LLM baselines +50.8% (Claude-4 Sonnet), +30.2% (Claude-3.5 Sonnet), +115.7% (DeepSeek-V3), +156.4% (Qwen2.5 Max) — [arXiv 2503.21710](https://arxiv.org/abs/2503.21710).
- **Code Graph Model (CGM)** — graph structure in the attention mechanism + agentless graph RAG; 43.00% SWE-bench Lite with Qwen2.5-72B, +12.33 over previous best open-model method — [arXiv 2505.16901](https://arxiv.org/abs/2505.16901).
- **LingmaAgent / RepoUnderstander (repo KG + MCTS)** — +18.5% relative over SWE-agent on Lite; 16.9% of Alibaba in-house issues auto-resolved — [arXiv 2406.01422](https://arxiv.org/abs/2406.01422).
- **Prometheus (unified repo KG + working memory)** — GPT-5: 74.4% SWE-bench Verified, 33.8% SWE-PolyBench Verified — [arXiv 2507.19942](https://arxiv.org/abs/2507.19942).
- **GraphCoder (code context graph: control flow, data and control dependence)** — +6.06 EM (code match) and +6.23 EM (identifier match) over retrieval-augmented baselines, less time/space — [arXiv 2406.07003](https://arxiv.org/abs/2406.07003).
- **DraCo (dataflow-guided repo context graph)** — +3.43% code EM and +3.27% identifier F1 over prior SOTA on ReccEval — [arXiv 2405.19782](https://arxiv.org/abs/2405.19782) (ACL 2024).
- **GRACE (AST + call graph + class hierarchy + data-flow graph, GNN retriever)** — +8.19% EM and +7.51% ES over the strongest graph-RAG baselines with DeepSeek-V3 — [arXiv 2509.05980](https://arxiv.org/abs/2509.05980).
- **RepoHyper (Repo-level Semantic Graph, expand-and-refine)** — "markedly outperforms" baselines on RepoBench; no numbers in abstract — [arXiv 2403.06095](https://arxiv.org/abs/2403.06095).
- **GraphCodeAgent (formerly CodeRAG)** — requirement graph + structural-semantic code graph; "significantly outperforms" baselines on DevEval and CoderEval; no numbers in abstract — [arXiv 2504.10046](https://arxiv.org/abs/2504.10046).
- **RANGER (repo KG to variable level; Cypher lookups for entity queries, MCTS graph exploration for NL queries)** — beats embedding baselines (incl. Qwen3-8B embeddings) on CodeSearchNet and RepoQA, better cross-file dependency retrieval on RepoBench, best EM on CrossCodeEval when paired with BM25; no numbers in abstract — [arXiv 2509.25257](https://arxiv.org/abs/2509.25257).
- **StackRepoQA** — Claude 3.5 Sonnet and GPT-4o improve when structural (graph) signals are added vs file-level RAG, but accuracy stays limited and high scores often reflect memorized Stack Overflow answers — [arXiv 2603.26567](https://arxiv.org/abs/2603.26567); the graph is a Neo4j database built from AST + static dependencies per a search summary of the paper ([search result pointing to the arXiv PDF](https://arxiv.org/pdf/2603.26567)).
- **Multi-CoLoR (SANER 2026)** — extends LocAgent to C++ and QML in an AMD enterprise codebase; improves Acc@5 over lexical and graph baselines while reducing tool calls — [arXiv 2602.19407](https://arxiv.org/abs/2602.19407).
- **AutoCodeRover (AST class/method search APIs + spectrum-based FL)** — 19% on Lite at $0.43 — [arXiv 2404.05427](https://arxiv.org/abs/2404.05427). **Agentless (hierarchical file -> class/function -> line localization, no agent)** — 32.00% Lite (96 fixes) at $0.70 — [arXiv 2407.01489](https://arxiv.org/abs/2407.01489).
- **MoatlessTools** uses pre-embedded vector stores (Voyage AI) for SWE-bench instances; reports 70.8% with Claude 4 Sonnet (split not stated) — [repo](https://github.com/aorwall/moatless-tools).

#### 2026 evidence (newest, most directly comparable to an Orion experiment)

- **"How Do Small Coding Agents Use Code Graphs and Embeddings?" (2026-10-25)** — same localization agent, arms differ only in tools: G = grep/read_file/list_dir; E = G + semantic_search (Qwen3-Embedding-0.6B over function/class nodes); T = G + find_entity + graph_neighbors (1-2 hop BFS over contain/import/invoke/inherit); ET = all. Metric: function Acc@10 (all gold functions in top 10). SWE-bench Verified, 79k episodes — [arXiv 2610.33958](https://arxiv.org/abs/2610.33958); [HTML](https://arxiv.org/html/2610.33958v1)
  - All 500 tasks, G / E / T / ET: Qwen3-8B 16.7 / 30.3 / 27.4 / 33.2; Qwen3-4B 26.8 / 29.1 / 37.1 / 36.5; Gemma 4 E4B 16.7 / 28.5 / 26.8 / 29.7; Qwen3-32B 32.0 / 35.7 / 34.0 / 35.9.
  - Structured-tool gain over grep: 7-9 points at 0.6B, 17-21 at 1.7-4B, 4-5 at 32B; from 8B up about half the gain is the form of the submission rather than finding the target.
  - Hop depth (shortest code-graph path from code the issue mentions to the changed function): 37.2% of Verified tasks are hop 0, 25.6% hop 1, 15.6% hop 2, 15.2% hop 3+; each extra hop multiplies the odds of correct localization by 0.50-0.67 and no tool set flattens that; graph gains are name lookup at hop 0 and one expansion at hop 1; agents chain expansions in only 12-14% of episodes.
  - Downstream: a fixed repair model resolved 6 / 13 / 12 / 13 of 160 tasks from G / E / T / ET localizations (oracle 26).
- **SweRank (ICLR 2026; retrieve-and-rerank, no graph)** — SWE-bench Lite file Acc@1 / function Acc@10: SweRankEmbed-Small (137M) 66.42 / 74.45; SweRankLLM-Large (32B) **83.21 / 88.69**; vs LocAgent (Claude-3.5) 77.74 / 77.37 and OpenHands 76.28 / 70.07; LocBench function Acc@10: SweRankLLM-Large 71.25 vs LocAgent Claude 59.29 — [HTML](https://arxiv.org/html/2505.07849v2); [arXiv 2505.07849](https://arxiv.org/abs/2505.07849).
- **CuraLoc (2026-10)** — plans graph-relationship inspection in one decision then retrieves deterministically: SWE-bench Verified Func@1 59.3% vs LocAgent 53.8%, RepoSearcher 56.2%, Agentless 39.6% (GPT-5.2), with 90.3% fewer tokens; Lite Func@5 83.8% — [arXiv 2610.31809](https://arxiv.org/abs/2610.31809).
- **SemNav (2026-09)** — language-server-resolved Semantic Navigation Graph + issue-conditioned semantic cards: File Hit@10 68.33% -> 82.67% with Gemma 4B on SWE-bench Lite/PLocBench; working-context load -48.2% vs full-source reading; downstream resolution 44.00% -> 52.33%; first on SWE-Explore evidence-quality metrics — [arXiv 2609.31176](https://arxiv.org/abs/2609.31176).
- **SeeRepo (ASE 2026)** — rendered dependency-graph images as a supplement to text tools cut input tokens by up to 26% with equal or better resolution; vision-only degrades accuracy — [arXiv 2606.14061](https://arxiv.org/abs/2606.14061).
- **RepoAtlas (2026-09)** — evolving multimodal views over a repo code graph: +2.4 points resolve on SWE-bench Verified, -5.8% input tokens, -7.8% model calls vs strongest multimodal graph baseline — [arXiv 2609.16936](https://arxiv.org/abs/2609.16936).
- **A^2Agent (EMNLP 2026)** — action-aware RL for localization agents: +1.58% average F1 on Verified and +8.55% on SWE-Bench Pro over SOTA; 4B model beats baselines up to 8x larger — [arXiv 2608.29831](https://arxiv.org/abs/2608.29831).
- **ColGREP small agents (2026-10)** — semantic (late-interaction) search beats grep for <2B localization agents on SWE-bench Lite and Multi-SWE-bench Flash; -44.1% latency on CPU, -29.1% tokens, better transfer to unseen languages — [arXiv 2610.05099](https://arxiv.org/abs/2610.05099).
- **XAgent (2026-09, execution-guided)** — 62.0% resolved and 72.8% function localization on SWE-bench Lite — [arXiv 2609.09769](https://arxiv.org/abs/2609.09769).
- **"Thousand-Graph Hypothesis" (2026-08)** — entity-only index with task-conditioned relation materialization: 92.1% / 94.2% / 95.6% "success" (base / one-layer / two-layer) on SWE-bench Verified with DeepSeek-V4-Flash and zero pre-built relation edges; the abstract does not define "success" — [arXiv 2608.26602](https://arxiv.org/abs/2608.26602).
- **"From Finding to Fixing" (2026-10)** — six FL methods on SWE-bench Verified and Defects4J, 3 LLMs, 16,248 repair trajectories: FL methods do not consistently improve repair, higher localization accuracy does not consistently yield larger repair gains; accurate locations can cut repair tokens by up to 39.2%, but current FL methods often cost more tokens than they save; single-reference-patch ground truth misses locations successful repairs used — [arXiv 2610.25826](https://arxiv.org/abs/2610.25826).
- **"Physical Cost of Context" (NeurIPS 2026 workshop)** — on 450 localization cases from SWE-rebench V2, preloading ten retrieved blocks ("Eager") cut model calls 35.7% and a GPU-energy proxy 33.2% vs on-demand grep, passing a five-point non-inferiority quality gate — [arXiv 2610.34763](https://arxiv.org/abs/2610.34763).
- **SWE-PolyBench retrieval metrics (non-graph agents, Sonnet 3.5)** show localization collapses outside Python: file recall Agentless-PB 60.9 (Python) vs 17.5 (TS); CST-node recall 38.2 (Python) vs 17.2 (TS) — [HTML](https://arxiv.org/html/2504.08703v3).

### Inferences
- The strongest published "graph beats no-graph" effects are on (a) repository completion (CodexGraph and RepoGraph roughly double or triple CrossCodeEval EM over no retrieval with GPT-4o) and (b) function-level localization (LocAgent +7 points function Acc@10 over OpenHands on Lite). End-to-end resolution gains are small (+2-3 points absolute for RepoGraph).
- Graph-vs-embedding is the contested comparison: CodeRankEmbed and SweRank (pure ranking models) match or beat graph agents on file/function localization. An Orion experiment must include an embedding-only arm (Orion already has `semantic_search`) and a grep-only arm, copying the G/E/T/ET design of arXiv 2610.33958, otherwise a graph win cannot be attributed to the graph.
- CodexGraph is the published system most like Orion (Neo4j, Cypher written by the LLM). Its Qwen2-72B result (worse than no retrieval) and stated failure modes (unbounded output, loops, weak models writing bad queries) are directly relevant to Orion-LM, a 4B model writing Cypher; Orion's bounded `run_cypher` (row caps, streaming) addresses the output-length problem CodexGraph names.
- Orion's CPG carries data-flow (FLOWS_TO), which none of LocAgent/CodexGraph/RepoGraph schemas have (they use contain/import/invoke/inherit). The dataflow-aware results (DraCo, GraphCoder, GRACE) are all completion results; the depth study shows most localization targets sit at 0-1 hops, where name lookup suffices. So Orion's distinctive value is more likely to show on tasks needing source->sink reasoning (VLoc-Bench, VulnGym, SWE-Flux dataflow questions) than on generic issue localization.
- Because "From Finding to Fixing" shows localization accuracy does not reliably transfer to repair, Orion should report localization metrics and token/cost efficiency directly, rather than relying on resolve rate.
- No paper evaluates a Joern CPG (as opposed to tree-sitter/AST-level graphs) on these benchmarks, as far as I found; that is an open slot.

### Gaps
- No primary-source numbers found for RepoHyper, GraphCodeAgent, RANGER or StackRepoQA (abstracts give no figures; full texts not read).
- No published graph-tool result on VLoc-Bench, VulnGym, SWE-Explore or SWE-bench-Live MultiLang localization was found.
- The "companion paper" that defines and validates hop depth for 2610.33958 was not identified.
- Whether OpenHands ships a native graph tool: not verified (only its use as a LocAgent baseline is confirmed).

---

## Key Question 3: Which tasks could reuse Orion's existing graph build with little adaptation, and which need execution environments?

### Takeaway
Any benchmark that ships (repo, base commit, issue or CWE text, gold files/functions) can be scored with only a
checkout plus Orion's static build: LocBench, SWE-bench Lite/Verified/Live/Pro-public/Multilingual/PolyBench/Multi-SWE
localization (gold from the reference patch), Long Code Arena bug localization, MULocBench, SWE-Explore,
IssueLoc-Bench, VLoc-Bench and VulnGym. Language coverage is the main filter: Joern covers Java, JS/TS, Python, Go and
C/C++, which includes all of SWE-PolyBench, most of Multi-SWE-bench and ~92% of VLoc-Bench tasks, but not Rust.
Execution environments (per-task Docker images) are needed only to score resolution (SWE-bench family), test-based
generation (DevEval, EvoCodeBench, CoderEval, REPOCOD, RepoExec, ExecRepoBench, SecRepoBench) and test-driven fault
localization (Defects4J/BugsInPy, if spectrum-based signals are used).

### Cited Findings
- Orion's graph: Joern -> Neo4j CPG (files, methods, calls, data flows) plus a vector index of code chunks; read-only Cypher, semantic_search and get_schema tools; Joern frontends selected by markers (`package.json` jssrc, `go.mod` golang, `pom.xml` javasrc, Python markers pythonsrc); streaming build reuses `cpg.bin` and streamed a 427-file C# repo at 493 MB peak RSS; scan_id hashes the absolute path — [C:/Users/h/Downloads/orion/CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md).
- Orion's graph has a known omission: calls nested in arrow functions assigned to object properties get no `CONTAINS_CALL` edge, so file attribution must be checked against source — [C:/Users/h/Downloads/orion/CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md).
- Orion's own eval found that a fix commit visible via `.git` in checkouts is an answer leak — [C:/Users/h/Downloads/orion/CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md); and "Shortcutting the Fix" found shortcut attempts (retrieving upstream solutions, exploiting local Git history, recalling memorized code) in 60.7-90.1% of trajectories on SWE-bench Multilingual and 55.5-89.1% on DeepSWE, with up to 72.1% of tasks resolved with an exploit's help; disabling network did not curb attempts; telling agents to solve independently cut attempts to 9.4-14.2% / 0.9-10.3% — [arXiv 2609.06780](https://arxiv.org/abs/2609.06780).
- Execution-based benchmarks ship per-task environments: SWE-bench-Live gives a Docker image per task — [arXiv 2505.23419](https://arxiv.org/abs/2505.23419); RepoLaunch builds MultiLang/Windows environments with >=98% success, 82% LM API cost and 78% Docker storage savings by reusing one built commit per repo — [README](https://raw.githubusercontent.com/microsoft/SWE-bench-Live/main/README.md); SWE-PolyBench, Multi-SWE-bench, SWE-bench-java ship Docker-based harnesses — [arXiv 2504.08703](https://arxiv.org/abs/2504.08703), [arXiv 2504.02605](https://arxiv.org/abs/2504.02605), [arXiv 2408.14354](https://arxiv.org/abs/2408.14354).
- Localization-only benchmarks score from static gold labels: LocBench Acc@k from patches — [HTML](https://arxiv.org/html/2503.09089v2); LCA bug localization rows carry `base_sha`, `changed_files`, `diff` — [HF](https://huggingface.co/datasets/JetBrains-Research/lca-bug-localization); VLoc-Bench pairs vulnerable and patched snapshots and scores File F1 / TNR with read-only access — [HTML](https://arxiv.org/html/2609.15939v1); SWE-Explore derives line-level gold from successful trajectories — [arXiv 2606.07297](https://arxiv.org/abs/2606.07297).
- SWE-PolyBench already computes file-level and CST-node-level (class/function) retrieval precision/recall from agent patches — [HTML](https://arxiv.org/html/2504.08703v3).
- Language mixes: VLoc-Bench Go 43%, Maven 21%, npm 18%, pip 10%, Rust 8%, Composer 1 task — [HTML](https://arxiv.org/html/2609.15939v1); Multi-SWE-bench repos Rust 10, JS 6, C++ 5, C 3, Go 3, Java 3, TS 3 — [HF](https://huggingface.co/datasets/ByteDance-Seed/Multi-SWE-bench); SWE-PolyBench Java 165, JS 1,017, TS 729, Python 199 — [arXiv 2504.08703](https://arxiv.org/abs/2504.08703); SWE-bench Multilingual covers Ruby, Rust, PHP, Java, Go, C, JS, TS, C++ — [HF](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual); SWE-bench-Live/MultiLang covers C/C++, C#, Java, TS/JS, Go, Rust — [README](https://raw.githubusercontent.com/microsoft/SWE-bench-Live/main/README.md).
- Indexing cost is a real failure mode: CodexGraph excluded some Sympy SWE-bench samples because indexing ran out of memory — [HTML](https://arxiv.org/html/2408.03910v3); RepoGraph's graphs average 1,419.3 nodes / 26,392.1 edges per SWE-bench repo — [HTML](https://arxiv.org/html/2410.14684v2).
- CrossCodeEval's raw repositories are available only by emailing the authors — [repo](https://github.com/amazon-science/cceval); CodeQueries supplies file-level context, not whole repos — [arXiv 2209.08372](https://arxiv.org/abs/2209.08372); CodeSearchNet is a function corpus — [arXiv 1909.09436](https://arxiv.org/abs/1909.09436).
- VLoc-Bench agents get only a read-only terminal (ls, find, grep, sed, cat), 15 commands, 20 turns; no network or execution — [HTML](https://arxiv.org/html/2609.15939v1).

### Inferences
- Low-adaptation candidates (checkout + `orion` build + an agent that outputs a ranked file/function list): LocBench, SWE-bench Lite/Verified localization, SWE-bench-Live (Python and MultiLang minus Rust), SWE-bench Pro public (Go/Python/JS/TS), SWE-PolyBench500, Multi-SWE-bench (minus Rust repos), LCA bug localization (Python/Java), MULocBench, IssueLoc-Bench, SWE-Explore (minus non-Joern languages), VLoc-Bench (~92% of tasks), VulnGym, RepoQA. Each needs: strip `.git` (answer leak, per Orion's own finding and the shortcut paper), map Orion's CpgMethod/CpgFile output to the benchmark's gold format (file path + qualified function name), and a graph per (repo, commit), i.e. up to 500 builds for Verified. Orion's existing `eval/` matcher (file + function or line range) is close to the Acc@k / File F1 these benchmarks use.
- VLoc-Bench is the most natural first target: its input (CWE description + unfamiliar repo), its two phases (vulnerable vs patched snapshot) and its metrics match Orion's scan and Orion-LM's vulnerable/patched-twin design, and its interface (read-only, bounded commands) can be swapped for Orion's read-only graph tools as a clean A/B arm.
- Medium adaptation: completion benchmarks (CrossCodeEval, RepoBench, RepoEval, ReccEval) need the target file truncated before the build so the graph does not contain the answer, and a completion harness Orion lacks; CodeQueries needs only a Python file per sample (graph adds little at file scope).
- High adaptation / execution required: resolve-rate on any SWE-bench variant (Docker per task, many GB), DevEval/EvoCodeBench/CoderEval/REPOCOD/RepoExec/ExecRepoBench (repo envs to run tests), SecRepoBench (C/C++ builds), Defects4J/BugsInPy if spectrum-based FL is used (static scoring against gold methods is possible without execution). SWE-Flux's gold answers are precomputed, so scoring is static even though the benchmark is execution-grounded.
- Language gaps: Rust (Multi-SWE-bench, VLoc 8%, MultiLang), Ruby/PHP (Multilingual), Kotlin (LCA), C# (CrossCodeEval, MultiLang) need Joern frontends whose support in Orion is not verified here (Orion's notes show a C# repo streamed, so C# likely works).
- JS-heavy benchmarks (SWE-PolyBench's 1,746 JS/TS tasks, SWE-bench Multimodal) will hit Orion's arrow-function CONTAINS_CALL omission; localization by function name could be undercounted there unless the agent reads source.

### Gaps
- Joern frontend support for Rust, Ruby, PHP, Kotlin was not checked in a primary source this session.
- Per-task Docker image sizes and total storage for SWE-bench-Live/Pro/PolyBench were not found.
- Whether RepoBench v1.1 ships full repositories (vs. extracted snippets) was not verified.

---

## Key Question 4: Which benchmarks are refreshed or time-split to limit contamination (latest frontier cutoffs ~May 2026)?

### Takeaway
Refreshed or private-by-design: SWE-bench-Live (Python +50 tasks/month; MultiLang 1,077 and Windows 66 tasks as of
Aug 2026), SWE-rebench (continuous decontaminated leaderboard), SWE-Bench Pro's held-out and commercial sets (private),
the K Prize (post-deadline issues), EvoCodeBench (evolving pipeline), LocBench (update tooling, but its post-Oct-2024
data is now before every current cutoff), CoREB (timed releases), SWE-Flux (perturbation variants), LiveCodeBench
(non-repo). VLoc-Bench includes 21 advisories from 2026. Everything else is static and should be treated as possibly
memorized: there is direct evidence of memorization on SWE-bench Verified (76% buggy-file identification from the issue
alone vs up to 53% on other repos), and OpenAI dropped Verified in February 2026.

### Cited Findings
- SWE-bench-Live: tasks from issues created since 2024; each month 50 newly verified issues are added to the `full` split while `lite` and `verified` stay frozen (process finalized 2025-09-17, data then through 08/2025); MultiLang (released 2026-01-01) reached 1,077 tasks / 431 repos / 8 languages and Windows 66 tasks / 48 repos by 2026-08-21; HF dataset last modified 2026-09-04 — [arXiv 2505.23419](https://arxiv.org/abs/2505.23419); [README](https://raw.githubusercontent.com/microsoft/SWE-bench-Live/main/README.md); [HF](https://huggingface.co/datasets/SWE-bench-Live/SWE-bench-Live).
- SWE-rebench: continuous supply of fresh tasks for a contamination-free benchmark; some models' SWE-bench Verified results "might be inflated due to contamination" — [arXiv 2505.20411](https://arxiv.org/abs/2505.20411); leaderboard [swe-rebench.com](https://swe-rebench.com).
- SWE-Bench Pro: held-out (12 repos) and commercial (18 proprietary repos) sets not publicly accessible; designed as "contamination-resistant" — [arXiv 2509.16941](https://arxiv.org/abs/2509.16941); OpenAI recommends Pro as the successor to Verified — [OpenAI](https://openai.com/en/index/why-we-no-longer-evaluate-swe-bench-verified).
- K Prize round 1 used only issues flagged after the 2025-03-12 deadline; top score 7.5% vs ~75% on Verified at the time — [Yahoo/TechCrunch](https://finance.yahoo.com/news/ai-coding-challenge-just-published-000000906.html); [Konwinski](https://andykonwinski.com/2025/03/12/kprize-next-steps.html).
- LocBench: issues after Oct 2024, chosen to post-date most LLM releases; tooling to keep adding examples — [HTML](https://arxiv.org/html/2503.09089v2).
- EvoCodeBench: evolving benchmark with an automatic update pipeline; first release EvoCodeBench-2403 — [arXiv 2404.00599](https://arxiv.org/abs/2404.00599).
- LiveCodeBench: continuously collects new contest problems (initial window May 2023 to May 2024) — [arXiv 2403.07974](https://arxiv.org/abs/2403.07974).
- CoREB: contamination-limited, timed releases, counterfactually rewritten LiveCodeBench problems — [arXiv 2605.04615](https://arxiv.org/abs/2605.04615).
- SWE-Flux: oracle-harvesting with input perturbation regenerates valid variants for almost 90% of instances, and variants are harder — [arXiv 2609.28449](https://arxiv.org/abs/2609.28449).
- VLoc-Bench: advisories disclosed 2016-2026, including 21 from 2026 — [HTML](https://arxiv.org/html/2609.15939v1).
- SWE-bench Multilingual PRs date 2017 to 2025 (static) — [HF](https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual).
- Memorization evidence: models identify buggy file paths from issue text alone with up to 76% accuracy on SWE-bench Verified vs up to 53% on repos outside SWE-bench; 5-gram verbatim reproduction up to 35% on Verified/Full vs up to 18% elsewhere — [arXiv 2506.12286](https://arxiv.org/abs/2506.12286).
- OpenAI (2026-02-23): all frontier models tested had seen some Verified problems and solutions; Verified SOTA went 74.9% -> 80.9% in six months — [OpenAI](https://openai.com/en/index/why-we-no-longer-evaluate-swe-bench-verified) (details via search summary; direct fetch returned 403); Epoch: OpenAI audited 27.6% of tasks, 59.4% of those had flawed tests; 100% of tasks and solutions public — [Epoch](https://epoch.ai/benchmarks/swe-bench-verified/review).
- Counterpoint: a matched-control re-evaluation of a reported SWE-bench Verified file-localization gap found GPT-4.1's +10.0-point Top-3 gap had 95% intervals including zero (inconclusive), and argues stronger contamination claims need provenance evidence and validated scorers — [arXiv 2610.05830](https://arxiv.org/abs/2610.05830).
- StackRepoQA found high scores often come from verbatim reproduction of Stack Overflow answers — [arXiv 2603.26567](https://arxiv.org/abs/2603.26567).

### Inferences
- Against cutoffs of ~May 2026, only tasks created after 2026-05-31 are clean: candidates are SWE-bench-Live monthly additions from June 2026 on, SWE-rebench's recent months, the 2026 slice of VLoc-Bench (exact months unverified), and any new LocBench-style examples built with LocAgent's update tooling or the SWE-bench-Live/RepoLaunch or SWE-rebench pipelines. Orion's existing CVE-mining pipeline (post-2026-05-31 advisories with fix commits) could produce a VLoc-style security-localization set by the same rule.
- For localization, contamination inflates exactly the "no-tool" baseline (file paths recalled from the issue), which shrinks the measured benefit of any retrieval tool on SWE-bench Verified/Lite. A graph-vs-no-graph comparison on Verified therefore likely understates the graph's value; a post-cutoff set is needed to measure it fairly.
- Static benchmarks still useful as references: SWE-bench Lite/Verified (only place with many published graph-localizer numbers), LocBench (Python, comparatively recent), SWE-PolyBench (no dates published).

### Gaps
- Exact creation-date ranges for Multi-SWE-bench, SWE-PolyBench, MULocBench, SWE-Explore, MM-IssueLoc tasks: not stated in the sources read.
- Whether SWE-bench-Live's monthly Python additions actually continued through Sep/Oct 2026 (HF last modified 2026-09-04 suggests yes; not verified per month).
- The month distribution of VLoc-Bench's 21 2026 advisories (before or after May 2026) was not read.
- K Prize rounds after round 1 and their sizes: not found.
