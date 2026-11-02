"""Estimated cost of every encode batch for a repo, from its chunk lengths (tokenizer only, CPU, no Neo4j).

Chunks are approximated as in measure.py (Python: ast def/class lines + line 1; Java: a declaration regex), each
a 40-line window. Batches are formed the way sentence-transformers forms them: sorted by -len(text), 16 at a
time, each padded to its longest member (capped at max_seq_length). Memory per batch ~ 203 B x n x L^2
(measured: 12.72 GiB for one 8192-token chunk); attention time ~ n x L^2, given relative to one 8192-token chunk.

    CUDA_VISIBLE_DEVICES= ~/orion/.venv/bin/python batches.py <root> .py|.java
"""
import ast
import os
import re
import sys

from transformers import AutoTokenizer

sys.path.insert(0, os.path.expanduser("~/orion"))
from orion import config, embed  # noqa: E402

JAVA_DECL = re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|default)\s+)*"
                       r"[\w<>\[\], .?]+\s+\w+\s*\([^;]*$")
NOT_DECL = re.compile(r"^\s*(return|new|if|for|while|switch|else|throw)\b")


def starts_py(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return [1]
    return sorted({1, *(n.lineno for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))})


def starts_java(path):
    return [i for i, l in enumerate(open(path, encoding="utf-8", errors="replace"), 1)
            if JAVA_DECL.match(l) and not NOT_DECL.match(l)]


root, ext = sys.argv[1], sys.argv[2]
starts = starts_py if ext == ".py" else starts_java
tok = AutoTokenizer.from_pretrained(config.EMBED_MODEL)
cap = 8192
texts = []
for d, _, fs in os.walk(root):
    for f in fs:
        if f.endswith(ext):
            p = os.path.join(d, f)
            for s in starts(p):
                w = embed._read_window(d, f, s, embed.METHOD_LINE_WINDOW)
                if w:
                    texts.append(w[0])
texts.sort(key=lambda t: -len(t))
lens = [min(len(tok(t)["input_ids"]), cap) for t in texts]
B = 16
rows = []
for i in range(0, len(texts), B):
    n, L = len(lens[i:i + B]), max(lens[i:i + B])
    rows.append((i // B, n, L, 203 * n * L * L / 2**30, n * L * L / cap**2))
print(f"{len(texts)} chunks, {len(rows)} batches")
print("batch  n  Lmax  est_GiB  rel_time")
for r in rows[:12]:
    print(f"{r[0]:5d} {r[1]:2d} {r[2]:5d} {r[3]:8.2f} {r[4]:8.2f}")
print("batches over 4 GiB:", sum(r[3] > 4 for r in rows), " over 32 GiB:", sum(r[3] > 32 for r in rows))
print("sum rel_time, all batches:", round(sum(r[4] for r in rows), 2),
      " | if batches over 32 GiB go one chunk at a time:",
      round(sum(r[4] for r in rows if r[3] <= 32)
            + sum(sum(l * l for l in lens[r[0] * B:r[0] * B + r[1]]) / cap**2 for r in rows if r[3] > 32), 2))
