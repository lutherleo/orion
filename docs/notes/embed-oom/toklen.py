"""Token lengths of every 40-line window (a superset of embed.index's method chunks) and every 60-line
fallback block, per file, with the model's own tokenizer. No Neo4j, no GPU."""
import json
import os
import sys

from sentence_transformers import SentenceTransformer

sys.path.insert(0, os.path.expanduser("~/orion"))
from orion import config, embed  # noqa: E402

root, exts = sys.argv[1], tuple(sys.argv[2].split(","))
embed._patch_transformers_compat()
model = SentenceTransformer(config.EMBED_MODEL, trust_remote_code=True, device="cpu")
tok = model.tokenizer
print("max_seq_length", model.max_seq_length, "tokenizer.model_max_length", tok.model_max_length)

rows = []
for d, _, files in os.walk(root):
    for f in files:
        if not f.endswith(exts):
            continue
        p = os.path.join(d, f)
        rel = os.path.relpath(p, root)
        with open(p, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        ntok = [len(tok(l, add_special_tokens=False)["input_ids"]) for l in lines]
        nchar = [len(l) for l in lines]
        best = (0, 0)
        for s in range(len(lines)):
            t = sum(ntok[s:s + embed.METHOD_LINE_WINDOW])
            if t > best[0]:
                best = (t, s + 1)
        longest = max(range(len(lines)), key=lambda i: ntok[i]) if lines else 0
        rows.append({"file": rel, "lines": len(lines), "max_window_tokens": best[0], "at_line": best[1],
                     "longest_line": longest + 1, "longest_line_tokens": ntok[longest] if lines else 0,
                     "longest_line_chars": nchar[longest] if lines else 0, "file_tokens": sum(ntok)})
rows.sort(key=lambda r: -r["max_window_tokens"])
print("files", len(rows))
over = [r for r in rows if r["max_window_tokens"] > 512]
print("files with a 40-line window > 512 tokens:", len(over), " > 2048:",
      sum(r["max_window_tokens"] > 2048 for r in rows), " > 8192:", sum(r["max_window_tokens"] > 8192 for r in rows))
for r in rows[:15]:
    print(json.dumps(r))
