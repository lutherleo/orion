"""`embed.index` survives chunks too long for the device instead of dying on out-of-memory.

The embedding model reads up to 8192 tokens and pads each batch of 16 to its longest chunk; its attention
for that batch is 16 x 12 x L^2 floats, 48 GiB at 8192 (ethereum/web3.py's bytecode-string modules hit it on
a 4 GB GPU, microsoft/gctoolkit's regex walls 12.9 GiB). `embed._encode` keeps the one plain
`model.encode(texts, batch_size=16)` call; only when it runs out of memory are the batches redone smaller,
a lone chunk that still does not fit going to the CPU. Token-free: a fake model whose "memory" is a budget on
len(batch) x longest^2, no torch, no Neo4j.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import numpy as np
import pytest

from orion import embed
from orion.graph import schema

CUDA_OOM = "CUDA out of memory. Tried to allocate 48.00 GiB. GPU 0 has a total capacity of 4.00 GiB"


def vec(text: str) -> np.ndarray:
    return np.array([len(text), ord(text[0]), 1.0], dtype=np.float32)


class FakeModel:
    """Embeds like sentence-transformers' encode (length-sorted batches of `batch_size`), but a batch whose
    len(batch) x longest^2 exceeds the device's budget raises the allocator's own error."""

    def __init__(self, budget: dict, device: str = "cuda:0", error: Exception | None = None):
        self.budget, self.device, self.error = budget, device, error
        self.calls: list[tuple[str, list[str], int]] = []
        self.moves: list[str] = []

    def to(self, device):
        self.device = str(device)
        self.moves.append(self.device)
        return self

    def encode(self, texts, batch_size, show_progress_bar, convert_to_numpy):
        assert show_progress_bar is False and convert_to_numpy is True
        self.calls.append((self.device, list(texts), batch_size))
        if self.error is not None:
            raise self.error
        order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))
        for start in range(0, len(order), batch_size):
            b = [texts[i] for i in order[start:start + batch_size]]
            if len(b) * max(len(t) for t in b) ** 2 > self.budget[self.device]:
                raise (RuntimeError(CUDA_OOM) if self.device != "cpu" else
                       RuntimeError("[enforce fail at alloc_cpu.cpp:117] DefaultCPUAllocator: can't allocate memory"))
        return np.stack([vec(t) for t in texts])


def _texts():
    # longest first: 100 (too big for the GPU even alone), 40 (fits alone, not with 15 others), 19 short
    return ["s" * 5] * 10 + ["H" * 100] + ["s" * 5] * 9 + ["M" * 40]


def test_a_run_that_fits_is_the_one_plain_call(capsys):
    texts = _texts()
    model = FakeModel({"cuda:0": float("inf")})
    warned = []
    out = embed._encode(model, texts, on_warn=warned.append)
    assert model.calls == [("cuda:0", texts, 16)]          # exactly what index always called, nothing more
    assert np.array_equal(out, np.stack([vec(t) for t in texts]))
    assert warned == [] and capsys.readouterr().err == ""
    assert model.moves == []


def test_out_of_memory_redoes_only_what_does_not_fit():
    texts = _texts()
    model = FakeModel({"cuda:0": 2000, "cpu": float("inf")})
    warned = []
    out = embed._encode(model, texts, on_warn=warned.append)

    assert np.array_equal(out, np.stack([vec(t) for t in texts]))     # every chunk, in input order
    plain, *rest = model.calls
    assert plain == ("cuda:0", texts, 16)                                # the plain call came first
    whole = [c for c in rest if len(c[1]) > 1]
    assert [len(c[1]) for c in whole] == [16, 5]                         # longest-first batches of 16, tried whole
    assert "H" * 100 in whole[0][1] and "M" * 40 in whole[0][1]
    alone_gpu = [c[1][0] for c in rest if len(c[1]) == 1 and c[0] == "cuda:0"]
    assert sorted(alone_gpu, key=len)[-2:] == ["M" * 40, "H" * 100]    # the failing batch, one chunk at a time
    assert len(alone_gpu) == 16
    assert [c[1] for c in rest if c[0] == "cpu"] == [["H" * 100]]       # only the chunk too big alone
    assert model.moves == ["cpu", "cuda:0"] and model.device == "cuda:0"  # and the model went back
    assert warned == ["embedding ran out of memory (RuntimeError: " + CUDA_OOM[:160] + "); re-embedded 1 of 2 "
                      "batches one chunk at a time, 1 chunk(s) on the CPU"]


def test_the_warning_goes_to_stderr_without_a_listener(capsys):
    embed._encode(FakeModel({"cuda:0": 2000, "cpu": float("inf")}), _texts())
    assert capsys.readouterr().err.startswith("orion embed: embedding ran out of memory (RuntimeError: CUDA out")


def test_an_error_that_is_not_out_of_memory_raises_unchanged():
    model = FakeModel({"cuda:0": float("inf")}, error=ValueError("bad input"))
    with pytest.raises(ValueError, match="bad input"):
        embed._encode(model, _texts())
    assert len(model.calls) == 1 and model.moves == []


def test_a_chunk_too_big_even_for_the_cpu_still_raises():
    model = FakeModel({"cuda:0": 2000, "cpu": 5000})
    with pytest.raises(RuntimeError, match="can't allocate memory"):
        embed._encode(model, _texts())
    assert model.device == "cuda:0"                                     # put back even when it fails


def test_a_model_already_on_the_cpu_splits_without_moving():
    texts = _texts()
    model = FakeModel({"cpu": 2000 * 6}, device="cpu")                  # 100 alone fits, its batch does not
    out = embed._encode(model, texts)
    assert np.array_equal(out, np.stack([vec(t) for t in texts]))
    assert model.moves == []
    model = FakeModel({"cpu": 2000}, device="cpu")                      # 100 alone does not: nowhere left
    with pytest.raises(RuntimeError, match="can't allocate memory"):
        embed._encode(model, texts)


@pytest.mark.parametrize("exc, oom", [
    (RuntimeError(CUDA_OOM), True),
    (RuntimeError("MPS backend out of memory (MPS allocated: 9.00 GB)"), True),
    (RuntimeError("DefaultCPUAllocator: can't allocate memory: you tried to allocate 51539607552 bytes"), True),
    (RuntimeError("[enforce fail at alloc_cpu.cpp] not enough memory: you tried to allocate 3 GB"), True),
    (MemoryError(), True),
    (RuntimeError("The size of tensor a (8193) must match the size of tensor b (8192)"), False),
    (ValueError("out of memory"), False),                               # not an allocator error type
])
def test_is_oom(exc, oom):
    assert embed._is_oom(exc) is oom


def test_torch_cuda_out_of_memory_counts():
    torch = pytest.importorskip("torch")
    assert embed._is_oom(torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 12.90 GiB"))


class _FakeSession:
    def __init__(self, runs):
        self.runs = runs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.runs.append((query, params))
        return []


def test_index_writes_every_chunk_after_an_out_of_memory(tmp_path, monkeypatch):
    """index() itself: the fallback's vectors reach the Chunk write, and the warning reaches on_warn."""
    (tmp_path / "big.py").write_text("x = '" + "ab" * 60 + "'\n", encoding="utf-8")
    (tmp_path / "small.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    batch = schema.Batch("s")
    for name, line in (("big.py", 1), ("small.py", 1)):
        batch.emit_node("CpgMethod", {"full_name": f"{name}:<module>", "file_path": name, "line": line})
        batch.emit_node("CpgFile", {"uid": name, "file_path": name})
    runs: list = []
    driver = SimpleNamespace(session=lambda **_: _FakeSession(runs), close=lambda: None)
    model = FakeModel({"cuda:0": 20_000, "cpu": float("inf")})   # 2 x 127^2 together: no; 127^2 alone: yes
    monkeypatch.setattr(embed, "_get_model", lambda: model)
    monkeypatch.setattr(embed, "_driver", lambda: driver)
    monkeypatch.setattr(embed.config, "SEMANTIC_BACKEND", "neo4j")
    monkeypatch.setattr(embed.config, "SEMANTIC_MODE", "vector")
    warned = []

    embed.index(os.fspath(tmp_path), "s", batch=batch, on_warn=warned.append)

    (query, params), = [r for r in runs if "CREATE (c:Chunk" in r[0]]
    rows = {r["file"]: r for r in params["rows"]}
    assert set(rows) == {"big.py", "small.py"}
    for r in rows.values():
        assert r["embedding"] == vec(r["text"]).tolist()
    assert len(warned) == 1 and "re-embedded 1 of 1 batches one chunk at a time, 0 chunk(s) on the CPU" in warned[0]
