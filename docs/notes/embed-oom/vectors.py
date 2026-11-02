"""What the RepoQA re-run could not show about the fallback's vectors, on the real model (no Neo4j, no tokens):

1. A chunk embedded alone vs inside a padded batch of 16, the only difference the split path makes.
2. The CPU path, which the re-run never reached (0 chunks on the CPU): the real `embed._encode`, with an injected
   out-of-memory for the longest chunk while the model is on the GPU.

Chunks are 40-line windows of web3.py, kept short (<= 600 tokens) so this takes seconds and little memory.

    ~/orion/.venv/bin/python vectors.py [<root of python files>]
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from orion import embed  # noqa: E402

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/bench-data/repoqa/repos/ethereum__web3.py/web3")
CAP = 600


def compare(name, a, b):
    cos = (a * b).sum(axis=1) / np.linalg.norm(a, axis=1) / np.linalg.norm(b, axis=1)
    print(f"{name}: min cosine {cos.min():.9f}, largest |difference| {np.abs(a - b).max():.2e}", flush=True)


model = embed._get_model()
start = model.device
print("device", start, "module", embed.__file__, flush=True)
tok = model.tokenizer

sized = []
for d, _, fs in sorted(os.walk(ROOT)):
    for f in sorted(fs):
        if f.endswith(".py") and "contract_data" not in d:
            w = embed._read_window(d, f, 1, embed.METHOD_LINE_WINDOW)
            if w:
                sized.append((len(tok(w[0])["input_ids"]), w[0]))
sized = sorted((s for s in sized if s[0] <= CAP), key=lambda s: -s[0])
picks = sized[:1] + sized[1::max(1, (len(sized) - 1) // 15)][:15]
texts = [t for _, t in picks]
lens = [n for n, _ in picks]
print(f"{len(texts)} chunks, {min(lens)}-{max(lens)} tokens", flush=True)


def encode(ts, batch_size):
    return model.encode(ts, batch_size=batch_size, show_progress_bar=False, convert_to_numpy=True)


together = encode(texts, 16)
alone = np.stack([encode([t], 1)[0] for t in texts])
compare(f"{start}, batch of 16 vs alone", together, alone)

longest = texts[lens.index(max(lens))]
plain = model.encode


def oom_for_longest(ts, *a, **kw):
    if str(model.device) != "cpu" and longest in ts:
        raise RuntimeError("CUDA out of memory. Tried to allocate 1 GiB (injected)")
    return plain(ts, *a, **kw)


messages = []
model.encode = oom_for_longest
try:
    fallback = embed._encode(model, texts, on_warn=messages.append)
finally:
    del model.encode
print("fallback said:", messages, flush=True)
print("device after the fallback:", model.device, flush=True)
compare("fallback vs plain batch", fallback, together)
assert str(model.device) == str(start), "the model did not go back to its device"
assert len(messages) == 1, messages
