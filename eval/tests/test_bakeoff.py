import json

import pytest

from orion.lm.model import GenResult
from eval import bakeoff

CASE = {"flow_id": "F0", "rank": 0, "sink_category": None, "source_uid": None, "sink_uid": None, "nodes": [],
        "snippets": [{"role": "target", "file": "a.py", "function": "f", "start": 1, "end": 2, "code": "x"}]}
LEAD = {"flow_id": "F0", "file": "a.py", "function": "f", "line": 1, "cwe": "CWE-89", "confidence": "HIGH"}
FIELDS = {"n", "vuln_recall", "patched_silence", "negative_silence", "pair_correct", "parse_fail_rate",
          "tokens", "tok_per_s", "model", "peak_rss_mb", "train_cutoff"}


def _dev(k):
    rows = []
    for i in range(k):
        rows.append({"pair_id": f"P{i}", "label": "vulnerable", "case": CASE, "lead": LEAD, "split": "dev"})
        rows.append({"pair_id": f"P{i}", "label": "patched", "case": CASE, "lead": None, "split": "dev"})
    rows.append({"pair_id": "X", "label": "vulnerable", "case": CASE, "lead": LEAD, "split": "train"})
    return rows


class Scripted:
    def __init__(self, log):
        self.log = log

    def generate(self, messages):
        self.log.append(messages[1]["content"])
        return GenResult(text=json.dumps({"action": "final", "leads": []}), prompt_tokens=5,
                         completion_tokens=1, cached_tokens=0)


def test_run_candidate_fields_and_loaded_path():
    log, paths = [], []

    def load(path):
        paths.append(path)
        return Scripted(log)

    out = bakeoff.run_candidate("m/a", _dev(3), schema_text="S", load=load, sample_n=4, seed=1,
                                train_cutoff="2025-01")
    assert FIELDS <= set(out) and out["model"] == "m/a" and paths == ["m/a"]
    assert out["train_cutoff"] == "2025-01" and out["n"] == 4 and len(log) == 4
    assert out["peak_rss_mb"] is None  # in-process: a whole-process high-water mark would mislead
    assert bakeoff.run_candidate("m/a", _dev(3), schema_text="S", load=load, sample_n=4)["train_cutoff"] is None


def test_sample_identical_across_candidates_and_seeded():
    la, lb, lc = [], [], []
    bakeoff.run_candidate("a", _dev(10), schema_text="S", load=lambda p: Scripted(la), sample_n=6, seed=0)
    bakeoff.run_candidate("b", _dev(10), schema_text="S", load=lambda p: Scripted(lb), sample_n=6, seed=0)
    bakeoff.run_candidate("c", _dev(10), schema_text="S", load=lambda p: Scripted(lc), sample_n=6, seed=5)
    assert la == lb and len(la) == 6


def test_table_names_every_model():
    rows = [{"model": "gemma-e4b", "n": 4, "vuln_recall": 0.5, "patched_silence": 1.0, "negative_silence": None,
             "pair_correct": 0.5, "parse_fail_rate": 0.0, "tokens": 10, "tok_per_s": None,
             "peak_rss_mb": None, "train_cutoff": None},
            {"model": "granite-8b", "n": 4, "vuln_recall": 0.25, "patched_silence": 0.5, "negative_silence": 1.0,
             "pair_correct": 0.25, "parse_fail_rate": 0.1, "tokens": 12, "tok_per_s": 3.5,
             "peak_rss_mb": 4096.0, "train_cutoff": "2025-06"}]
    t = bakeoff.table(rows)
    assert "gemma-e4b" in t and "granite-8b" in t and "n/a" in t and "4096" in t


def test_isolated_uses_ru_maxrss(monkeypatch):
    resource = pytest.importorskip("resource")   # POSIX-only
    import sys

    class RU:
        ru_maxrss = 3 * 1024 * 1024  # bytes on macOS, KB on Linux: both give 3 MB / 3072 MB below

    monkeypatch.setattr(resource, "getrusage", lambda who: RU)
    out = bakeoff.run_candidate("m", _dev(1), schema_text="S", load=lambda p: Scripted([]), isolated=True)
    assert out["peak_rss_mb"] == (3.0 if sys.platform == "darwin" else 3072.0)


def test_run_all_spawns_one_child_per_candidate_and_parses_json():
    cmds = []

    def runner(cmd):
        cmds.append(cmd)
        m = cmd[cmd.index("--one") + 1]
        return "noise line\n" + json.dumps({"model": m, "n": 1, "peak_rss_mb": 12.5})

    rows = bakeoff.run_all(["a", "b"], "dev.jsonl", sample_n=7, seed=3, train_cutoffs={"a": "2025-01"}, run=runner)
    assert [r["model"] for r in rows] == ["a", "b"] and len(cmds) == 2
    assert "eval.bakeoff" in cmds[0] and "--sample-n" in cmds[0] and "7" in cmds[0]
    assert "--train-cutoff" in cmds[0] and "--train-cutoff" not in cmds[1]


def _flaky(bad_output):
    def runner(cmd):
        m = cmd[cmd.index("--one") + 1]
        if m == "bad":
            if isinstance(bad_output, Exception):
                raise bad_output
            return bad_output
        return json.dumps({"model": m, "n": 1, "peak_rss_mb": 1.0})
    return runner


def _check_error_row(rows):
    assert [r["model"] for r in rows] == ["a", "bad", "c"]
    assert rows[0]["n"] == 1 and rows[2]["n"] == 1
    bad = rows[1]
    assert bad["error"] and set(bakeoff.COLUMNS) - {"model"} <= set(bad)
    assert all(bad[c] is None for c in bakeoff.COLUMNS if c != "model")


def test_run_all_child_crash_becomes_error_row():
    import subprocess
    exc = subprocess.CalledProcessError(1, ["x"], output="", stderr="Traceback\nOOM killed")
    _check_error_row(bakeoff.run_all(["a", "bad", "c"], "d.jsonl", run=_flaky(exc)))
    rows = bakeoff.run_all(["a", "bad", "c"], "d.jsonl", run=_flaky(exc))
    assert "OOM killed" in rows[1]["error"]


def test_run_all_empty_output_becomes_error_row():
    _check_error_row(bakeoff.run_all(["a", "bad", "c"], "d.jsonl", run=_flaky("  \n")))


def test_run_all_non_json_last_line_becomes_error_row():
    _check_error_row(bakeoff.run_all(["a", "bad", "c"], "d.jsonl", run=_flaky("ok\nnot json")))


def test_sample_dev_reads_stage2_traces_and_skips_train_traces():
    """Final review I5: traces carry pair_id/split/label under `meta`; a train trace is never dev."""
    import pytest

    def trace(pid, label, split):
        return {"case": CASE, "actions": [{"action": "final", "leads": [LEAD] if label == "vulnerable" else []}],
                "results": [], "meta": {"pair_id": pid, "split": split, "label": label}}

    rows = [trace("P", "vulnerable", "dev"), trace("P", "patched", "dev"), trace("T", "vulnerable", "train")]
    out = bakeoff.sample_dev(rows, sample_n=10, seed=0)
    assert [(r["meta"]["pair_id"], r["meta"]["label"]) for r in out] == [("P", "patched"), ("P", "vulnerable")]
    with pytest.raises(ValueError, match="split"):
        bakeoff.sample_dev([{"pair_id": "P", "label": "vulnerable", "case": CASE, "lead": LEAD}], sample_n=10, seed=0)


# --- 2026-10-29 (merge plan Phase 6, Gate 1): sampling, per-row outcomes, the paired comparison ------

class Sampled(Scripted):
    def __init__(self, log, **sampling):
        super().__init__(log)
        self.sampling = {"temperature": 1.0, "top_p": 0.95, "top_k": 0, **sampling}


def test_sampling_reaches_load_and_the_row_records_what_ran():
    got = []

    def load(path, **kw):
        got.append(kw)
        return Sampled([], **kw)

    out = bakeoff.run_candidate("m", _dev(2), schema_text="S", load=load, sample_n=4, sampling={"temperature": 0.0},
                                keep_rows=True)
    assert got == [{"temperature": 0.0}] and out["temperature"] == 0.0
    assert out["sampling"] == {"temperature": 0.0, "top_p": 0.95, "top_k": 0}
    assert [(r["i"], r["label"]) for r in out["rows"]] == [(0, "patched"), (1, "vulnerable"),
                                                           (2, "patched"), (3, "vulnerable")]
    plain = bakeoff.run_candidate("m", _dev(2), schema_text="S", load=lambda p: Scripted([]), sample_n=4)
    assert "rows" not in plain and plain["sampling"] is None and plain["temperature"] is None


def test_run_all_passes_sampling_to_every_child():
    cmds = []

    def runner(cmd):
        cmds.append(cmd)
        return json.dumps({"model": cmd[cmd.index("--one") + 1]})

    bakeoff.run_all(["a", "b"], "d.jsonl", sampling={"temperature": 0.0, "top_p": None}, run=runner)
    for cmd in cmds:
        assert cmd[cmd.index("--temperature") + 1] == "0.0" and "--top-p" not in cmd


def _row(model, flags, temperature=0.0):
    """A keep_rows row from (pair, label, flagged) outcomes."""
    rows = [{"i": i, "pair_id": p, "label": lab, "flagged": f, "parse_failed": False, "overflow": False}
            for i, (p, lab, f) in enumerate(flags)]
    return {"model": model, "rows": rows, "sampling": {"temperature": temperature}}


def _pairs(n, vuln_flagged, patched_flagged):
    out = []
    for k in range(n):
        out += [(f"P{k}", "patched", k < patched_flagged), (f"P{k}", "vulnerable", k < vuln_flagged)]
    return out


def test_compare_keeps_c1_only_when_it_clearly_beats_its_base():
    base = _row("base", _pairs(40, vuln_flagged=10, patched_flagged=5))
    good = _row("c1", _pairs(40, vuln_flagged=36, patched_flagged=5))
    c = bakeoff.compare(base, good, B=500)
    assert c["decision"] == "KEEP_C1" and c["n_pairs"] == 40
    assert c["base_metrics"]["vuln_recall"] == 0.25 and c["candidate_metrics"]["vuln_recall"] == 0.9
    assert c["ci"]["pair_correct"][0] > 0
    noisy = _row("c1", _pairs(40, vuln_flagged=36, patched_flagged=30))       # flags everything: false alarms
    assert bakeoff.compare(base, noisy, B=500)["decision"] == "RETRAIN"
    same = _row("c1", _pairs(40, vuln_flagged=10, patched_flagged=5))
    assert bakeoff.compare(base, same, B=500)["decision"] == "RETRAIN"         # no gain is not good enough
    text = bakeoff.compare_text(c)
    assert "KEEP_C1" in text and "pair_correct" in text and "temperature 0.0" in text


def test_compare_refuses_different_samples_or_temperatures_and_is_seeded():
    import pytest
    base = _row("base", _pairs(5, 1, 0))
    with pytest.raises(ValueError, match="same sample"):
        bakeoff.compare(base, _row("c1", _pairs(6, 1, 0)))
    with pytest.raises(ValueError, match="temperatures"):
        bakeoff.compare(base, _row("c1", _pairs(5, 1, 0), temperature=1.0))
    with pytest.raises(ValueError, match="--out"):
        bakeoff.compare({"model": "x"}, base)
    one = _row("b", _pairs(1, 1, 0))
    assert bakeoff.compare(one, _row("c", _pairs(1, 1, 0)), B=50)["decision"] == "INCONCLUSIVE"
    a, b = bakeoff.compare(base, _row("c1", _pairs(5, 4, 0)), B=300), \
        bakeoff.compare(base, _row("c1", _pairs(5, 4, 0)), B=300)
    assert a == b


def test_cli_one_with_out_and_compare(tmp_path, monkeypatch, capsys):
    dev = tmp_path / "dev.jsonl"
    dev.write_text("\n".join(json.dumps(r) for r in _dev(6)) + "\n", encoding="utf-8")
    seen = []

    def load(path, **kw):
        seen.append((path, kw))
        return Sampled([], **kw)

    monkeypatch.setattr(bakeoff.lm_model, "load", load)
    monkeypatch.setattr(bakeoff, "_maxrss_mb", lambda: None)
    monkeypatch.setattr(bakeoff.run_candidate, "__kwdefaults__",
                        {**bakeoff.run_candidate.__kwdefaults__, "load": load})
    for name in ("base", "c1"):
        bakeoff.main(["--one", f"models/{name}", "--dev", str(dev), "--sample-n", "8", "--temperature", "0",
                      "--out", str(tmp_path / f"{name}.json")])
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "rows" not in printed and printed["temperature"] == 0.0
    assert seen == [("models/base", {"temperature": 0.0}), ("models/c1", {"temperature": 0.0})]
    assert len(json.loads((tmp_path / "c1.json").read_text(encoding="utf-8"))["rows"]) == 8
    bakeoff.main(["--compare", str(tmp_path / "base.json"), str(tmp_path / "c1.json")])
    out = capsys.readouterr().out
    assert "Gate 1: models/c1 vs base models/base" in out and "RETRAIN" in out    # identical scripts: no gain
