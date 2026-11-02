# Evaluation harnesses, benchmark infrastructure and methodology for "model + Orion vs the same model alone"

Scope note for the report writer: these notes cover (a) harnesses that can run agents and benchmarks, (b) SAST/SARIF scoring tools, (c) statistical and reporting method, and (d) how recent papers structure "tool-augmented vs base model" comparisons. Facts marked "(prior knowledge, not re-fetched 2026-10-31)" come from the cited primary source as I know it but were not re-opened in this session. Treat them as lower confidence than the rest. Orion-internal facts cite files in the local checkout (C:/Users/h/Downloads/orion).

## Q1. Which harnesses can wrap an external CLI agent (like `claude -p` with MCP servers) or a custom in-process model, and run it in a sandboxed Docker container per task?

### Takeaway
Two harnesses do this out of the box in late 2026: **UK AISI Inspect** (its `sandbox_agent_bridge()` plus the `inspect_swe` package's `claude_code()` agent) and **Harbor** (the official Terminal-Bench 2.0 harness, `--agent claude-code`). Both run one Docker container per sample and record tokens and transcripts. SWE-bench's harness and OpenHands' benchmarks repo are tied to their own task family and agent. METR's Vivaria was archived on 2026-10-07 (METR moved to Inspect), and OpenAI's simple-evals stopped updating in July 2025. Neither should be a new dependency.

### Cited Findings

**Inspect (UK AISI) and inspect_swe**
- `sandbox_agent_bridge()` lets an agent running *inside* a sandbox make its model calls through Inspect's model provider. The docs name Claude Code, Codex CLI and Gemini CLI as examples. You put the agent binary in the sandbox Dockerfile, point it at a localhost port (13131 in the docs) via a base-URL env var, and launch it with `sandbox().exec()`. The agent can be written in any language. — [Inspect: Agent Bridge](https://inspect.aisi.org.uk/agent-bridge.html)
- The bridge runs a proxy inside the container that accepts OpenAI, Anthropic and Google API requests and relays them to the current Inspect model. It listens only on the sandbox's loopback interface. — [Inspect: Agent Bridge](https://inspect.aisi.org.uk/agent-bridge.html)
- MCP: `bridged_tools` / `BridgedToolsSpec` expose host-side Inspect tools to sandboxed agents as MCP tools. The bridge writes an MCP server script into the sandbox and returns `MCPServerConfigStdio` configs in `bridge.mcp_server_configs` for CLI agents. — [Inspect: Agent Bridge](https://inspect.aisi.org.uk/agent-bridge.html)
- In-process Python agents use `agent_bridge(state)` with model name `"inspect"`. The docs cite the OpenAI Agents SDK, Pydantic AI and LangChain. `model_aliases` / `model_resolver` map agent-side model names to Inspect models. — [Inspect: Agent Bridge](https://inspect.aisi.org.uk/agent-bridge.html)
- Bridge limitations:
  - Only the OpenAI Completions, OpenAI Responses, Anthropic and Google APIs are bridged.
  - Client generation parameters (`max_tokens`, `temperature`, reasoning settings) are DROPPED by default unless `forward_generation_config=True`.
  - Provider-executed tools (`web_search`, `web_fetch`, `code_execution`, remote MCP) are withheld unless granted.
  - Request headers are dropped except `Accept-Encoding` and any listed in `forward_client_headers`.
  - `n=1` is recommended.

  — [Inspect: Agent Bridge](https://inspect.aisi.org.uk/agent-bridge.html)
- `inspect_swe` (`pip install inspect-swe`, by Meridian Labs) wraps six CLI agents as standard Inspect agents:
  - `claude_code()`, `codex_cli()`, `gemini_cli()`, `kimi_code()`, `opencode()`, `mini_swe_agent()`;
  - each runs inside the sample's sandbox, with model calls proxied back through the bridge, so any model can drive it and token/time limits and transcripts work as usual;
  - the docs' example task uses `solver=claude_code()`, `scorer=model_graded_qa()` and `sandbox="docker"`.

  — [Inspect SWE docs](https://meridianlabs-ai.github.io/inspect_swe/)
- Terminal-Bench 2.0 and SWE-Bench Pro run in Inspect through a separate **Inspect Harbor** package, not inspect_evals. Some Docker-based inspect_evals tasks may need up to 32 GB of RAM. — [inspect_evals README](https://github.com/UKGovernmentBEIS/inspect_evals)
- Run commands: `uv run inspect eval inspect_evals/<task> --model <provider/model>`, `inspect eval-set ...` for several tasks, `inspect view` for the log viewer. — [inspect_evals README](https://github.com/UKGovernmentBEIS/inspect_evals)
- Inspect supports repeated runs per sample ("epochs") with reducers such as mean, max, `pass_at_k` and `at_least_k`. — [Inspect: Scorers / reducing epochs](https://inspect.aisi.org.uk/scorers.html) (prior knowledge, not re-fetched 2026-10-31)

**Harbor (Terminal-Bench team)**
- Harbor is "the official harness for Terminal-Bench-2.0". It evaluates arbitrary agents, including Claude Code, OpenHands and Codex CLI. — [harbor-framework/harbor](https://github.com/harbor-framework/harbor)
- Example: `harbor run --dataset terminal-bench@2.0 --agent claude-code` plus a model flag and a concurrency setting. It runs locally on Docker, or on cloud sandboxes via `--env`; Daytona is named, and mirrors also list Modal, LangSmith, Blaxel and Novita. Installs with `uv tool install harbor` / `pip install harbor`. Mirrors' citation metadata says v0.16.1. Harbor also supports custom benchmarks/environments and RL rollouts. — [harbor-framework/harbor](https://github.com/harbor-framework/harbor) (via search-result summaries of the README and its mirrors)

**SWE-bench harness and sb-cli**
- `swebench.harness.run_evaluation` runs locally on Docker by default. It pulls prebuilt Linux images from DockerHub, and `--namespace ''` builds them locally. `--modal true` runs the evaluation on Modal. — [SWE-bench harness reference](https://swebench.com/SWE-bench/reference/harness); [SWE-bench repo](https://github.com/SWE-bench/SWE-bench)
- Recommended local resources: x86_64, at least 120 GB free storage, 16 GB RAM, 8 CPU cores. — [SWE-bench repo](https://github.com/SWE-bench/SWE-bench)
- `sb-cli` is the cloud-evaluation / leaderboard-submission tool (described as running on AWS). I found no commands or auth details. — [swebench on PyPI](https://pypi.org/project/swebench)

**OpenHands evaluation**
- The `OpenHands/benchmarks` repo holds evaluation infrastructure for OpenHands agents (SWE-Bench, SWE-Bench Pro) and is migrating from V0 to the V1 Agent SDK. Not every benchmarks version works with every SDK version. — [OpenHands/benchmarks](https://github.com/OpenHands/benchmarks)
- Scale-out uses a remote runtime API that provisions cloud containers. Default endpoint `https://runtime.eval.all-hands.dev`; needs `RUNTIME_API_KEY`. The SWE-bench flow builds images from the SDK commit, then runs `uv run swebench-infer` with a config in `.llm_config/`. — [OpenHands/benchmarks README](https://raw.githubusercontent.com/OpenHands/benchmarks/main/README.md)

**OpenReward / EnvCommons (General Reasoning)**
- The Open Reward Standard (ORS, March 2026) is an HTTP protocol. Environments are servers that expose tasks, tools and rewards, and actions are tool calls. — [The Open Reward Standard](https://www.gr.inc/releases/the-open-reward-standard)
- OpenReward is a platform for serving RL environments, launched March 2026. EnvCommons is a GitHub org hosting "200+" prebuilt ORS environments (vendor-reported). — [Introducing OpenReward](https://www.gr.inc/releases/introducing-openreward)
- The `openreward` Python SDK (0.1.125, May 2026) builds environments and trains agents against them. Hugging Face TRL integrates it via `trl[openreward]`. — [TRL OpenReward docs](https://huggingface.co/docs/trl/en/openenv)

**METR Vivaria / Task Standard: ARCHIVED**
- The Vivaria repo "was archived by the owner on Oct 7, 2026. It is now read-only." The README says METR "is transitioning its internal tooling from Vivaria to Inspect" and recommends Inspect for new projects. The Task Standard follows semver, but its current maintenance status is not stated. — [METR/vivaria](https://github.com/METR/vivaria)

**OpenAI simple-evals: DEPRECATED; openai/evals**
- simple-evals' deprecation notice: "July 2025: simple-evals will no longer be updated for new models or benchmark results." It keeps reference implementations for HealthBench, BrowseComp and SimpleQA. — [openai/simple-evals](https://github.com/openai/simple-evals)
- openai/evals is a registry of YAML/JSONL evals. Custom systems plug in through a "completion function" protocol. — [openai/evals](https://github.com/openai/evals) (prior knowledge, not re-fetched; its late-2026 maintenance status is not verified)

**Text-only / single-turn harnesses (poor fit for an agentic CLI)** (all prior knowledge, not re-fetched)
- EleutherAI lm-evaluation-harness: few-shot and log-likelihood benchmarks over HF / vLLM / API models. It has no Docker-per-task agent sandbox. — [EleutherAI/lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness)
- Stanford HELM: holistic multi-metric evaluation of models across scenarios, with a model-centric design. — [stanford-crfm/helm](https://github.com/stanford-crfm/helm)
- BigCode evaluation harness: code-generation benchmarks scored by pass@k with code execution, optionally in Docker. It is single-function generation, not repo agents. Its current maintenance status is not verified. — [bigcode-evaluation-harness](https://github.com/bigcode-project/bigcode-evaluation-harness)

**Red-team / LLM-app eval tools (relevant to Orion's prompt-injection surface, not to vuln-finding recall)** (all prior knowledge, not re-fetched)
- NVIDIA garak: an LLM vulnerability scanner built from probes, detectors and generators, including a REST generator for custom endpoints. — [NVIDIA/garak](https://github.com/NVIDIA/garak)
- Microsoft PyRIT: Python Risk Identification Toolkit for red-teaming generative-AI systems. — [Azure/PyRIT](https://github.com/Azure/PyRIT)
- Promptfoo: a CLI for prompt/model evals and red-teaming. It supports custom providers, including script/exec-style ones. — [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo)
- Meta CyberSecEval runner (PurpleLlama `CybersecurityBenchmarks`): insecure-code, MITRE/ATT&CK, prompt-injection, interpreter-abuse and related benchmarks. It is also packaged in inspect_evals as CyberSecEval_2/3/4. — [PurpleLlama CybersecurityBenchmarks](https://github.com/meta-llama/PurpleLlama/tree/main/CybersecurityBenchmarks); [inspect_evals catalog](https://ukgovernmentbeis.github.io/inspect_evals/)
- DeepEval: pytest-style LLM test cases and metrics, including G-Eval. RAGAS: RAG metrics (context precision/recall, faithfulness). LangSmith and Braintrust: hosted experiment tracking, datasets and judges. — [confident-ai/deepeval](https://github.com/confident-ai/deepeval); [explodinggradients/ragas](https://github.com/explodinggradients/ragas); [LangSmith docs](https://docs.smith.langchain.com/); [Braintrust docs](https://www.braintrust.dev/docs)

### Inferences
- **Plain-agent arms (`plain-sonnet5` / `plain-opus5`):** Inspect + `inspect_swe.claude_code()` is the closest off-the-shelf match for "the same model without Orion".
  - It gives one Docker container per repo/sample, with transcripts and token accounting from the proxy rather than from a CLI shim. Orion's `eval/shim/claude` exists only because Orion's `--json` carries no usage (CLAUDE.md, "Open-weight study").
  - Withholding provider-side `web_search`/`web_fetch` by default helps with the known "frontier agents have web access" harness bug. Claude Code's client-side web tools would still need container network rules.
- **Orion arm:**
  - The Orion arm needs Neo4j + Joern and runs `claude -p` with Orion's MCP server. That fits Inspect's bridge pattern: the binary sits in the image, MCP is configured per sample, and model calls are proxied.
  - Running Neo4j inside every per-task sandbox is heavy on a 16 GB M4 (Joern + Neo4j + model cannot co-reside). Phased runs with an external Neo4j are more realistic than full per-task isolation.
- **Sampling freeze:** the bridge silently drops `temperature` unless `forward_generation_config=True`. Any Inspect wrapper must set it, or the frozen sampling will not reach the model.
- **Orion-LM:** the in-process MLX model cannot use the bridge, which only speaks OpenAI/Anthropic/Google APIs. It would need an OpenAI-compatible server (e.g. MLX-LM's server) or a custom Inspect model provider, and either could change behaviour versus the in-process arm. Keeping `eval/arms/lm.py` in-process and only borrowing the statistics is lower risk.
- **Status flags for the report:** Vivaria is archived and simple-evals is deprecated. lm-eval-harness, HELM and bigcode-evaluation-harness are the wrong shape for agentic repo scans. Garak, PyRIT and Promptfoo fit only a separate study of whether hostile repo text can steer Orion's agents.
- **Decision D5 still holds:** Krish's `eval/` is the one measurement path. Inspect or Harbor would wrap *execution* (sandboxing, transcripts). Scoring would still go through `eval/normalize` + `eval/match`.

### Gaps
- I did not read `inspect_swe`'s per-agent reference page, so its options are unverified: system prompt override, MCP server config, `--add-dir`-style folder restriction, version pinning.
- Harbor's task-folder format and whether its claude-code agent accepts custom MCP servers were not verified.
- sb-cli commands, auth and pricing were not found.
- I found no evidence that any harness ships a ready "SAST scanner vs answer key" scorer. All the ones checked score tests, flags or graded answers.
- Promptfoo ownership/status changes in 2026 are not verified, nor whether garak or PyRIT added agent-CLI targets.

## Q2. Which benchmarks from the security and code-retrieval families already have ready-made task definitions in these harnesses, and which SAST scoring tools exist?

### Takeaway
inspect_evals now ships a large cyber suite. Some of its tasks are close to Orion's detection-and-localization task: **CASTLE** (CWE detection in small C programs), **CyberGym** (real-world vulnerability analysis), **PatchEval** (CVE repair in Python/JS/Go), **CVEBench** (web CVE exploitation) and the **CyberSecEval** 2/3/4 suites. SWE-bench Verified is in inspect_evals and in OpenHands; Terminal-Bench 2.0 and SWE-Bench Pro come via Harbor / Inspect Harbor. None scores SARIF findings against a fix-site answer key. For that, the ready-made option is the **OWASP Benchmark scorecard** (BenchmarkUtils). For SARIF handling: **sarif-tools** (diff/summary/CI gating), the **Microsoft SARIF SDK Multitool** and GitHub code scanning (10 MB SARIF upload limit).

### Cited Findings

**Ready-made security tasks in inspect_evals** (category grouping inferred from descriptions on the catalog page)
- Vulnerability detection, repair and CVE tasks:
  - CASTLE (CWE automated security testing; LLMs detect vulnerabilities in small C programs)
  - CyberGym (real-world vulnerability analysis at large scale)
  - PatchEval (automated repair of real CVEs in Python, JavaScript and Go, verified with security tests)
  - CVEBench (agents exploit real web-app CVEs)
  - ExploitBench (V8 N-day exploits)
  - CyberSecEval_2, CYBERSECEVAL 3 and CyberSecEval 4 (CyberSecEval 4 covers ATT&CK compliance, insecure code, prompt injection, phishing)

  — [inspect_evals catalog](https://ukgovernmentbeis.github.io/inspect_evals/)
- CTF and knowledge tasks: Cybench, GDM in-house CTF, InterCode CTF, 3CB, TarantuBench, CTI-REALM, CyberMetric, SecQA, SEvenLLM, b3. Agent-security tasks: AgentDojo, AgentThreatBench, CodeIPI (prompt injection against coding agents), MCPTox, Hack-Verifiable Terminal Bench. — [inspect_evals catalog](https://ukgovernmentbeis.github.io/inspect_evals/)
- Coding tasks: SWE-bench Verified, SWE-Lancer, BigCodeBench, HumanEval, MBPP, APPS, DS-1000, ClassEval, SciCode, USACO, LiveCodeBench-Pro, KernelBench, ComputeEval, Frontier-CS, IFEvalCode. — [inspect_evals catalog](https://ukgovernmentbeis.github.io/inspect_evals/)
- Cybench and the GDM capability evals pull Docker images. — [inspect_evals README](https://github.com/UKGovernmentBEIS/inspect_evals)
- In Harbor: Terminal-Bench 2.0 (`terminal-bench@2.0`). In OpenHands/benchmarks: SWE-Bench and SWE-Bench Pro. — [harbor-framework/harbor](https://github.com/harbor-framework/harbor); [OpenHands/benchmarks](https://github.com/OpenHands/benchmarks)

**OWASP Benchmark scorecard**
- The Benchmark is a Java and Python test suite, and the current release is v1.2. Its stated intent is that every scored vulnerability is actually exploitable. The project page links Youden's J, ROC and precision/recall as the scoring basis. — [OWASP Benchmark project](https://owasp.org/www-project-benchmark/)
- The Benchmark score is Youden's J = TPR − FPR, reported per CWE category and overall. — [OWASP Benchmark project](https://owasp.org/www-project-benchmark/) (formula from prior knowledge; the fetched overview page links Youden's J but does not state the formula)
- Scorecards come from BenchmarkUtils:
  - `mvn org.owasp:benchmarkutils-maven-plugin:create-scorecard -DconfigFile=config/YOURCUSTOMconfig.yaml`;
  - suites usually ship `createScorecards.sh` and `createAnonScorecards.sh` (the latter anonymizes commercial tools);
  - the generator compares the tool results in `/results` with `expectedresults-##.csv` and writes to `/scorecard`;
  - the README does not list the parsed formats, nor whether SARIF is accepted.

  — [OWASP-Benchmark/BenchmarkUtils](https://github.com/OWASP-Benchmark/BenchmarkUtils)
- Orion's eval already pins BenchmarkJava `a0f07b8` and BenchmarkPython `f129148`. `--owasp eval/dataset/owasp_sample.json` adds 120 OWASP cases as the false-alarm set, and `strip_answers` removes the answer CSV, the per-case XML and the scorecards. — local: C:/Users/h/Downloads/orion/CLAUDE.md ("Harness finished (2026-10-22)")

**SARIF tooling**
- sarif-tools (Microsoft; Python CLI + library, v3.0.5):
  - `diff` compares two sets of SARIF files (new vs old findings, JSON output with `-o`);
  - `summary` and `diff` take `--check <level>` to exit non-zero when issues at a level increase versus a baseline;
  - `pip install sarif-tools`, or `python -m sarif`.

  — [sarif-tools on PyPI](https://pypi.org/project/sarif-tools); [docs index](https://docsearch.algolia.com/mcp/docs/repo/microsoft/sarif-tools)
- GitHub code scanning rejects SARIF files over 10 MB and syntactically invalid SARIF. Uploads also fail when Code Security is off or CodeQL default setup is on. — [GitHub: troubleshoot SARIF uploads](https://docs.github.com/en/enterprise-server@3.17/code-security/reference/code-scanning/sarif-files/troubleshoot-sarif-uploads)
- SARIF 2.1.0 (OASIS Standard) gives results:
  - `ruleId`;
  - `locations[].physicalLocation` with `artifactLocation.uri` and `region.startLine/endLine`;
  - `codeFlows`;
  - `partialFingerprints`;
  - `baselineState` (new/unchanged/updated/absent);
  - `taxonomies` / `taxa` for CWE references.

  — [OASIS SARIF v2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html) (prior knowledge, not re-fetched)
- The Microsoft SARIF SDK's `Sarif.Multitool` validates, merges and rewrites SARIF, and can match results across runs ("match-results-forward"). — [microsoft/sarif-sdk](https://github.com/microsoft/sarif-sdk) (prior knowledge, not re-fetched)

**Datasets used for paired / time-split vulnerability detection** (prior knowledge, not re-fetched)
- PrimeVul introduced chronological splits, cross-dataset deduplication, a *pairwise* evaluation (vulnerable function and its patched twin, both judged), and VD-Score (FNR at a fixed low FPR). It showed that models scoring high on older datasets collapse on realistic, paired evaluation. — [Ding et al. 2024, arXiv:2403.18624](https://arxiv.org/abs/2403.18624)
- CWE-Bench-Java (from IRIS): 120 manually validated vulnerabilities in real Java projects, averaging about 300K LOC and up to 7M. — [IRIS, arXiv:2405.17238](https://arxiv.org/abs/2405.17238)

### Inferences
- **Closest task definitions:** for Orion's task (find and localize a vuln in a whole repo, emit file / line / CWE), the closest ready-made inspect_evals tasks are CyberGym and CASTLE. CASTLE is small single-file C, a language Orion does not target. CyberGym's scorer is reproduction-based, not location-based. So the security benchmarks can be *executed* through these harnesses, but scoring would still go through Orion's own matcher.
- **PatchEval (Python/JS/Go CVEs):** it is a repair task, but its CVE instances and security tests could be mined as answer keys with fix sites. Check its CVE dates against the 2026-05-31 cutoff rule.
- **OWASP scorecard:** BenchmarkUtils is the accepted way to report OWASP Benchmark numbers (Youden-style score per CWE). Orion's SARIF could be fed to it only if BenchmarkUtils has a SARIF reader; that is unverified (see Gaps). Otherwise, compute TPR/FPR per category from `expectedresults-*.csv` in `eval/owasp.py` and state that the formula matches the scorecard's.
- **SARIF for paired comparisons:** `sarif diff` between the arms' SARIF files gives a quick "new vs absent" view. It matches on fingerprints/rules, which is the same problem `runs.compare` solved with its location pass, so it is not a scoring substitute.

### Gaps
- Whether BenchmarkUtils parses SARIF (generic or tool-specific) was not confirmed; the README did not say. Check `src/main/java/.../score/parsers/` in the repo.
- Exact OWASP Benchmark test-case counts (Java v1.2, Python) were not re-confirmed this session.
- Not checked:
  - GitHub's other SARIF limits (results per run, runs per file);
  - which inspect_evals tasks need GPUs or >16 GB RAM, beyond the "up to 32 GB" note;
  - the dates of CyberGym / PatchEval / CASTLE instances against Orion's post-2026-05-31 rule.

## Q3. What are the recommended statistical practices for small benchmarks (tens of tasks) to claim an improvement credibly?

### Takeaway
Use a **paired, within-task design**: same vulns, same repos, same model, Orion on vs off, with **clustered** uncertainty (cluster = repo). With tens of tasks, **do not use CLT/normal intervals**. Use exact or Bayesian intervals, an exact McNemar / sign test on the discordant pairs, and a cluster bootstrap. Run each arm several times and report both mean-over-runs and reliability (pass^k). Pre-register one primary metric. Validate any LLM judge against human labels with chance-corrected agreement (kappa). Do a power check up front: an eval of ~200 binary items only detects large effects.

### Cited Findings
- Miller's ("Adding Error Bars to Evals") recommendations:
  1. Report CLT standard errors.
  2. Use **clustered** standard errors when questions are grouped.
  3. Reduce variance by resampling answers K times or using next-token probabilities, and do NOT lower temperature to cut variance.
  4. Compare two models on **question-level paired differences**.
  5. Run a **power analysis**.

  — [Miller 2024, arXiv:2411.00640](https://arxiv.org/html/2411.00640v1)
- Miller's formulas and examples:
  - Paired difference SE = √(SE_A² + SE_B² − 2·SE_A·SE_B·Corr(s_A, s_B)). At correlation 0.5, pairing cuts estimator variance by 1/3.
  - Clustered vs naive SE ratios in his examples: 3.05 (DROP), 1.88 (MGSM), 1.10 (RACE-H).
  - Sample size: n = (z_{α/2}+z_β)²(ω² + σ_A²/K_A + σ_B²/K_B)/δ². Detecting a 3-point difference at 80% power needs about 969 independent questions.
  - With n = 198, raising resamples K from 1 to 10 lowers the minimum detectable effect from 13.2% to 7.5%.

  — [Miller 2024, arXiv:2411.00640](https://arxiv.org/html/2411.00640v1)
- With fewer than a few hundred datapoints, CLT-based intervals "usually dramatically underestimat[e] uncertainty". The paper offers frequentist and Bayesian alternatives, with a Python library (`bayes_evals`). — [Bowyer, Aitchison, Ivanova, ICML 2025 position paper, arXiv:2503.01747](https://arxiv.org/abs/2503.01747); [sambowyer/bayes_evals](https://github.com/sambowyer/bayes_evals)
- Agentic benchmarks often have task-setup or reward-design flaws that over- or underestimate performance "by up to 100% in relative terms". Examples: "SWE-bench Verified uses insufficient test cases" and "TAU-bench counts empty responses as successful". Applying the Agentic Benchmark Checklist to CVE-Bench "reduces the performance overestimation by 33%". — [Zhu et al., arXiv:2507.02825](https://arxiv.org/abs/2507.02825)
- pass@k has the unbiased estimator 1 − C(n−c,k)/C(n,k) from n samples with c correct. — [Chen et al. 2021, arXiv:2107.03374](https://arxiv.org/abs/2107.03374) (prior knowledge, not re-fetched)
- **pass^k** is the probability that *all* k independent trials succeed (estimated as C(c,k)/C(n,k)), a reliability measure for agents. τ-bench showed it dropping steeply as k grows. — [Yao et al. 2024 τ-bench, arXiv:2406.12045](https://arxiv.org/abs/2406.12045) (prior knowledge, not re-fetched)
- McNemar's test, for two classifiers on the same items, uses only the discordant counts (b, c). It is recommended over resampled t-tests for its low Type I error. The exact binomial form applies when b + c is small. — [Dietterich 1998, Neural Computation 10(7)](https://doi.org/10.1162/089976698300017197) (prior knowledge, not re-fetched)
- LLM-as-judge: strong LLM judges reached over 80% agreement with human preferences, about the same as human-human agreement. The same paper documents position, verbosity and self-enhancement biases. — [Zheng et al. 2023, arXiv:2306.05685](https://arxiv.org/abs/2306.05685) (prior knowledge, not re-fetched)
- Contamination controls in code evals:
  - LiveCodeBench uses time-windowed problems (collected after a model's cutoff) to detect contamination — [Jain et al. 2024, arXiv:2403.07974](https://arxiv.org/abs/2403.07974);
  - BIG-bench embeds a canary GUID string so benchmark text can be filtered out of training corpora and leakage detected — [google/BIG-bench](https://github.com/google/BIG-bench);
  - PrimeVul uses chronological train/test splits for vulnerability detection — [arXiv:2403.18624](https://arxiv.org/abs/2403.18624).

  (all prior knowledge, not re-fetched)
- **Already in Orion:**
  - `eval/report.py` has a **paired cluster bootstrap** (`paired_bootstrap`, B=10,000) that resamples REPOS, so both arms' runs for a repo move together. — local: C:/Users/h/Downloads/orion/eval/report.py (lines ~20, 339-341)
  - `eval/bakeoff.py` cluster-bootstraps PAIRS (vulnerable + patched twin kept together). — local: C:/Users/h/Downloads/orion/eval/bakeoff.py (~184)
  - Gate 1 requires the pair_correct gain CI > 0 and the patched_silence CI ≥ −5 pp. — local: C:/Users/h/Downloads/orion/CLAUDE.md
  - The protocol is 3 runs per arm per repo, and the `eval-prereg-v1` / `orion-lm-prereg-v1` tags freeze arms, answer keys, prompt, matcher and hypotheses before scoring. — local: C:/Users/h/Downloads/orion/CLAUDE.md

### Inferences
- **Practical recipe for "Orion + model vs model alone" on tens of vulns:**
  1. Paired unit = one ground-truth vuln (keyed by fix site), clustered by repo. Both arms see identical checkouts (.git-free), the same model ID and the same frozen sampling.
  2. Primary metric (pre-registered, exactly one): per-vuln caught-right-type recall averaged over the 3 runs. Report the false-alarm rate on the OWASP/patched set as a co-primary with a non-inferiority margin. Orion's design already uses 5 pp.
  3. Test: exact McNemar / sign test on vuln-level discordant pairs (majority-of-3 or mean-of-3 per vuln), plus the existing repo-cluster paired bootstrap CI on the difference. If they disagree, report both. For tens of items, also give exact (Clopper-Pearson / Wilson) or Beta-posterior intervals per arm instead of ±1.96·SE.
  4. Report pass@1 (mean over runs), pass^3 ("caught in all 3 runs", reliability) and pass@3 ("caught in any run", ceiling). A scanner is run once by users, so pass@1 and pass^3 matter more than pass@3.
  5. Run a power check before running: by Miller's formula, ~200 binary items detect only roughly a 13-point gap at K=1, and fewer items detect only larger gaps. Frame a small set as "can detect large effects only", or pool runs (K=3) to shrink the minimum detectable effect.
  6. Correct for multiple comparisons (Holm) across secondary metrics and ablations, or label them exploratory.
- **LLM judge:** validate on ≥100 Krish-labelled items, as Orion's rule already says. Report Cohen's kappa (two labelers, or judge vs human), not raw agreement. Freeze the judge prompt and model before scoring, and report credited items with and without the judge (a sensitivity analysis).
- **Contamination:**
  - Orion's post-2026-05-31 advisory + fix rule is a time split. Add a canary GUID to manifests and answer keys so leaks into future training corpora can be detected.
  - Run a memorization probe: ask the plain model to name the CVE or fix for a repo/version without code. A hit means the row is control-tier.
  - Stripping `.git` (the fix-commit leak) and confining reads stay essential.
- **Failed runs:** keep them MISSING with worst-case bounds (Orion's Phase 5 rule). That is consistent with ABC's warning about counting empty or failed responses as outcomes.

### Gaps
- I did not fetch Bowyer et al.'s full text, so their exact recommended methods (e.g. Beta-Binomial posteriors, Wilson intervals, paired Bayesian comparison) are not quoted. The `bayes_evals` README should be checked.
- I found no AISI, CAISI or METR methodology post giving minimum run counts for agentic security evals (not searched in depth within budget).
- I found no source specifically validating cluster-bootstrap coverage when the number of clusters is very small (e.g. under 10 repos). This is a known weakness of bootstraps with few clusters and should be checked before trusting repo-cluster CIs on a handful of repos.

## Q4. How do recent papers (2024-2026) evaluating tool-augmented or retrieval-augmented security/code agents structure their comparisons and report cost?

### Takeaway
The common structure:
1. Base model(s) alone, and a classic SAST tool (CodeQL / Semgrep), as baselines.
2. The tool-augmented system on several base models.
3. Ablations that remove one component at a time.
4. Headline counts of detected vulns, plus a false-discovery or precision figure.

Cost reporting is uneven. Agent-methodology work (Kapoor et al.; Princeton HAL, ICLR 2026) argues that accuracy must be reported **against dollar cost on a Pareto frontier**. Some 2026 security papers report $ per valid finding. Many CPG-agent papers report tool calls but not dollars.

### Cited Findings
- **IRIS** (ICLR 2025): an LLM mines CWE-specific taint specs for library APIs that feed CodeQL, then a second LLM stage triages paths. On CWE-Bench-Java (120 vulns):
  - v1 reports 69 detected with GPT-4 vs 27 for the SOTA static tool;
  - v2 evaluates 7 open and closed LLMs, best GPT-4 at 55 detected, "28 (103.7%) more than CodeQL";
  - false discovery: v1 claims more than 80% fewer false alarms in the best case; v2 reports average FDR 84.82%, 5.21% lower than CodeQL's.
  - The numbers differ between versions, so cite the final ICLR version.

  — [IRIS arXiv:2405.17238 (v1/v2)](https://arxiv.org/abs/2405.17238); [ICLR 2025 proceedings](https://proceedings.iclr.cc/paper_files/paper/2025/hash/582d4e27fa24168f3af1f4582655034b-Abstract-Conference.html)
- **VulAgentRL** ("Graph Is the Verifier", 2026): an RL policy queries a CPG (callers, callees, dataflow) at inference; at training, the same graph checks the evidence the policy cites. Reported claims:
  - 71.7% of vulnerable functions need evidence from outside the function;
  - it beats "state-of-the-art baselines, including frontier models" on a strict **pair-wise-correct** metric with fewer tool calls;
  - it uses a repository-level split to prevent leakage, an out-of-distribution corpus and a class-imbalance evaluation.

  Dollar cost, and whether a same-model-without-graph arm is included, are not stated in the abstract. — [arXiv:2607.26656](https://arxiv.org/abs/2607.26656)
- **LLMxCPG** (USENIX Security 2025) integrates LLMs with code property graphs for vulnerability detection. Its cost figures were not seen. — [Awesome-LLMs-for-Vulnerability-Detection index](https://github.com/huhusmang/Awesome-LLMs-for-Vulnerability-Detection); mentioned in [arXiv:2604.21282](https://arxiv.org/html/2604.21282v1)
- **Cost per finding:** a 2026 Node.js taint-style detection + confirmation paper (LLMVD.Js) reports about $0.084 per valid exploit on successfully exploited packages ($0.050 amortized). — [arXiv:2604.20179](https://arxiv.org/pdf/2604.20179)
- A 2026 heterogeneous multi-agent detector treats cost as a design objective and includes an ablation table with cost per sample. — [arXiv:2604.21282](https://arxiv.org/abs/2604.21282)
- MultiVer (2026, zero-shot multi-agent detection) reports ablations; no cost-per-vuln metric was seen in the snippets. — [arXiv:2602.17875](https://arxiv.org/abs/2602.17875)
- **Cost methodology:**
  - "AI Agents That Matter" argues for cost-controlled evaluation, joint optimization of accuracy and cost, and **dollar** cost rather than proxies such as parameter count. Simple baselines beat complex agent architectures on HumanEval at much lower cost — [Kapoor et al., arXiv:2407.01502](https://arxiv.org/html/2407.01502v1).
  - HAL (Holistic Agent Leaderboard; paper accepted to ICLR 2026) decouples models from benchmarks and publishes **cost-accuracy Pareto frontiers** — [hal.cs.princeton.edu](https://hal.cs.princeton.edu/).
- **Orion's existing cost practice:**
  - headline = efficiency (vulns per 1M tokens) + false-alarm rate, with recall reported either way;
  - per-message tokens kept for hung runs (labelled lower bound);
  - cost re-priced after finding that `claude -p` writes the 1-hour cache at 2x (NodeGoat/PyGoat Opus runs $0.73/$1.01, not $0.61/$0.84).

  — local: C:/Users/h/Downloads/orion/CLAUDE.md

### Inferences
- **A credible Orion paper table would mirror IRIS's structure while closing its gaps:**
  1. Rows: plain model (same model, same file-read tools, no graph); Orion full; ablations graph-off (no `run_cypher`), semantic-search-off, verifier-off (raw discovery leads), shortlists on/off, runtime on/off; Semgrep/CodeQL as non-LLM anchors.
  2. Columns: caught (L1 location), caught-right-type (L2 CWE), false alarms on patched twins and OWASP, $ and tokens per run (cache-write pricing stated), vulns per 1M tokens, $ per confirmed true finding, wall time.
  3. A cost-accuracy Pareto plot (HAL style) across models × arms.
  4. Orion already has the flags for ablations: `--shortlists`, `--semantic-mode`, `--verify-routing`, `--runtime`, `--lm-verify` (CLAUDE.md). A verifier-off arm would need discovery leads scored directly from `leads.json`.
- **Metric choice:** papers increasingly use **pair-wise correct** (vulnerable flagged AND patched twin silent) because it penalizes "always say vulnerable". Orion's bake-off already uses `pair_correct`, so the headline CVE comparison should report it too, not recall alone.
- **Model-version drift:** IRIS's numbers shifted between versions. Pin model IDs and the scoring code version (pre-registration tag), and publish per-vuln outcome tables so readers can recompute.

### Gaps
- Not seen within budget: the full ablation tables and dollar costs of IRIS, LLMxCPG and VulAgentRL (only abstracts or snippets were reachable).
- Not reviewed: tool-augmented code-retrieval papers (e.g. repo-level RAG for SWE-bench such as Agentless / CodeRAG-style ablations).
- Not reviewed: CyberGym's and BountyBench's own cost-reporting conventions.
- I found no 2024-2026 security paper that reports a *same-model, tool on/off* paired comparison with clustered CIs and McNemar together. That combination appears to be uncommon, but this is an absence-of-evidence statement from a limited search.
