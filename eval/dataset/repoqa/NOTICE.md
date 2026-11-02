# RepoQA SNF data: source and attribution

`eval/repoqa.py` reads the **RepoQA** dataset by the EvalPlus team (https://github.com/evalplus/repoqa,
`main` at `ae876deb1365dbf5a15b0533723c8ed123eee586`; data release
https://github.com/evalplus/repoqa_release, tag `2024-06-23`), licensed under the Apache License 2.0.
Copyright (c) 2024 EvalPlus Team.

File used: `repoqa-2024-06-23.json.gz` (RepoQA's default `REPOQA_DATA_VERSION`), 12,525,636 bytes,
sha256 `c050a2ad90a7df89d9dc1f1c3b3b20683edd20a56293b35fcaae43dec115d681`, kept outside the repo
(WSL `~/bench-data/repoqa/data/`). No dataset file or repository content is committed here.

What is taken from it: the "Search Needle Function" (SNF) task for the Python, Java and TypeScript
subsets only (10 repos x 10 needles each): each repo's name, pinned `commit_sha` and `entrypoint_path`,
and each needle's `name`, `path`, `start_line` / `end_line` (tree-sitter rows: `start_line` is 0-based,
`end_line` is exclusive, so the 1-based inclusive span is `start_line + 1 .. end_line`) and its
natural-language `description`. The descriptions were written by RepoQA's own pipeline.

The scanned code comes from each repository at RepoQA's pinned commit (depth-1 fetch, sparse to the
entrypoint path), under that repository's own license (MIT or Apache-2.0, per GitHub).
