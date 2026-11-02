"""Token length of every 60-line block of a repo's files: the chunks a file with no located method falls back to
(embed._build_chunks). Tokenizer only, no GPU, no Neo4j.

    ~/orion/.venv/bin/python blocks.py <root> .java
"""
import os
import sys

from transformers import AutoTokenizer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from orion import config, embed  # noqa: E402

root, ext = sys.argv[1], sys.argv[2]
tok = AutoTokenizer.from_pretrained(config.EMBED_MODEL, trust_remote_code=True)
rows = []
for d, _, fs in os.walk(root):
    for f in fs:
        if f.endswith(ext):
            with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            for start in range(0, len(lines), embed.FALLBACK_BLOCK_LINES):
                block = lines[start:start + embed.FALLBACK_BLOCK_LINES]
                if any(line.strip() for line in block):
                    text = "".join(block)
                    rows.append((len(tok(text)["input_ids"]), len(text), f, start + 1))
rows.sort(reverse=True)
print(len(rows), "blocks; tokens, characters, file, first line:")
for r in rows[:16]:
    print(r)
print("over 2048 tokens:", sum(r[0] > 2048 for r in rows), " over 4000:", sum(r[0] > 4000 for r in rows))
