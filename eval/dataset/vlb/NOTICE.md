# VLB answer keys: source and attribution

`keys.json` is derived from the **Vulnerability Localization Benchmark** by Cisco Foundation AI
(https://github.com/cisco-foundation-ai/vulnerability-localization-benchmark, commit
`fa50b671aa0b954d73691d6ef8d592d42a2ac1e0`; paper arXiv 2609.15939), licensed under the Apache License 2.0.
Copyright 2026 Cisco Foundation AI.

What is taken from VLB's `data/manifest.csv`: each task's id, repository, ecosystem, language, CWE ids and
CWE descriptions, GHSA and CVE ids, year, vulnerable and fix commit SHAs, and its ground-truth files (filtered
with VLB's own rule: code files only, no tests). VLB's metadata comes from the GitHub Security Advisory
Database. No repository content is included.

What Orion adds (`eval/vlb.py`): `scope` (which tasks Orion's languages cover) and `fix_sites`, the
vulnerable-file line ranges each fix hunk changes, read from GitHub's compare diff between the two commits.
The fix sites are proposed and unsigned; VLB's ground-truth files are VLB's.
