"""One 8192-token chunk (web3 offchain_resolver.py lines 1-40, the <module> window): batch-1 cost on cuda and on cpu.
Plus an estimate of how many web3 / gctoolkit chunks are long (def/class lines via ast/regex + line 1)."""
import ast
import os
import re
import resource
import sys
import time

import torch
from sentence_transformers import SentenceTransformer

sys.path.insert(0, os.path.expanduser("~/orion"))
from orion import config, embed  # noqa: E402

embed._patch_transformers_compat()
W3 = os.path.expanduser("~/bench-data/repoqa/repos/ethereum__web3.py/web3")
GC = os.path.expanduser("~/bench-data/repoqa/repos/microsoft__gctoolkit/parser/src/main/java")


def window(path, line, n=embed.METHOD_LINE_WINDOW):
    r = embed._read_window(os.path.dirname(path), os.path.basename(path), line, n)
    return r[0] if r else None


def starts_py(path):
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
    except SyntaxError:
        return [1]
    return sorted({1, *(n.lineno for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))})


JAVA_DEF = re.compile(r"^\s*(public|private|protected|static|final|abstract|synchronized|default|\w+<|[\w\[\]<>, ]+\s+\w+\s*\()")


def starts_java(path):
    out = []
    for i, l in enumerate(open(path, encoding="utf-8", errors="replace"), 1):
        if re.match(r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|default)\s+)*[\w<>\[\], .?]+\s+\w+\s*\([^;]*$", l) \
                and not re.match(r"^\s*(return|new|if|for|while|switch|else|throw)\b", l):
            out.append(i)
    return out


model = SentenceTransformer(config.EMBED_MODEL, trust_remote_code=True)
tok = model.tokenizer
print("device", model.device)

for name, root, ext, starts in (("web3", W3, ".py", starts_py), ("gctoolkit", GC, ".java", starts_java)):
    lens = []
    for d, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(ext):
                p = os.path.join(d, f)
                for s in starts(p):
                    t = window(p, s)
                    if t:
                        lens.append(len(tok(t)["input_ids"]))
    lens.sort(reverse=True)
    print(name, "approx chunks", len(lens), "top16", lens[:16],
          ">8192:", sum(x > 8192 for x in lens), ">4096:", sum(x > 4096 for x in lens),
          ">2048:", sum(x > 2048 for x in lens), ">1024:", sum(x > 1024 for x in lens))

text = window(os.path.join(W3, "_utils/contract_sources/contract_data/offchain_resolver.py"), 1)
print("chunk tokens", len(tok(text)["input_ids"]))

torch.cuda.reset_peak_memory_stats()
t0 = time.monotonic()
v_gpu = model.encode([text], batch_size=16, show_progress_bar=False, convert_to_numpy=True)
torch.cuda.synchronize()
print(f"cuda batch-1: {time.monotonic() - t0:.1f}s peak {torch.cuda.max_memory_allocated() / 2**30:.2f} GiB")

model.to("cpu")
torch.cuda.empty_cache()
r0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
t0 = time.monotonic()
v_cpu = model.encode([text], batch_size=16, show_progress_bar=False, convert_to_numpy=True)
print(f"cpu batch-1: {time.monotonic() - t0:.1f}s maxrss {r0 / 2**20:.2f} -> "
      f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20:.2f} GiB")
import numpy as np  # noqa: E402
print("cos(gpu, cpu)", float(np.dot(v_gpu[0], v_cpu[0]) / np.linalg.norm(v_gpu[0]) / np.linalg.norm(v_cpu[0])),
      "max|diff|", float(np.abs(v_gpu - v_cpu).max()))
