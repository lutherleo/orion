# Memory, long-context and retrieval (incl. GraphRAG / text-to-Cypher) benchmarks, and which could test Orion

Scope note for the report writer. Orion = code-security scanner; its Joern code property graph (CPG) in Neo4j plus a vector/keyword index of code chunks is *external structured memory* that Claude agents (and Orion-LM) query through read-only tools (`run_cypher`, `semantic_search`, `get_schema`) instead of reading the whole repo ([CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md)). Orion's own eval already has "plain" frontier arms (plain-sonnet5 / plain-opus5 / plain-gpt) and a headline of efficiency (vulns per 1M tokens) + false-alarm rate ([CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md)).

Conventions used below:
- **Fit** (assessment, in each "Inferences" table): **D** = could test Orion or one of its components directly (as-is or with a thin adapter); **A** = only a methodology analogy (borrow the design, not the data); **N** = not useful for Orion.
- **†** = facts recalled from the linked primary paper/repo, not re-fetched in this session (2026-10-31). Unmarked facts were checked this session against the cited page. Numbers not found are "not stated".
- **Cutoff check**: latest frontier training cutoff ≈ May 2026 (Opus 5). Anything public before that may be in training data; synthetic/regenerable benchmarks resist this.

---

## (a) Long-context benchmarks: what exists, and can any of them test Orion?

### Takeaway
Long-context benchmarks are plentiful and mature (NIAH → RULER/HELMET/NoLiMa/LongBench v2 → 2026's LongBench Pro and PredicateLongBench), and their shared headline is that *effective* context is far shorter than *claimed* context, especially when the needle shares no words with the question. None of them can be run through Orion directly (they are prose or synthetic text with no code graph to build), but several are excellent design templates, and a few have code-like sub-tasks (RULER variable tracking, Michelangelo Latent List, InfiniteBench Code.Debug/Run, LongBench v2 code-repository subset).

### Cited Findings

| Benchmark | Year / maintainer | What, size, lengths, format, scoring | Published results (as stated) | Links | Released |
|---|---|---|---|---|---|
| Needle-in-a-Haystack (NIAH) | 2023, Greg Kamradt (community) | One fact inserted at varied depths of Paul Graham essays; sweep length × depth; heat-map of retrieval success † | Most long-context LMs now score perfectly on vanilla NIAH (HELMET's finding, below). **Superseded / saturated.** | [repo](https://github.com/gkamradt/LLMTest_NeedleInAHaystack) | Nov 2023 † |
| RULER | 2024, NVIDIA (COLM 2024 †) | 13 synthetic tasks in 4 categories: retrieval (multi-key / multi-value NIAH), multi-hop tracing (incl. **variable tracking**: chains of variable assignments), aggregation, QA; configurable length (4K–128K+), regenerable; reports "effective context length" † | 17 long-context LMs: near-perfect vanilla NIAH but "large performance drops" with length; all claim ≥32K, "only half" are satisfactory at 32K † | [paper](https://arxiv.org/abs/2404.06654), [repo](https://github.com/NVIDIA/RULER) | Apr 2024 |
| LongBench (v1) | 2023, THUDM / Tsinghua (ACL 2024) | 21 datasets, 6 categories (single-doc QA, multi-doc QA, summarization, few-shot, synthetic, **code completion** via LCC and RepoBench-P); bilingual EN/ZH; avg ≈6.7K words EN † | Not re-verified. **Superseded by v2 / Pro.** | [paper](https://arxiv.org/abs/2308.14508), [repo](https://github.com/THUDM/LongBench) | Aug 2023 |
| LongBench v2 | 2024, THUDM (ACL 2025) | 503 hard multiple-choice questions, 8K–2M words, 6 categories incl. **code repository understanding** and long structured data; accuracy † | Human experts 53.7% (15-min limit); best direct-answer model 50.1%; o1-preview with longer reasoning 57.7% † | [paper](https://arxiv.org/abs/2412.15204), [leaderboard](https://longbench2.github.io) | Dec 2024 |
| LongBench Pro | 2026 | 1,500 bilingual samples, 8K–256K tokens; 46 long-context LLMs evaluated | Effective context length "typically shorter than the claimed"; pronounced cross-lingual misalignment; gains from extending effective context exceed gains from scaling model size | [paper](https://arxiv.org/abs/2601.02872) | 6 Jan 2026 |
| InfiniteBench (∞Bench) | 2024, OpenBMB / Tsinghua (ACL 2024 †) | 12 tasks, average length >100K tokens; includes **Code.Debug** (find the bug in a large code file) and **Code.Run** (simulate execution), Retrieve.PassKey/Number/KV, Math.Find, En/Zh QA, summarization † | Not re-verified | [paper](https://arxiv.org/abs/2402.13718), [repo](https://github.com/OpenBMB/InfiniteBench) | Feb 2024 |
| HELMET | 2024, Princeton (ICLR 2025 †) | 7 application categories (synthetic recall, RAG, re-ranking, citation generation, long QA, summarization, many-shot ICL), lengths controllable to 128K † | Study of 59 LCLMs: synthetic tasks like NIAH "do not reflect" downstream performance; categories correlate weakly; open models lag closed ones on full-context reasoning, gap widens with length † | [paper](https://arxiv.org/abs/2410.02694), [repo](https://github.com/princeton-nlp/HELMET) | Oct 2024 |
| BABILong | 2024, AIRI et al. (NeurIPS 2024 D&B †) | 20 bAbI reasoning tasks hidden in PG19 text; lengths up to millions of tokens; regenerable † | Popular LLMs "effectively utilize only 10–20% of the context"; RAG reaches ~60% on single-fact QA independent of length; fine-tuned recurrent memory transformers process up to 50M tokens † | [paper](https://arxiv.org/abs/2406.10149), [repo](https://github.com/booydar/babilong) | Jun 2024 |
| NoLiMa | 2025, Adobe Research et al. (ICML 2025) | NIAH variant whose needle and question have **minimal lexical overlap**, so models must infer latent associations | 13 LLMs claiming ≥128K: fine under 1K, but at 32K, 11 fall below 50% of their short-context baseline; GPT-4o 99.3% → 69.7% at 32K; reasoning models and CoT do not fix it | [paper](https://arxiv.org/abs/2502.05167) | 7 Feb 2025 (v3 9 Jul 2025) |
| LongProc | 2025, Princeton † | Long *procedural generation*: 6 tasks (e.g., HTML→TSV, pseudocode→code, path traversal, ToM tracking, countdown, travel planning) with 0.5K/2K/8K-token outputs † | Models degrade sharply at 8K-token outputs † | [paper](https://arxiv.org/abs/2501.05414) | Jan 2025 † |
| Michelangelo | 2024, Google DeepMind † | "Latent Structure Queries": **Latent List** (track a Python list through a sequence of operations hidden in irrelevant text), MRCR (multi-round co-reference), IDK (answerable vs not); up to 1M tokens † | Not re-verified | [paper](https://arxiv.org/abs/2409.12640) | Sep 2024 |
| OpenAI-MRCR | 2025, OpenAI | Open version of MRCR (from Gemini 1.5 report and Michelangelo): distinguish 2/4/8 near-identical needles; exact match | Aggregator lists self-reported MRCR v2 8-needle scores (e.g., a Gemini Flash at 54.0%) — unverified | [HF dataset](https://huggingface.co/datasets/openai/mrcr), [secondary](https://benchmarklist.com/benchmarks/mrcr_v2_8_needle/) | Apr 2025 (with GPT-4.1) † |
| Graphwalks | 2025, OpenAI | **A graph written as an edge list in the prompt**; tasks BFS (nodes at depth k) and parents; F1 vs gold node set; reported split <128K / >128K; all prompts and solutions public; official data at 128K and 1M only | Aggregator: GPT-4.1 25.0% on parents >128K, GPT-4.1 mini 11.0%, nano 5.6% (unverified) | [HF dataset](https://huggingface.co/datasets/openai/graphwalks), [Epoch review](https://epoch.ai/benchmarks/graphwalks/review), [ATLAS note on lengths](https://arxiv.org/pdf/2605.28079) | Apr 2025 † |
| LOFT | 2024, Google DeepMind (NAACL 2025 Findings) | 6 categories, 35 datasets (text, visual, audio): retrieval, RAG, **SQL-like** reasoning, many-shot ICL; auto-generated context lengths up to millions of tokens; Recall@1, subspan EM, SQL execution accuracy | LCLMs "rival" SOTA retrieval and RAG systems without training for it, but struggle with compositional reasoning needed in SQL-like tasks | [paper](https://arxiv.org/abs/2406.13121), [repo (Apache-2.0)](https://github.com/google-deepmind/loft) | Jun 2024 |
| ZeroSCROLLS (and SCROLLS) | 2023, Tel Aviv Univ. et al. † | 10 zero-shot long-text tasks (summarization, QA, aggregation) † | Not re-verified. **Superseded** by HELMET/LongBench v2 | [ZeroSCROLLS](https://arxiv.org/abs/2305.14196), [SCROLLS](https://arxiv.org/abs/2201.03533) | May 2023 / Jan 2022 |
| L-Eval | 2023, Fudan et al. (ACL 2024 †) | ~20 sub-tasks over long documents, human-labelled QA, length-instruction-enhanced eval † | Not re-verified. **Superseded** | [paper](https://arxiv.org/abs/2307.11088) | Jul 2023 |
| Loong | 2024 (EMNLP 2024 †) | Extended multi-document QA where *every* document is needed: spotlight locating, comparison, clustering, chain-of-reasoning; several length bands up to ~250K † | Not re-verified | [paper](https://arxiv.org/abs/2406.17419) | Jun 2024 |
| LV-Eval / NeedleBench / LooGLE | 2023–2024 † | LV-Eval: 5 length levels to 256K with confusing-fact insertion; NeedleBench (OpenCompass): bilingual multi-needle reasoning; LooGLE: long-dependency QA over post-2022 documents † | Not re-verified | [LV-Eval](https://arxiv.org/abs/2402.05136), [NeedleBench](https://arxiv.org/abs/2407.11963), [LooGLE](https://arxiv.org/abs/2311.04939) | 2023–2024 |
| HELM Long Context | 2025, Stanford CRFM | Long-context leaderboard within HELM (details not fetched) | not stated here | [post](https://crfm.stanford.edu/2025/09/29/helm-long-context.html) | 29 Sep 2025 |
| Context Rot (Chroma tech report) | 2025, Chroma | Controlled experiments on 18 LLMs (GPT-4.1, Claude 4, Gemini 2.5, Qwen3): vary input length with task held fixed; separates *distractors* (topically related near-misses) from irrelevant filler; open code | Performance "grows increasingly unreliable" with input length; NIAH scores create a false impression of uniform context use | [report](https://research.trychroma.com/context-rot) | Jul 2025 |
| ATLAS | 2026 | "All-round Testing of Long-context Abilities across Scales"; re-synthesised Graphwalks at lengths other than 128K/1M | not fetched | [paper](https://arxiv.org/pdf/2605.28079) | May 2026 |
| PredicateLongBench | 2026 | Find the longest contiguous span meeting a constraint (e.g., lexicographic order) in synthetic and natural text; difficulty axes; no LLM judge | Frontier models struggle as difficulty is scaled along the axes | [paper](https://arxiv.org/abs/2607.08284) | 9 Jul 2026 (post-cutoff) |

### Inferences

| Benchmark | Fit | Why (one line) | Contamination (release vs ~May 2026 cutoff) |
|---|---|---|---|
| NIAH | A | Depth × length sweep template; saturated, prose only | Pre-cutoff, but regenerable |
| RULER | A (strong) | Its **variable-tracking** task is literally multi-hop assignment chasing, i.e. dataflow/taint in miniature; copy its "effective length" idea for repos | Pre-cutoff; regenerable, low risk |
| LongBench v1 / v2 / Pro | A (v2 code subset ≈ weak D) | v2's code-repository MCQs could be answered by an agent using Orion's graph instead of the full repo, but no security labels; Pro is the current version | v1/v2 likely seen; Pro (Jan 2026) pre-cutoff |
| InfiniteBench | A (Code.Debug ≈ weak D) | Code.Debug "find the planted bug in a huge file" is the nearest prose-benchmark task to vuln-finding; single file, not a repo | Pre-cutoff |
| HELMET | A | Evidence that synthetic recall does not predict real tasks: argue for real-repo evaluation, not a synthetic code-needle alone | Pre-cutoff |
| BABILong | A | "Fraction of context actually used" metric; RAG-vs-long-context contrast | Regenerable |
| NoLiMa | A (strong) | Template for a **no-lexical-overlap** code test: rename identifiers so grep/keyword search fails; does Cypher over CPG still find the flow? | Pre-cutoff |
| LongProc | N/A-weak | Output-length stress; Orion's outputs are short findings | — |
| Michelangelo (Latent List) | A | Tracking a Python list through operations ≈ tracking taint through statements | Pre-cutoff |
| OpenAI-MRCR | A | Near-duplicate disambiguation ≈ vulnerable vs patched twin functions | Public answers |
| Graphwalks | A (strong) | The cleanest analog of Orion's thesis: the *same graph* either in the prompt as text or in a DB behind a tool; Orion could run Graphwalks graphs through Neo4j + Cypher to show tool access beats in-context traversal | Prompts and solutions public |
| LOFT | A (strong) | Exactly the "long context vs retrieval vs SQL" question Orion poses (whole repo vs semantic_search vs Cypher) | Pre-cutoff; lengths regenerable |
| ZeroSCROLLS / L-Eval / Loong / LV-Eval / NeedleBench / LooGLE | N–A | Prose QA/summarization; superseded | Pre-cutoff |
| Context Rot | A | Distractor design (topically related near-misses) maps to patched-twin / sanitized-flow distractors | Pre-cutoff |
| LongBench Pro / ATLAS / PredicateLongBench | N–A | Current long-context leaderboards for picking a plain-arm model; PredicateLongBench is post-cutoff | Pro/ATLAS pre-cutoff, PredicateLongBench post |

### Gaps
- RULER's exact effective-length threshold and per-model numbers, HELMET's model count (59) and BABILong's maximum length were recalled, not re-fetched.
- No primary OpenAI page for MRCR/Graphwalks was fetched; per-model Graphwalks/MRCR numbers come from an aggregator and are unverified.
- HELM Long Context and ATLAS contents were not read beyond titles/snippets.
- Licenses for most long-context sets were not verified (LOFT is Apache-2.0).

---

## (b) Agent / long-term memory benchmarks: what they measure, and do they say anything about Orion?

### Takeaway
Agent-memory benchmarks (LongMemEval, LoCoMo, MemoryAgentBench, BEAM, MemBench, MSC/DMR, etc.) are about *chat histories* (sessions with a user), not codebases, so none can test Orion directly; LoCoMo and DMR are near-saturated and the vendor numbers (Mem0, Zep, Letta) are disputed. Their value to Orion is methodological: full-context vs retrieval vs memory-agent baselines, oracle-evidence settings, per-competency breakdowns, abstention, and reporting latency and token cost next to accuracy. Letta's result (a plain filesystem + grep agent rivalled a dedicated memory layer on LoCoMo) is the most important cautionary analog: Orion must beat a strong "agent with file tools" baseline, not just a naive one.

### Cited Findings

| Benchmark / evaluation | Year / maintainer | What, size, format, scoring | Published results (as stated) | Links | Released |
|---|---|---|---|---|---|
| LongMemEval | 2024, UCLA / Tencent AI (ICLR 2025) | 500 questions embedded in "freely scalable" user–assistant chat histories; 5 abilities: information extraction, multi-session reasoning, temporal reasoning, knowledge updates, **abstention**; LongMemEval_S ≈115K tokens and _M ≈1.5M tokens, plus an oracle (evidence-only) setting † | Commercial chat assistants and long-context LLMs show a **30% accuracy drop** across sustained interactions; proposes session decomposition, fact-augmented key expansion, time-aware query expansion (gains not quantified in abstract) | [paper](https://arxiv.org/abs/2410.10813), [repo](https://github.com/xiaowu0162/LongMemEval) † | 14 Oct 2024 (v2 4 Mar 2025) |
| LoCoMo | 2024, Snap Research et al. (ACL 2024 †) | Very long multi-session conversations (≈300 turns, ≈9K tokens, up to 35 sessions †); QA (single-hop, multi-hop, temporal, open-domain, adversarial), event summarization, multimodal dialogue † | Vendor scores now ~80–95% (below); critics say LoCoMo never scores knowledge updates and is close to saturated | [paper](https://arxiv.org/abs/2402.17753), [repo](https://github.com/snap-research/locomo) †, [critique](https://dev.to/gde03/the-ai-memory-benchmark-everyone-quotes-forbids-saying-i-dont-know-o1n) | Feb 2024 |
| MemoryAgentBench | 2025, UC San Diego (Hu, Wang, McAuley) | Converts long-context datasets (and new ones) into **incremental multi-turn** feeds; 4 competencies: accurate retrieval, test-time learning, long-range understanding, selective forgetting / conflict resolution; evaluates long-context, RAG and agents "with external memory modules and tool integration" | "Current methods fall short of mastering all four competencies" | [paper](https://arxiv.org/abs/2507.05257) | 7 Jul 2025 (v4 28 Jun 2026); a secondary listing says ICLR 2026 ([search snippet](https://api2.openreview.net/pdf/099e0660ae13bb4cc8499c4e1691f5ee1eea6415.pdf)), arXiv page states no venue |
| MemBench | 2025, Tan et al. (ACL Findings 2025 †) | Memory of LLM agents along effectiveness, efficiency, capacity; participation vs observation scenarios (from a secondary comparison table) | not stated here | [secondary table in AgentMemBench](https://arxiv.org/pdf/2608.00009) | 2025 |
| MSC (Multi-Session Chat) | 2021, Facebook AI ("Beyond Goldfish Memory", ACL 2022 †) | Crowdsourced multi-session (up to 5) open-domain dialogues with persona summaries † | Not re-verified | [paper](https://arxiv.org/abs/2107.07567) | Jul 2021 |
| DMR (Deep Memory Retrieval) | 2023, MemGPT/Letta team | MSC-derived QA over prior sessions; MemGPT's primary memory metric; MemGPT also ran nested key-value retrieval and multi-doc QA † | MemGPT 93.4%, Zep 94.8% (near ceiling) | [MemGPT](https://arxiv.org/abs/2310.08560), [Zep](https://arxiv.org/abs/2501.13956) | Oct 2023 |
| Zep evaluation | 2025, Zep (Graphiti **temporal knowledge graph** memory) | DMR + LongMemEval | DMR 94.8% vs MemGPT 93.4%; LongMemEval accuracy up to +18.5% over baselines; latency −90% vs baselines | [paper](https://arxiv.org/abs/2501.13956) | 20 Jan 2025 |
| Mem0 evaluation (incl. graph variant Mem0g) | 2025, Mem0 | LoCoMo vs 6 baseline families (memory-augmented systems, RAG with varied chunk/k, **full-context**, open-source memory, a proprietary system, a memory platform); LLM-as-judge (J) | +26% relative J over OpenAI's memory; graph variant ≈+2% over base Mem0; **91% lower p95 latency and >90% token savings vs full context** | [paper](https://arxiv.org/abs/2504.19413) | 28 Apr 2025 |
| Mem0–Zep–Letta dispute | 2025–2026, vendor blogs | Implementation and category-handling disputes on LoCoMo | Mem0 paper J 66.88 vs Zep 65.99; Zep says a correct Zep beats Mem0 by ~10%; Mem0 says Zep's 84% is inflated (adversarial category); **Letta: plain files + grep-style tools = 74.0% with GPT-4o mini vs Mem0's reported 68.5% graph variant**; 2026 self-reports (Mem0 ~91.6–92.5%, Zep up to 94.7%) use different models/harnesses and are not comparable | [Zep rebuttal](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/), [Mem0 blog](https://mem0.ai/blog/ai-memory-benchmarks), [secondary summary](https://www.agenticwire.news/article/mem0-zep-letta-agent-memory) | 2025–2026 |
| A-MEM | 2025 † | Zettelkasten-style agentic memory, evaluated on LoCoMo † | Not re-verified | [paper](https://arxiv.org/abs/2502.12110) | Feb 2025 |
| PerLTQA | 2024 † | Personal long-term memory QA combining semantic and episodic memories of characters † | Not re-verified | [paper](https://arxiv.org/abs/2402.16288) † | Feb 2024 |
| MemoryBank (SiliconFriend) | 2023, (AAAI 2024 †) | Long-term memory with an Ebbinghaus-curve forgetting mechanism; companion-chat evaluation † | Not re-verified | [paper](https://arxiv.org/abs/2305.10250) | May 2023 |
| EpBench (episodic memory) | 2025, ICLR 2025 † | Synthetic episodic events (time, place, entities) for recall/ordering questions † | Not verified this session (search found nothing) | [paper (ID recalled)](https://arxiv.org/abs/2501.13121) † | Jan 2025 † |
| StoryBench | 2025 | Listed in a comparison table as an interactive-fiction game benchmark with human annotation, "80+ trajectories" | not stated | [secondary table](https://arxiv.org/pdf/2608.00009) | 2025 |
| BEAM ("Beyond a Million Tokens") | 2025, Univ. Alberta / UMass Amherst (ICLR 2026) | Generator of long coherent conversations; 100 conversations, 2,000 validated questions, scales 128K / 500K / 1M / 10M tokens; 10 memory abilities; topics include **coding** and math | 1M-context models degrade as conversations grow, with and without RAG; proposed LIGHT (episodic + working memory + scratchpad) +3.5% to +12.69% over strongest baselines | [paper](https://arxiv.org/abs/2510.27246), [ICLR](https://iclr.cc/virtual/2026/poster/10006595), [data](https://huggingface.co/datasets/Mohammadta/BEAM-10M) | 31 Oct 2025 |
| EMemBench | 2026 | Programmatic long-term-memory benchmark for agents via interactive games | not stated | [paper](https://arxiv.org/pdf/2601.16690) | Jan 2026 |
| AgentMemBench | 2026 | 5 memory-strategy families × 3 task types, 491 annotated question turns from 3 public multi-session datasets; measures **efficiency** (says MemoryAgentBench does not) | not stated here | [paper](https://arxiv.org/pdf/2608.00009) | Aug 2026 (post-cutoff) |

### Inferences

| Benchmark | Fit | Why | Contamination |
|---|---|---|---|
| LongMemEval | A (strong) | Copy (1) the **oracle-evidence** setting → give the agent only the vulnerable function + callers (Orion already cuts this context for `--verify-context`), separating retrieval failure from reasoning failure; (2) **abstention** → Orion's patched-twin "say nothing" cases | Pre-cutoff (Oct 2024) |
| LoCoMo / DMR / MSC | N (A for baseline design) | Chat persona memory; saturated, disputed | Pre-cutoff |
| MemoryAgentBench | A (strong) | Its 4-competency breakdown and "incremental feed" idea map to Orion: accurate retrieval (find the sink), long-range understanding (cross-file flow), selective forgetting ≈ re-scan after a patch (does memory update when code changes?) | Pre-cutoff (v1 Jul 2025) |
| Mem0 / Zep / Letta evaluations | A (strong) | Shows the standard reporting triple: accuracy + p95 latency + tokens vs a **full-context baseline**; Letta's filesystem result says the baseline that matters is "agent with grep/read tools", which is what Orion's plain arms are | Pre-cutoff |
| Zep (temporal KG) / Mem0g (graph) | A | Only graph-structured agent memories in this group; conversational facts, not code | Pre-cutoff |
| BEAM | A (weak D for coding conversations) | Generator + 10M-token scale; its coding conversations are chat about code, not a repository | Oct 2025, pre-cutoff; regenerable |
| MemBench / PerLTQA / MemoryBank / EpBench / StoryBench / EMemBench / A-MEM | N | Persona, episodic or game memory | Pre-cutoff except EMemBench (Jan 2026, still pre-cutoff) |
| AgentMemBench | A | Efficiency-aware memory comparison; post-cutoff | Aug 2026, post-cutoff |

### Gaps
- MemBench, StoryBench and EpBench primary papers were not retrieved (search returned only a secondary table, or nothing for EpBench); their sizes, IDs and venues are unverified.
- LongMemEval_S/_M token sizes and the oracle setting are from memory of the repo, not re-checked.
- LoCoMo license and exact released size (10 conversations publicly †) not verified.
- Letta's own filesystem post was not fetched; the 74.0% comes from a secondary article.
- No agent-memory benchmark built on code repositories was found (BEAM's coding chats are the closest).

---

## (c) RAG and multi-hop retrieval benchmarks and RAG evaluation frameworks

### Takeaway
Classic RAG/retrieval benchmarks (KILT, BEIR, MTEB, HotpotQA, 2WikiMultiHopQA, MuSiQue, MultiHop-RAG, CRAG, FRAMES, RAGBench) are Wikipedia/news text and heavily contaminated; they cannot test Orion's scanner. The pieces that transfer are: (1) code retrieval sets inside the MTEB family (CoIR) that can directly score Orion's `semantic_search` embedder/hybrid mode; (2) CRAG's design of giving the system **mock APIs** (web + KG search) rather than documents, which is the closest RAG analog to Orion's tool access; (3) the evaluation frameworks (RAGAS, ARES, RAGBench's TRACe) whose context-relevance / faithfulness / utilization metrics map onto "did the agent's Cypher results actually support the finding".

### Cited Findings

| Benchmark / framework | Year / maintainer | What, size, format, scoring | Published results | Links | Released |
|---|---|---|---|---|---|
| KILT | 2020, Facebook AI (NAACL 2021 †) | 11 datasets, 5 knowledge-intensive tasks (fact checking, entity linking, slot filling, open QA, dialogue) over one Wikipedia snapshot; scores answer + provenance † | Not re-verified | [paper](https://arxiv.org/abs/2009.02252), [repo](https://github.com/facebookresearch/KILT) | Sep 2020 |
| BEIR | 2021, UKP/TU Darmstadt et al. (NeurIPS 2021 D&B †) | 18 zero-shot IR datasets, 9 task types; nDCG@10 † | BM25 is a robust baseline; re-ranking / late interaction best on average but costly † | [paper](https://arxiv.org/abs/2104.08663), [repo](https://github.com/beir-cellar/beir) | Apr 2021 |
| MTEB / MMTEB | 2022 / 2025, Hugging Face + community | MTEB: 8 embedding task types, 58 datasets, 112 languages †; MMTEB: massively multilingual extension (500+ tasks †); live leaderboard incl. code retrieval tracks † | No single embedding method dominates all tasks † | [MTEB](https://arxiv.org/abs/2210.07316), [MMTEB](https://arxiv.org/abs/2502.13595), [leaderboard](https://huggingface.co/spaces/mteb/leaderboard) | Oct 2022 / Feb 2025 |
| CoIR (code IR) | 2024 † | Code information-retrieval benchmark (multiple code retrieval tasks/domains), MTEB-compatible † | Not re-verified | [paper](https://arxiv.org/abs/2407.02883) † | Jul 2024 |
| HotpotQA | 2018, CMU/Stanford/Montréal | ~113K Wikipedia multi-hop QA pairs with supporting-fact labels; distractor and full-wiki settings † | Not re-verified | [paper](https://arxiv.org/abs/1809.09600), [site](https://hotpotqa.github.io) | Sep 2018 |
| 2WikiMultiHopQA | 2020 (COLING 2020 †) | Multi-hop QA with evidence triples (reasoning paths) from Wikipedia + Wikidata † | Not re-verified | [paper](https://arxiv.org/abs/2011.01060) | Nov 2020 |
| MuSiQue | 2021, Stony Brook / AI2 (TACL 2022 †) | ~25K composed 2–4-hop questions built to resist single-hop shortcuts † | Not re-verified | [paper](https://arxiv.org/abs/2108.00573) | Aug 2021 |
| MultiHop-RAG | 2024, HKUST (COLM 2024 †) | 2,556 queries over news (Sep–Dec 2023) needing 2–4 docs; inference / comparison / temporal / null queries † | Not re-verified | [paper](https://arxiv.org/abs/2401.15391) | Jan 2024 |
| CRAG | 2024, Meta (KDD Cup 2024; NeurIPS 2024 D&B †) | 4,409 QA pairs + **mock APIs for web and knowledge-graph search**; 5 domains, 8 question types; truthfulness scoring penalises hallucination † | Best LLMs ≤34% alone; straightforward RAG 44%; SOTA industry RAG answers only 63% without hallucination † | [paper](https://arxiv.org/abs/2406.04744) | Jun 2024 |
| FRAMES | 2024, Google † | 824 questions needing 2–15 Wikipedia articles; factuality, retrieval, reasoning † | Gemini-1.5-Pro 0.40 without retrieval → 0.66 with multi-step retrieval † | [paper](https://arxiv.org/abs/2409.12941) | Sep 2024 |
| RAGBench | 2024, Galileo † | ~100K RAG examples across industry domains; **TRACe** metrics (utilization, relevance, adherence, completeness) † | Not re-verified | [paper](https://arxiv.org/abs/2407.11005) | Jul 2024 |
| ARES | 2023, Stanford (NAACL 2024 †) | Automated RAG eval: fine-tuned LLM judges for context relevance, answer faithfulness, answer relevance, with prediction-powered inference on a small human-labelled set † | Not re-verified | [paper](https://arxiv.org/abs/2311.09476) | Nov 2023 |
| RAGAS | 2023 † | Reference-free RAG metrics (faithfulness, answer relevance, context precision/recall) † | n/a (framework) | [paper](https://arxiv.org/abs/2309.15217), [repo](https://github.com/explodinggradients/ragas) | Sep 2023 |

### Inferences

| Benchmark | Fit | Why | Contamination |
|---|---|---|---|
| KILT / HotpotQA / 2Wiki / MuSiQue / MultiHop-RAG / FRAMES | N (A for multi-hop design) | Wikipedia/news QA; but "supporting facts" / "evidence chains" labels are the analog of Orion's ground-truth source→sink path (Orion-LM's `eval/pathgt.py` already proposes path keys) | Heavily pre-cutoff; Wikipedia-based, likely memorised |
| BEIR / MTEB | A | Embedder choice for `semantic_search`; general text, not code | Pre-cutoff |
| CoIR (and MTEB code tracks) | **D (component)** | Can directly score Orion's chunk embedder and `--semantic-mode vector|keyword|hybrid` (RRF fusion) on code retrieval, isolating the semantic layer from the scanner | Pre-cutoff |
| CRAG | A (strong) | Only mainstream RAG benchmark where the system answers through **APIs to a KG**, and that scores hallucination explicitly, like Orion's grounding rule | Pre-cutoff |
| RAGBench TRACe / ARES / RAGAS | A (strong) | "Utilization" and "faithfulness" can be redefined for Orion: fraction of a finding's claims backed by a returned Cypher row, and fraction of retrieved rows actually used; ARES's small-human-set + judge pattern mirrors Orion's judge calibration rule (≥100 labels) | Frameworks, no contamination issue |

### Gaps
- Sizes/licenses for KILT, 2Wiki, CoIR, RAGBench were recalled, not re-verified.
- I did not verify whether the current MTEB leaderboard has a dedicated code benchmark tab and which models top it.

---

## (d) GraphRAG and knowledge-graph retrieval benchmarks — and which test structured/graph memory accessed through tools (closest to Orion)

### Takeaway
GraphRAG benchmarking is young and contested: Microsoft's original evaluation and BenchmarkQED use LLM-judged win rates on "global" sensemaking questions; LightRAG/HippoRAG reuse multi-hop QA; the two 2025 "GraphRAG-Bench" papers are distinct (Xiao et al.: 1,018 college-level questions; Xiang et al.: "when to use graphs", finding GraphRAG often *underperforms* vanilla RAG). Almost all build the graph *from text with an LLM*, unlike Orion's deterministic program-analysis graph. The benchmarks closest to Orion's setup—an agent that **queries a pre-built graph through tools/queries and must ground answers in returned rows**—are GRBench (Graph-CoT's graph-function calls), CRAG (mock KG APIs), CypherBench / Text2Cypher / Mind the Query (execution against Neo4j), STaRK (semi-structured KB retrieval), and in the code world CodexGraph and LocAgent.

### Cited Findings

| Benchmark / evaluation | Year / maintainer | What, size, format, scoring | Published results | Links | Released |
|---|---|---|---|---|---|
| Microsoft GraphRAG ("From Local to Global") | 2024, Microsoft Research | LLM-built entity graph + community summaries for "global sensemaking questions over datasets in the 1 million token range" (podcast transcripts, news †); LLM-judged pairwise win rates on comprehensiveness, diversity, empowerment † | Substantial win-rate gains over naive RAG on global questions † | [paper](https://arxiv.org/abs/2404.16130), [repo](https://github.com/microsoft/graphrag) | Apr 2024 |
| BenchmarkQED (AutoQ / AutoE / AutoD) | 2025, Microsoft Research | Toolkit: AutoQ synthesises queries on a 2×2 local↔global design space; AutoE LLM-judged comprehensiveness/diversity/empowerment/relevance; AutoD dataset prep | Example: 1,397 AP News health articles, 200 queries (50/class); LazyGraphRAG wins especially on global queries; compared against vector RAG across context-window sizes; LazyGraphRAG avoids "prohibitive up-front indexing costs" (author-reported) | [MSR post](https://www.microsoft.com/en-us/research/?p=1140429), [repo](https://github.com/microsoft/benchmark-qed) † | 5 Jun 2025 |
| LightRAG (UltraDomain eval) | 2024, HKU † | Dual-level graph retrieval; evaluated with LLM-judged win rates on UltraDomain (agriculture, CS, legal, mixed) † | Not re-verified | [paper](https://arxiv.org/abs/2410.05779), [repo](https://github.com/HKUDS/LightRAG) | Oct 2024 |
| HippoRAG | 2024, Ohio State (NeurIPS 2024 †) | KG + Personalized PageRank "hippocampal index"; MuSiQue, 2Wiki, HotpotQA † | Up to +20% on multi-hop QA; single-step retrieval matches/beats iterative IRCoT while **10–30× cheaper and 6–13× faster** † | [paper](https://arxiv.org/abs/2405.14831), [repo](https://github.com/OSU-NLP-Group/HippoRAG) | May 2024 |
| HippoRAG 2 ("From RAG to Memory") | 2025, Ohio State (ICML 2025) | Frames RAG as non-parametric continual learning; factual (NQ, PopQA), sense-making (NarrativeQA), associative (MuSiQue, 2Wiki, HotpotQA, LV-Eval) memory | ~+7 F1 on associative tasks over NV-Embed-v2 (e.g., MuSiQue 48.6 vs 45.7, per a secondary summary); average-F1 figures conflict across secondary sources | [paper](https://arxiv.org/abs/2502.14802), [secondary](https://api.emergentmind.com/topics/hipporag) | Feb 2025 |
| GraphRAG-Bench (Xiao et al.) | 2025, Hong Kong PolyU et al. | 1,018 college-level questions, 16 disciplines; multi-hop; evaluates graph construction, retrieval, answer and **rationale** generation; 9 GraphRAG methods | not stated in snippet | [paper](https://arxiv.org/abs/2506.02404), [repo](https://github.com/jeremycp3/GraphRAG-Bench) | Jun 2025 |
| GraphRAG-Bench (Xiang et al., "When to use Graphs in RAG") | 2025 (v3 Feb 2026) | Tasks of increasing difficulty: fact retrieval, complex reasoning, contextual summarization, creative generation; whole-pipeline eval (construction → retrieval → generation); size not in abstract (a secondary listing says 4,076 questions over 2 datasets incl. a medical one — unverified) | Motivated by GraphRAG "frequently underperform[ing] vanilla RAG on many real-world tasks"; gives conditions where graphs help | [paper](https://arxiv.org/abs/2506.05690), [repo](https://github.com/GraphRAG-Bench/GraphRAG-Benchmark), [secondary](https://www.alphaxiv.org/benchmarks/the-hong-kong-polytechnic-university/graphrag-bench) | 6 Jun 2025; v2 7 Oct 2025; v3 22 Feb 2026 |
| WebQSP | 2016, Microsoft (ACL 2016 †) | 4,737 Freebase questions with SPARQL parses † | Not re-verified | [paper](https://aclanthology.org/P16-2033/) | 2016 |
| CWQ (ComplexWebQuestions) | 2018, Tel Aviv Univ. † | 34,689 complex Freebase questions (compositions, conjunctions, superlatives) † | Not re-verified | [paper](https://arxiv.org/abs/1803.06643) | Mar 2018 |
| MetaQA | 2017/2018 (AAAI 2018 †) | 400K+ 1/2/3-hop questions over the WikiMovies KB † | Not re-verified (largely saturated) | [paper](https://arxiv.org/abs/1709.04071) | Sep 2017 |
| STaRK | 2024, Stanford SNAP (NeurIPS 2024 D&B †) | Retrieval over **semi-structured** KBs (text + relations): product search (Amazon), academic (MAG), precision medicine (PrimeKG) † | Current retrieval systems struggle with combined textual+relational queries † | [paper](https://arxiv.org/abs/2404.13207), [repo](https://github.com/snap-stanford/stark) | Apr 2024 |
| GRBench (Graph-CoT) | 2024, UIUC et al. (ACL 2024 Findings †) | 1,740 questions over 10 domain graphs (academic, e-commerce, literature, healthcare, legal) †; the Graph-CoT agent iterates reason → **call graph functions** (retrieve node, node feature, neighbour check, node degree) → execute † | Not re-verified | [paper](https://arxiv.org/abs/2404.07103), [repo](https://github.com/PeterGriffinJin/Graph-CoT) † | Apr 2024 |
| CRAG (KG API part) | 2024, Meta | Mock KG search APIs alongside web search (see (c)) | see (c) | [paper](https://arxiv.org/abs/2406.04744) | Jun 2024 |

### Inferences

Which benchmarks test **structured/graph memory accessed through tools** (ranked by closeness to Orion):
1. **CodexGraph** (code graph DB + agent-written graph queries; NAACL 2025) — same architecture as Orion, but evaluated on coding tasks (CrossCodeEval, SWE-bench, EvoCodeBench), not memory or security ([paper](https://arxiv.org/abs/2408.03910)). Fit: A (strongest architectural analog; a related-work anchor).
2. **LocAgent / Loc-Bench, RepoGraph, LARGER (LocBench 560 issues, MuLocBench 1,100)** — agents traverse a repository graph through tools for localization ([LocAgent](https://arxiv.org/abs/2503.09089), [RepoGraph](https://arxiv.org/abs/2410.14684), [LARGER](https://arxiv.org/pdf/2605.16352)). Fit: D-ish for "does a code graph help an agent find the right function", since Orion's eval already uses a fix-site location convention; covered in more depth by the code-benchmark researcher.
3. **GRBench / Graph-CoT** — agent calls graph functions over a pre-built graph. Fit: A (non-code graphs; Orion could re-host one GRBench graph in Neo4j to sanity-check its tool loop).
4. **CypherBench / Text2Cypher / Mind the Query** — execution-grounded Cypher against Neo4j (see (e)). Fit: D for the Cypher-writing component only.
5. **CRAG** — KG APIs + hallucination-penalising truthfulness score. Fit: A.
6. **STaRK** — semi-structured (text + relations) retrieval, the nearest analog to Orion's CPG + code-chunk text. Fit: A.
7. **Zep / Mem0g** — graph memories on conversational benchmarks. Fit: A.

| Benchmark | Fit | Why | Contamination |
|---|---|---|---|
| MS GraphRAG eval / BenchmarkQED | A | Win-rate LLM judging does not fit Orion's labelled-ground-truth scoring; but BenchmarkQED's **local↔global query classes** and "compare vs vector RAG across context-window sizes" are reusable; the indexing-cost point applies to Joern build time | Pre-cutoff; AutoQ regenerable |
| LightRAG / HippoRAG / HippoRAG 2 | A | Cost framing (10–30× cheaper than iterative retrieval) is the efficiency claim Orion wants; graphs are LLM-built from text | Pre-cutoff |
| GraphRAG-Bench (Xiao) | N–A | College-level domain QA | Jun 2025, pre-cutoff |
| GraphRAG-Bench (Xiang) | A (strong) | Its core finding (graphs often lose to vanilla RAG; they help on certain task types) predicts Orion should **stratify results by vuln class**: multi-hop dataflow (where the CPG should help) vs single-location classes (secrets, headers, deps) where it may not | Pre-cutoff (v3 Feb 2026) |
| WebQSP / CWQ / MetaQA | N | Freebase/WikiMovies KGQA, old and memorised | Very old |
| STaRK | A | Hybrid text+structure retrieval | Pre-cutoff |
| GRBench | A | Tool-based graph traversal | Pre-cutoff |

Key mismatch to state plainly: every GraphRAG benchmark found builds its graph from prose with an LLM, so "graph quality" there means extraction quality; Orion's graph is produced by static analysis (Joern) and is exact but incomplete (known gaps such as missing `CONTAINS_CALL` edges inside arrow functions, per [CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md)). No GraphRAG benchmark measures a program-analysis graph.

### Gaps
- Per-method numbers for both GraphRAG-Bench papers, LightRAG's UltraDomain win rates and Microsoft's original win-rate figures were not retrieved.
- GRBench size (1,740 questions / 10 graphs) and Graph-CoT function names were recalled, not re-verified.
- The BenchmarkQED repo URL is recalled (not on the fetched page).
- No 2026 GraphRAG benchmark beyond Xiang v3 was found in this session's searches.

---

## (e) Text-to-Cypher benchmarks (relevant because Orion's agents and Orion-LM write Cypher)

### Takeaway
There are now five-plus public text-to-Cypher resources: Neo4j's Text2Cypher (2024, 44,387 pairs), IBM's Mind the Query (EMNLP 2025 Industry, 27,529 pairs over 11 executable graphs), CypherBench (ACL 2025, 11 Wikidata-derived property graphs, 7.8M entities, 10K+ questions, execution accuracy), the older Chinese SpCQL (CIKM 2022), and 2026 work (CypherSmith training data at ACL 2026; PIPE-Cypher, which generates benchmarks from a *live* property graph). All of them test *schema generalisation over many generic graphs*; none uses a code property graph, and none tests Orion's specific constraints (fixed Joern CPG vocabulary, read-only, scan_id-anchored patterns). They are usable as a **direct component test** of Cypher competence (Claude agents, and especially Orion-LM), but the more valuable move is to reuse PIPE-Cypher / Mind-the-Query's generation pipelines on Orion's own CPG schema to build a CPG-specific, post-cutoff text-to-Cypher set.

### Cited Findings

| Benchmark | Year / maintainer | What, size, format, scoring | Published results | Links | Released |
|---|---|---|---|---|---|
| Neo4j Text2Cypher (2024) | 2024, Neo4j | Combined/cleaned public NL→Cypher sets: 44,387 instances (39,554 train / 4,833 test); Google-BLEU and exact-match; on Hugging Face | Closed models (GPT-4o, Gemini 1.5 Pro) best; all fine-tuned models improved BLEU and EM (no hyperparameter tuning); caveat: train and test share one distribution | [paper](https://arxiv.org/abs/2412.10064), [benchmark blog](https://neo4j.com/blog/developer/benchmarking-neo4j-text2cypher-dataset/), [HF](https://huggingface.co/datasets/neo4j/text2cypher-2024v1) † | Dec 2024 |
| Mind the Query | 2025, IBM Research (EMNLP 2025 Industry) | 27,529 NL questions + Cypher over 11 graph datasets, each shipped with its graph DB for execution; schema/runtime/value checks + manual review; complexity tiers; released data + synthesis pipeline | Reports recurring hallucination patterns in LLM Cypher (results not fetched) | [IBM page](https://researcher.ibm.com/publications/mind-the-query-a-benchmark-dataset-towards-text2cypher-task), [ACL PDF](https://aclanthology.org/2025.emnlp-industry.133) | 2025 |
| CypherBench | 2024, Megagon Labs / Politecnico di Torino et al. (ACL 2025, pp. 8934–8958) | 11 large multi-domain property graphs (7.8M entities) converted from Wikidata, Neo4j-compatible; 10,000+ questions over 12 graph-pattern types (global, multi-hop, temporal, aggregation); execution accuracy; argues RDF schemas are too big for LLMs, property-graph views fix that; paper CC BY 4.0 | Accuracy falls as pattern complexity rises (secondary summary); best-model number not on abstract page | [paper](https://arxiv.org/abs/2412.18702), [ACL](https://aclanthology.org/2025.acl-long.438) | Dec 2024 |
| Multi-Agent GraphRAG (uses CypherBench) | 2025 | Text-to-Cypher multi-agent pipeline on labelled property graphs; 5 CypherBench graphs × 150 sampled QA; Gemini 2.5 Pro, GPT-4o, GigaChat 2 MAX, Qwen3 Coder | numbers not retrieved | [paper](https://arxiv.org/abs/2511.08274) | Nov 2025 |
| SpCQL | 2022 (CIKM 2022 †) | Chinese NL→Cypher semantic parsing dataset (~10,000 pairs †) over a Neo4j KG † | Not re-verified | [ACM DL](https://dl.acm.org/doi/10.1145/3511808.3557703) † | 2022 |
| CypherSmith | 2026 (ACL 2026 main) | Instruction-tuning set >12× larger than prior public Text-to-Cypher data, synthetic via open LLMs + likelihood filtering | Reports SOTA fine-tuned performance (snippet only) | [ACL 2026 PDF](https://aclanthology.org/2026.acl-long.1601.pdf) | 2026 |
| PIPE-Cypher | 2026 preprint | Local pipeline that turns a **live property graph** (+ optional seed queries) into balanced NL→Cypher benchmarks | Zero-shot transfer weak; schema-specific few-shot banks help; a leaderboard lists mean accuracy 26.9 on its 296-example held-out set (secondary) | [listing](https://papers.cool/arxiv/2606.08481) (identity of this arXiv ID with PIPE-Cypher not confirmed) | 2026 |

### Inferences

| Benchmark | Fit | Why | Contamination |
|---|---|---|---|
| Neo4j Text2Cypher 2024 | D (component) | Quick, cheap check that Orion-LM's Cypher writing has not regressed on general Cypher; but it mixes many schemas, and Orion-LM is trained on one fixed CPG schema, so a low score is expected and not very informative | Public on HF since Dec 2024: likely in frontier and Qwen training data |
| Mind the Query | D (component) + A | Executable graphs → execution accuracy; its released **synthesis pipeline** can be pointed at Orion's CPG schema | 2025, pre-cutoff |
| CypherBench | D (component) | Execution-accuracy scoring and "pattern-type" breakdown (multi-hop, aggregation) map to Orion's query mix; the read-only nature fits Orion's guard | Dec 2024, pre-cutoff |
| SpCQL | N | Chinese, small, old | Old |
| CypherSmith | A | A training-data recipe for Orion-LM's Cypher skill (note: Orion's plan says AI-written text2cypher rows were dropped and Love Kush writes templates, per [CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md), so this is only a reference point) | 2026 |
| PIPE-Cypher | A (strong) | Generate a **fresh, post-cutoff CPG text-to-Cypher benchmark** from Orion's own Neo4j graphs (NodeGoat/PyGoat scans) — contamination-free by construction | 2026 |

Orion-specific adapter issues (inference): public Cypher benchmarks assume one graph per database; Orion's MCP guard refuses any pattern not anchored to `scan_id` and any procedure outside a read-only allowlist ([CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md)), so running them through Orion's real tool path needs either a scan_id-stamping loader or scoring the model's Cypher on a bare Neo4j outside the guard (which then no longer tests the guard).

### Gaps
- CypherBench's best-model execution accuracy was not found on the abstract page (a figure around 61.58% for Claude 3.5 Sonnet is recalled but unverified — do not quote).
- Whether Neo4j released a "text2cypher-2025v1" dataset was not verified.
- PIPE-Cypher's arXiv ID and full results were not confirmed; Mind the Query's model results were not fetched.
- No text-to-Cypher benchmark over a code property graph (Joern or otherwise) was found.

---

## (f) Memory / long-context benchmarks built on source code or software repositories

### Takeaway
Yes, several exist and they are the nearest "memory" benchmarks Orion could actually run: RepoQA (needle-function search, 500 tasks, 50 repos, ~16K tokens), LongCodeBench (32K→1M-token ladder from real GitHub issues), LoCoBench (Salesforce, 10K–1M tokens, 8,000 scenarios incl. a **security analysis** category) and its multi-turn LoCoBench-Agent, Long Code Arena (JetBrains, 6 project-level tasks incl. bug localization), LongCodeU (ACL 2025; drops sharply past 32K), plus code sub-tasks inside general suites (LongBench LCC/RepoBench-P, LongBench v2 code-repo, InfiniteBench Code.Debug/Run). None is a vulnerability benchmark with source→sink ground truth, and none exposes a code graph through tools except the *methods* CodexGraph/LocAgent/RepoGraph/LARGER.

### Cited Findings

| Benchmark | Year / maintainer | What, size, lengths, scoring | Published results | Links | Released |
|---|---|---|---|---|---|
| RepoQA | 2024 (ICML 2024 workshop) | "Searching Needle Function": given a NL description, retrieve the matching function from ~16K tokens of repo code; 500 tasks, 50 repos, 5 languages (Python, C++, Java, TypeScript, Rust); 10 needles per repo at depths 10%–100%; pass if output is most similar function with BLEU > 0.8 | 26 models (arXiv version); small gap between best open and proprietary models | [HF paper page](https://huggingface.co/papers/2406.06025), [workshop](https://mlanthology.org/icmlw/2024/liu2024icmlw-repoqa) | Jun 2024 |
| LongCodeBench | 2025, Panasonic AI Research / Sapienza / Stanford et al. | Code comprehension (LongCodeQA) and repair (LongSWE-Bench) from real GitHub issues; context ladder 32K, 64K, 128K, 256K, 512K, 1M; 1,043 instances from 108 repos (a third party says 600 — conflict) | Long context is a weakness for all models: Claude 3.5 Sonnet 29% → 3%, Qwen2.5 70.2% → 40% as context grows | [paper](https://arxiv.org/abs/2505.07897) | 12 May 2025 (v3 22 Oct 2025) |
| LoCoBench | 2025, Salesforce AI Research | 1,000 synthetic project specs, 10 languages, 36 domains → >50K files, 15M LOC → 8,000 scenarios in 8 categories (architectural understanding, cross-file refactoring, bug investigation, feature implementation, **security analysis**, …); 10K–1M tokens; 17 metrics in 4 dimensions → LoCoBench Score (LCBS) | "Substantial performance gaps"; long-context SE understanding unsolved | [paper](https://arxiv.org/abs/2509.09614), [repo](https://github.com/SalesforceAIResearch/LoCoBench) | Sep 2025 |
| LoCoBench-Agent | 2025, Salesforce | Multi-turn agent version of LoCoBench scenarios, 9 comprehension/efficiency metrics | not stated | [listing](https://www.catalyzex.com/paper/locobench-a-benchmark-for-long-context-large) | 2025 |
| Long Code Arena | 2024, JetBrains Research / TU Delft | 6 project-level tasks: library-based generation, CI build repair, project-level completion, commit messages, **bug localization**, module summarization; HF leaderboard | All models struggle to use long-context information for complex SE tasks (secondary) | [paper](https://huggingface.co/papers/2406.11612), [secondary](https://www.alphaxiv.org/benchmarks/delft-university-of-technology/long-code-arena) | Jun 2024 |
| LongCodeU | 2025, Beihang et al. (ACL 2025) | 4 aspects / 8 tasks of long-code understanding on real code with context dependencies (not concatenated snippets) | Performance "drops dramatically" beyond 32K, far short of claimed 128K–1M | [paper](https://arxiv.org/abs/2503.04359) | Mar 2025 |
| RepoBench | 2023 (ICLR 2024 †) | Repository-level code auto-completion: retrieval (R), completion (C), pipeline (P) † | Not re-verified | [paper](https://arxiv.org/abs/2306.03091) | Jun 2023 |
| CodeRAG-Bench | 2024 † | Retrieval-augmented code generation with docs/repos as retrieval sources † | Not re-verified | [paper](https://arxiv.org/abs/2406.14497) † | Jun 2024 |
| Code sub-tasks in general suites | — | LongBench LCC + RepoBench-P; LongBench v2 "code repository understanding"; InfiniteBench Code.Debug/Code.Run † | see (a) | [LongBench](https://arxiv.org/abs/2308.14508), [LongBench v2](https://arxiv.org/abs/2412.15204), [InfiniteBench](https://arxiv.org/abs/2402.13718) | 2023–2024 |
| CodexGraph (method) | 2024, NAACL 2025 | LLM agent + graph DB interface extracted from a repo; agent writes graph queries (Cypher-style) for structure-aware retrieval; argues similarity retrieval has low recall on complex tasks | Evaluated on CrossCodeEval, SWE-bench, EvoCodeBench (numbers not fetched) | [paper](https://arxiv.org/abs/2408.03910), [ACL](https://preview.aclanthology.org/landing_page/2025.naacl-long.7.pdf) | Aug 2024 |
| LocAgent + Loc-Bench (method + benchmark) | 2025 | Repo parsed into a directed heterogeneous graph, agent traverses it for code localization; Loc-Bench released; accuracy declines below file level (later study) | not fetched | [paper](https://arxiv.org/abs/2503.09089) | Mar 2025 |
| RepoGraph (method) | 2024 (ICLR 2025 †) | Line-level definition/reference graph usable as an extra agent action | not fetched | [paper](https://arxiv.org/abs/2410.14684) | Oct 2024 |
| LARGER (method) | 2026 | Repository-graph index for agent search; LocBench (560 issues, 5 repos) and MuLocBench (1,100 multi-file issues, 46 repos); baselines BM25, Agentless, SWE-agent, OpenHands, Claude Code, CoSIL, LocAgent | not fetched | [paper](https://arxiv.org/pdf/2605.16352) | May 2026 |

### Inferences

| Benchmark | Fit | Why | Contamination |
|---|---|---|---|
| RepoQA | **D** | Orion can build the CPG + chunk index for each RepoQA repo and answer "which function matches this description" via `semantic_search`/Cypher, versus the 16K-token in-context baseline; tests retrieval, not security | Jun 2024 public repos: memorised code is likely |
| LongCodeBench | **D (QA part)** | Its 32K→1M ladder from the same issues is exactly the controlled sweep needed: plain agent vs Orion-tooled agent at each rung, tokens per correct answer | 2025 GitHub issues, pre-cutoff |
| LoCoBench (+Agent) | **D (security-analysis category)** | Only long-context code benchmark found with a security category; synthetic projects (lower memorisation); 1M-token rungs; but synthetic code is not a CVE, and scoring is LLM/metric-based | Sep 2025, pre-cutoff, synthetic |
| Long Code Arena (bug localization) / Loc-Bench / LocBench / MuLocBench | D (localization) | Fix-site localization is Orion's lead convention; a graph-tool agent can be scored on whether it names the right file/function | Pre-cutoff |
| LongCodeU | D–A | Real-code long understanding; can be answered with graph tools instead of the full text | Pre-cutoff |
| RepoBench / CodeRAG-Bench / CoIR | D (component) | Score the retrieval layer (chunking, embedder, hybrid RRF) | Pre-cutoff |
| CodexGraph / RepoGraph / LARGER | A (related work) | Same "code graph behind tools" thesis on non-security tasks; Orion's differentiator is the CPG dataflow (REACHING_DEF/FLOWS_TO) and the verifier | — |

### Gaps
- No long-context or memory benchmark was found that is specifically about vulnerabilities with source→sink ground truth (security-specific benchmarks are presumably covered by the code-benchmark researcher).
- LoCoBench's security-analysis category size, scoring details and published per-model numbers were not retrieved.
- RepoQA's leaderboard numbers and license were not retrieved; LongCodeBench's instance count conflicts (1,043 vs 600).
- No benchmark was found that pairs a code graph DB with text-to-Cypher questions (a "code Text2Cypher").

---

## (g) Methodology to borrow so Orion's benefit can be shown without a code-specific memory benchmark

### Takeaway
The literature converges on five reusable designs: (1) **controlled context-length sweeps** with the task held fixed (NIAH, RULER, LongCodeBench, LOFT, Context Rot); (2) **lexical-overlap and distractor controls** (NoLiMa, Context Rot, MRCR); (3) **memory/retrieval ablations against a full-context baseline and a strong tool baseline** (Mem0's full-context arm, Letta's filesystem agent, LOFT's LCLM-vs-retrieval-vs-SQL, MemoryAgentBench's agent families); (4) **oracle-evidence and abstention settings** to separate retrieval from reasoning and to score silence (LongMemEval); (5) **cost reporting next to accuracy** — tokens, p95 latency, and one-off indexing cost (Mem0, Zep, HippoRAG, LazyGraphRAG). Orion already has the scoring path (`eval/`) and plain arms; these designs slot in as extra arms/strata rather than new benchmarks.

### Cited Findings
- Length × depth sweeps: NIAH varies needle depth and context length ([repo](https://github.com/gkamradt/LLMTest_NeedleInAHaystack)); RepoQA places 10 needles per repo at depths 10%–100% ([HF](https://huggingface.co/papers/2406.06025)); LongCodeBench evaluates the same task family at 32K/64K/128K/256K/512K/1M ([paper](https://arxiv.org/abs/2505.07897)); LOFT auto-generates increasing context lengths ([paper](https://arxiv.org/abs/2406.13121)).
- "Effective vs claimed" context: RULER finds only half of models satisfactory at 32K despite ≥32K claims † ([paper](https://arxiv.org/abs/2404.06654)); LongBench Pro finds effective < claimed across 46 models ([paper](https://arxiv.org/abs/2601.02872)); BABILong finds 10–20% of context effectively used † ([paper](https://arxiv.org/abs/2406.10149)).
- Lexical-overlap control: NoLiMa removes literal matches between question and needle; GPT-4o 99.3% → 69.7% at 32K ([paper](https://arxiv.org/abs/2502.05167)).
- Distractors: Context Rot separates topically related distractors from irrelevant filler across 18 LLMs ([report](https://research.trychroma.com/context-rot)); MRCR asks models to distinguish near-identical needles ([HF](https://huggingface.co/datasets/openai/mrcr)).
- Same graph, two access paths: Graphwalks puts a graph's edge list in the prompt and scores BFS/parent node-set F1, split at 128K ([Epoch review](https://epoch.ai/benchmarks/graphwalks/review)).
- Long-context vs retrieval vs SQL on the same corpus: LOFT ([paper](https://arxiv.org/abs/2406.13121)).
- Full-context baseline with cost: Mem0 reports accuracy plus 91% lower p95 latency and >90% token savings vs full context ([paper](https://arxiv.org/abs/2504.19413)); Zep reports −90% latency ([paper](https://arxiv.org/abs/2501.13956)); HippoRAG reports 10–30× cheaper, 6–13× faster than iterative retrieval † ([paper](https://arxiv.org/abs/2405.14831)).
- Strong simple baseline: Letta's files + grep agent scored 74.0% on LoCoMo vs a reported 68.5% for Mem0's graph variant ([secondary](https://www.agenticwire.news/article/mem0-zep-letta-agent-memory)).
- Indexing cost matters: LazyGraphRAG is motivated by avoiding "prohibitive up-front indexing costs" ([MSR](https://www.microsoft.com/en-us/research/?p=1140429)).
- Graphs do not always help: GraphRAG "frequently underperforms vanilla RAG on many real-world tasks" ([Xiang et al.](https://arxiv.org/abs/2506.05690)).
- Competency breakdown and incremental feeds: MemoryAgentBench ([paper](https://arxiv.org/abs/2507.05257)); oracle + abstention: LongMemEval ([paper](https://arxiv.org/abs/2410.10813); oracle setting †).
- Benchmark generation from a live graph to avoid contamination: PIPE-Cypher ([listing](https://papers.cool/arxiv/2606.08481)); Mind the Query releases its synthesis pipeline ([IBM](https://researcher.ibm.com/publications/mind-the-query-a-benchmark-dataset-towards-text2cypher-task)); BEAM releases a conversation generator ([paper](https://arxiv.org/abs/2510.27246)).
- Orion's existing hooks: plain frontier arms, efficiency headline (vulns per 1M tokens) + false-alarm rate, per-message token capture, patched-commit negatives, opt-in feature flags (`--shortlists`, `--semantic-mode`, `--runtime`, `--verify-context`) with byte-identical defaults ([CLAUDE.md](C:/Users/h/Downloads/orion/CLAUDE.md)).

### Inferences
Concrete Orion experiments built from the above (none run; all assessment):
1. **Repo-size sweep (RULER/LongCodeBench style).** Fix a set of known vulns (NodeGoat, PyGoat, post-cutoff CVEs), then pad each repo with realistic but irrelevant first-party-looking code to 32K / 128K / 512K / 1M / 5M tokens. Arms: plain agent with file tools, plain agent with whole repo in context (where it fits), Orion. Plot recall, false alarms and tokens per true finding vs size; claim = Orion's curve stays flat while plain arms decay ("effective repo size").
2. **No-lexical-cue test (NoLiMa style).** Rename identifiers and route sources through wrappers so `req.body`, `exec`, `query` no longer appear near each other; compare grep-based plain agents, `semantic_search` vector/keyword/hybrid, and Cypher over FLOWS_TO. Expect keyword/grep to drop and graph dataflow to hold if Orion's thesis is right.
3. **Distractor test (Context Rot / MRCR style).** Insert patched twins and sanitized near-miss flows next to the real flow; score false alarms. Orion already trains/evaluates with patched commits; this makes it a controlled stratum.
4. **Access-path ablation (LOFT / Graphwalks / Mem0 style).** Same model, same repo: (a) full repo in context, (b) file tools only (Letta-style strong baseline = Orion's plain arm), (c) `semantic_search` only, (d) Cypher only, (e) full Orion. Report accuracy, tokens, p50/p95 latency, and **graph build + embedding cost amortised per scan** (do not hide Joern time).
5. **Oracle-evidence split (LongMemEval style).** Give the verifier only the enclosing function + 2 callers/callees (the existing `--verify-context` cut) vs Orion's tool path vs whole repo; the gap between oracle and Orion = retrieval loss, the gap between oracle and 100% = reasoning loss.
6. **Stratify by "needs a graph" (GraphRAG-Bench / BenchmarkQED style).** Report separately for multi-hop cross-file dataflow vulns vs single-location classes (secrets, headers, ReDoS, deps), mirroring `verify.route()`'s light/full split; Xiang et al. predict graph gains concentrate in the former.
7. **Cypher competence (component).** Score Orion-LM and the Claude agents on a CypherBench / Mind-the-Query subset for general competence, and on a PIPE-Cypher-style set generated from Orion's own CPG schema (post-cutoff, contamination-free) for the competence that matters.
8. **Graphwalks re-host (quick, cheap demo).** Load Graphwalks graphs into Neo4j and let an agent answer BFS/parents via Cypher vs in-context: a clean, non-code demonstration of "external graph memory beats reading the graph", with tokens counted. Caveat: proves the access-path claim, not the security claim.
9. **Reporting rules borrowed:** effective-length curves; tokens and latency next to recall; abstention/false-alarm rate as a first-class metric; N≥3 runs (Orion's protocol) since the vendor memory disputes show single-run, single-harness numbers do not survive scrutiny.

### Gaps
- No published study was found that measures a static-analysis code graph as agent memory with any of these designs; the Orion experiments above are proposals, with no numbers.
- Whether padding repos with synthetic code creates detectable artefacts (models spotting the padding) is untested; LoCoBench's synthetic-project approach may inform it but was not read in detail.
- Licenses for reusing most datasets (RepoQA, LongCodeBench, LoCoBench, Mind the Query, CypherBench data) were not verified.
