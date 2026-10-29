"""orion/ui/runs.py: the UI's read-only view of the run dirs `orion scan` writes. Token-free: every run
dir is synthetic, in tmp_path, written with the CLI's OWN writers (cli._save_leads, cli._append_verdict,
monitor.run_logger), so what the UI reads is pinned to what the CLI writes -- CRLF line ends on Windows
included. Links that the OS refuses to make (Windows without the symlink privilege) skip; directory links
fall back to a junction there, so the run-dir and repo escape checks run on Windows too."""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
from pathlib import Path

import pytest

from orion import cli, monitor
from orion.contracts import Lead, Verdict
from orion.ui import runs
from orion.ui.runs import Runs
from tests.conftest import symlink_or_skip

TS1, TS2, TS3 = "20261027T090000Z", "20261028T090000Z", "20261029T090000Z"


def _lead(i: int, text: str | None = None, **kw) -> Lead:
    return Lead(index=i, shape="A", text=text or f"lead {i}", evidence=f"evidence {i}", confidence="HIGH", **kw)


def _run(root: Path, scan_id: str = "s1", ts: str = TS1, *, leads=None, repo=None, engine="claude",
         model=None, logged=(), final=None, report=None, events=()) -> Path:
    """A run dir as the CLI leaves it at whatever point the arguments describe."""
    d = root / scan_id / ts
    d.mkdir(parents=True)
    if events:
        log = monitor.run_logger(str(d), quiet=True)
        for evt in events:
            log(evt)
    if leads is not None:
        cli._save_leads(str(d), scan_id, repo, leads, engine=engine, model=model)
    for v in logged:
        cli._append_verdict(str(d), v)
    if final is not None:     # the end-of-run write in cli._run_scan
        payload = json.dumps([dataclasses.asdict(v) for v in final], indent=2, default=str)
        (d / "verdicts.json").write_text(payload, encoding="utf-8")
    if report is not None:
        (d / "report.txt").write_text(report, encoding="utf-8")
    return d


def _dir_link_or_skip(link: Path, target: Path) -> None:
    """A directory link: a symlink where the OS allows one, else on Windows a junction (no privilege
    needed), else skip."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        reason: Exception = exc
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, AttributeError, OSError) as exc:
            reason = exc
    pytest.skip(f"cannot create a directory link here: {reason}")


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / ".orion" / "runs"


@pytest.fixture
def view(tmp_path: Path, root: Path) -> Runs:
    return Runs(root, base_dir=tmp_path)


# ── run ids ─────────────────────────────────────────────────────────────────────────────────────
def test_run_ids_round_trip():
    assert runs.run_id("abc123", TS1) == f"abc123~{TS1}"
    assert runs.parse_run_id(f"abc123~{TS1}") == ("abc123", TS1)
    assert runs.parse_run_id("a.b_c-D~x") == ("a.b_c-D", "x")
    assert runs.parse_run_id("x" * 201 + "~t") == ("x" * 201, "t")       # PART's longest part


@pytest.mark.parametrize("rid", [
    "", "~", "a", "a~", "~b", "a~b~c", "..~t", "s~..", ".~t", "a/b~t", "s~a/b", "a\\b~t", "s~a\\b",
    "/abs~t", "C:\\x~t", "C:~t", "s~t\n", " s~t", "s t~x", "-x~t", "_x~t", "x" * 202 + "~t", 5, None,
    ["s", "t"],
])
def test_malformed_and_escaping_run_ids_are_refused(view, rid):
    with pytest.raises(ValueError):
        runs.parse_run_id(rid)
    with pytest.raises(ValueError):
        view.run_dir(rid)


@pytest.mark.parametrize("scan_id, ts", [("a/b", TS1), ("s", "../x"), ("s", "t\n"), ("s", ""), ("~", TS1),
                                         ("s", 5)])
def test_run_id_refuses_bad_parts(scan_id, ts):
    with pytest.raises(ValueError):
        runs.run_id(scan_id, ts)


def test_an_absent_run_is_a_key_error_with_a_plain_message(view, root):
    with pytest.raises(KeyError) as err:
        view.run_dir(f"nope~{TS1}")                       # no root at all yet
    assert str(err.value) == f"no run nope~{TS1}"          # not KeyError's quoted repr
    (root / "nope").mkdir(parents=True)
    (root / "nope" / TS1).write_text("a file, not a run dir")
    with pytest.raises(KeyError):
        view.run_dir(f"nope~{TS1}")


def test_a_run_dir_linked_out_of_the_root_is_refused_and_not_listed(view, root, tmp_path):
    _run(root, "s1", TS1)
    elsewhere = tmp_path / "elsewhere" / "real"
    (elsewhere / TS2).mkdir(parents=True)
    _dir_link_or_skip(root / "s9", elsewhere)
    with pytest.raises(ValueError):
        view.run_dir(f"s9~{TS2}")
    assert [r["id"] for r in view.list()] == [f"s1~{TS1}"]


def test_an_alias_of_a_run_dir_is_not_that_run(view, root):
    # Windows strips a trailing dot, so "s1." opens s1's dir: refused as not being root/s1./ts itself.
    _run(root, "s1", TS1)
    with pytest.raises(ValueError if os.name == "nt" else KeyError):
        view.run_dir(f"s1.~{TS1}")
    assert [r["id"] for r in view.list()] == [f"s1~{TS1}"]


def test_rid_for_is_the_inverse_of_run_dir(view, root, tmp_path):
    d = _run(root, "s1", TS1)
    rid = f"s1~{TS1}"
    assert view.rid_for(view.run_dir(rid)) == rid
    assert view.rid_for(d) == rid
    assert view.rid_for(Path(".orion") / "runs" / "s1" / TS1) == rid       # relative = to base_dir
    assert view.rid_for(root / "s2" / TS2) == f"s2~{TS2}"                   # need not exist yet
    for other in (root, root / "s1", root / "s1" / TS1 / "deeper", root / "s1" / "bad name",
                  tmp_path / "x" / "s1" / TS1):
        assert view.rid_for(other) is None


# ── states, counts, the list ────────────────────────────────────────────────────────────────────
def test_state_comes_from_the_files_on_disk(view, root):
    _run(root, "s1", TS1)                                                    # crashed before discovery
    _run(root, "s2", TS1, leads=[_lead(0), _lead(1)], repo="r",
         logged=[Verdict(_lead(0), "CONFIRM", "r0")])                        # stopped mid-verify
    _run(root, "s3", TS1, leads=[_lead(0)], repo="r", engine="lm", model="models/orion-lm",
         logged=[Verdict(_lead(0), "INCONCLUSIVE", "unverified (engine=lm)")])
    done = [Verdict(_lead(0), "CONFIRM", "r0"), Verdict(_lead(1), "REJECT", "r1")]
    _run(root, "s4", TS1, leads=[_lead(0), _lead(1)], repo="r", logged=done, final=done, report="text")
    rows = {r["scan_id"]: r for r in view.list()}

    assert rows["s1"]["state"] == "incomplete" and rows["s1"]["n_leads"] is None
    assert rows["s1"]["problem"] is None and not rows["s1"]["resumable"]
    assert rows["s2"]["state"] == "stopped" and rows["s2"]["resumable"]
    assert rows["s2"]["n_leads"] == 2 and rows["s2"]["n_verdicts"] == 1
    assert rows["s2"]["engine"] == "claude" and rows["s2"]["model"] is None and rows["s2"]["repo"] == "r"
    assert rows["s3"]["state"] == "stopped" and not rows["s3"]["resumable"]     # the CLI refuses lm resumes
    assert (rows["s3"]["engine"], rows["s3"]["model"]) == ("lm", "models/orion-lm")
    assert rows["s4"]["state"] == "done" and not rows["s4"]["resumable"]
    assert rows["s4"]["counts"] == {"CONFIRM": 1, "INCONCLUSIVE": 0, "REJECT": 1, "ERROR": 0}
    assert all(r["problem"] is None for r in rows.values())


def test_an_old_run_without_an_engine_is_claude(view, root):
    d = _run(root, "s1", TS1)
    (d / "leads.json").write_text(json.dumps({"scan_id": "s1", "repo": "r", "leads": []}), encoding="utf-8")
    row = view.summary(f"s1~{TS1}")
    assert row["engine"] == "claude" and row["state"] == "stopped" and row["resumable"] and row["n_leads"] == 0


def test_the_job_runners_state_wins_and_is_never_resumable(view, root, tmp_path):
    d = _run(root, "s2", TS1, leads=[_lead(0)], repo="r")
    rid = f"s2~{TS1}"
    [row] = view.list(active={str(d.resolve()): "running"})
    assert row["state"] == "running" and not row["resumable"]
    assert view.summary(rid, active_state="stopping")["state"] == "stopping"
    assert view.detail(rid, active_state="running")["state"] == "running"
    # the same dir named another way still matches (relative to base_dir); a state the runner never
    # reports for a live job is not taken as one
    [row] = view.list(active={os.path.join(".orion", "runs", "s2", TS1): "stopping"})
    assert row["state"] == "stopping"
    assert view.list(active={str(d.resolve()): "exited"})[0]["state"] == "stopped"
    assert view.summary(rid, active_state="done")["state"] == "stopped"


def test_counts_prefer_verdicts_json_then_the_latest_log_line_per_lead(view, root):
    leads = [_lead(i) for i in range(3)]
    d = _run(root, "s1", TS1, leads=leads, repo="r",
             logged=[Verdict(leads[0], "ERROR", "boom"), Verdict(leads[1], "REJECT", "no"),
                     Verdict(leads[0], "CONFIRM", "retried on resume")])
    rid = f"s1~{TS1}"
    row = view.summary(rid)
    assert row["counts"] == {"CONFIRM": 1, "INCONCLUSIVE": 0, "REJECT": 1, "ERROR": 0}
    assert row["n_verdicts"] == 2 and row["state"] == "stopped"
    assert [(v["lead"]["index"], v["decision"]) for v in view.verdicts(rid)] == [(0, "CONFIRM"), (1, "REJECT")]

    final = [Verdict(leads[i], "INCONCLUSIVE", "x") for i in range(2)] + [Verdict(leads[2], "ERROR", "y")]
    (d / "verdicts.json").write_text(json.dumps([dataclasses.asdict(v) for v in final]), encoding="utf-8")
    row = view.summary(rid)                         # the cached summary must notice the new file
    assert row["state"] == "done"
    assert row["counts"] == {"CONFIRM": 0, "INCONCLUSIVE": 2, "REJECT": 0, "ERROR": 1}
    assert [v["decision"] for v in view.verdicts(rid)] == ["INCONCLUSIVE", "INCONCLUSIVE", "ERROR"]


def test_a_torn_last_line_is_skipped_and_a_growing_log_is_reread(view, root):
    leads = [_lead(i) for i in range(3)]
    d = _run(root, "s1", TS1, leads=leads, repo="r", logged=[Verdict(leads[0], "CONFIRM", "r")])
    rid = f"s1~{TS1}"
    assert view.summary(rid)["n_verdicts"] == 1
    with open(d / "verdicts.jsonl", "a", encoding="utf-8") as f:
        f.write('{"lead": {"index": 2')                     # a crash mid-write
    row = view.summary(rid)
    assert row["n_verdicts"] == 1 and row["state"] == "stopped" and row["problem"] is None
    assert [v["lead"]["index"] for v in view.verdicts(rid)] == [0]
    with open(d / "verdicts.jsonl", "a", encoding="utf-8") as f:   # the resumed scan appends on
        f.write("\n" + json.dumps(dataclasses.asdict(Verdict(leads[1], "REJECT", "r"))) + "\n")
    assert view.summary(rid)["counts"]["REJECT"] == 1


def test_an_unknown_or_missing_decision_counts_as_error(view, root):
    d = _run(root, "s1", TS1, leads=[_lead(0), _lead(1)], repo="r")
    rows = [{"lead": {"index": 0}, "decision": "MAYBE"}, {"lead": {"index": 1}}, "not a verdict", 7]
    (d / "verdicts.json").write_text(json.dumps(rows), encoding="utf-8")
    row = view.summary(f"s1~{TS1}")
    assert row["counts"]["ERROR"] == 2 and row["n_verdicts"] == 2 and row["state"] == "done"


@pytest.mark.parametrize("name, text, problem", [
    ("leads.json", "{not json", "leads.json is not valid JSON"),
    ("leads.json", '{"leads": 5}', "leads.json holds no list of leads"),
    ("leads.json", "[1, 2]", "leads.json holds no list of leads"),
    ("leads.json", "[" * 100_000, "leads.json is not valid JSON"),         # deep nesting: RecursionError
    ("verdicts.json", '[{"lead": {"index": 0}, "decision": "CONF', "verdicts.json is not valid JSON"),
    ("verdicts.json", '{"a": 1}', "verdicts.json is not a list of verdicts"),
], ids=["leads-junk", "leads-not-a-list", "leads-a-list", "leads-deep", "verdicts-torn", "verdicts-a-dict"])
def test_an_unreadable_file_makes_the_run_incomplete_never_an_exception(view, root, name, text, problem):
    leads = [_lead(0), _lead(1)]
    d = _run(root, "s1", TS1, leads=leads, repo="r", logged=[Verdict(leads[0], "CONFIRM", "r")])
    _run(root, "s2", TS2, leads=leads, repo="r")
    (d / name).write_bytes(text.encode("utf-8"))
    rows = {r["scan_id"]: r for r in view.list()}
    assert rows["s1"]["state"] == "incomplete" and problem in rows["s1"]["problem"]
    assert not rows["s1"]["resumable"]
    assert rows["s2"]["state"] == "stopped" and rows["s2"]["problem"] is None
    if name == "verdicts.json":                      # counts fall back to the log
        assert rows["s1"]["counts"]["CONFIRM"] == 1 and rows["s1"]["n_leads"] == 2
    detail = view.detail(f"s1~{TS1}")                # the detail view still opens
    assert detail["state"] == "incomplete" and isinstance(detail["verdicts"], list)


def test_list_survives_a_run_dir_that_cannot_be_summarised(view, root, monkeypatch):
    _run(root, "s1", TS1, leads=[_lead(0)], repo="r")
    _run(root, "s2", TS2, leads=[_lead(0)], repo="r")
    real = runs._read_snapshot

    def flaky(d):
        if d.parent.name == "s1":
            raise RuntimeError("disk on fire")
        return real(d)

    monkeypatch.setattr(runs, "_read_snapshot", flaky)
    rows = {r["scan_id"]: r for r in view.list()}
    assert rows["s1"]["state"] == "incomplete" and "disk on fire" in rows["s1"]["problem"]
    assert set(rows["s1"]) == set(rows["s2"])                       # the same shape as a good row
    assert rows["s2"]["state"] == "stopped"


def test_list_is_newest_first_and_skips_what_is_not_a_run_dir(view, root):
    _run(root, "s1", TS1)
    _run(root, "s2", TS3)
    _run(root, "s1", TS2)
    _run(root, "s3", "manual")                       # not a CLI timestamp: after all that are
    (root / "notes.txt").write_text("x")
    (root / ".hidden" / TS3).mkdir(parents=True)
    (root / "s1" / "bad name").mkdir()
    (root / "s1" / "a-file").write_text("x")
    assert [r["id"] for r in view.list()] == [f"s2~{TS3}", f"s1~{TS2}", f"s1~{TS1}", "s3~manual"]


def test_list_is_empty_before_the_first_scan(tmp_path):
    assert Runs(tmp_path / "nothing" / "here", base_dir=tmp_path).list() == []
    (tmp_path / "afile").write_text("x")
    assert Runs(tmp_path / "afile", base_dir=tmp_path).list() == []


def test_a_relative_root_is_taken_from_base_dir(tmp_path, root):
    _run(root, "s1", TS1)
    assert [r["id"] for r in Runs(Path(".orion/runs"), base_dir=tmp_path).list()] == [f"s1~{TS1}"]


def test_summary_fields(view, root):
    d = _run(root, "s1", TS1, leads=[_lead(0)], repo="fixtures/NodeGoat",
             events=[cli._event("build", "start", detail="building")])
    os.utime(d / "leads.json", (1_800_000_000, 1_800_000_000))
    os.utime(d / "progress.jsonl", (1_800_000_100, 1_800_000_100))
    row = view.summary(f"s1~{TS1}")
    assert set(row) == {"id", "scan_id", "ts", "started", "repo", "engine", "model", "settings", "state",
                        "elsewhere", "resumable", "counts", "n_leads", "n_verdicts", "last_activity", "problem"}
    assert row["settings"] is None and row["elsewhere"] is False         # recorded no settings; no lock held
    assert (row["id"], row["scan_id"], row["ts"]) == (f"s1~{TS1}", "s1", TS1)
    assert row["started"] == "2026-10-27T09:00:00+00:00"
    assert row["last_activity"] == _iso(1_800_000_100)
    assert row["repo"] == "fixtures/NodeGoat"                          # as recorded, not resolved
    assert row["counts"] == dict.fromkeys(runs.DECISIONS, 0) and row["n_verdicts"] == 0
    json.dumps(row)                                                     # plain JSON for the API
    _run(root, "s2", "manual")
    assert view.summary("s2~manual")["started"] is None


def test_detail_adds_leads_verdicts_files_and_the_report(view, root, monkeypatch):
    leads = [_lead(0), _lead(1)]
    final = [Verdict(leads[0], "CONFIRM", "r0"), Verdict(leads[1], "REJECT", "r1")]
    d = _run(root, "s1", TS1, leads=leads, repo="r", logged=final, final=final, report="line 1\nline 2\n",
             events=[cli._event("report", "done", detail="report rendered")])
    (d / "results.sarif").write_text("{}", encoding="utf-8")
    (d / "extra.txt").write_text("not offered")
    before = sorted(p.name for p in d.iterdir())
    out = view.detail(f"s1~{TS1}")
    assert out["state"] == "done" and out["id"] == f"s1~{TS1}"
    assert [x["index"] for x in out["leads"]] == [0, 1]
    assert [v["decision"] for v in out["verdicts"]] == ["CONFIRM", "REJECT"]
    assert out["files"] == list(runs.DOWNLOADS)                         # all five, in DOWNLOADS order
    assert out["report_text"] == "line 1\nline 2\n"                     # CRLF folded on Windows
    assert sorted(p.name for p in d.iterdir()) == before                # reading never writes

    monkeypatch.setattr(runs, "REPORT_MAX_BYTES", 4)
    assert view.detail(f"s1~{TS1}")["report_text"].startswith("line\n[report.txt is longer than 4 bytes")
    _run(root, "s2", TS2)
    bare = view.detail(f"s2~{TS2}")
    assert bare["leads"] == [] and bare["verdicts"] == [] and bare["files"] == [] and bare["report_text"] is None


# ── events ──────────────────────────────────────────────────────────────────────────────────────
def test_events_hand_out_complete_lines_from_a_byte_offset(view, root):
    d = _run(root, "s1", TS1, events=[cli._event("build", "start", detail="building"),
                                      cli._event("build", "done", detail="graph build complete")])
    rid, log = f"s1~{TS1}", d / "progress.jsonl"
    events, offset = view.events(rid)
    assert [(e["phase"], e["event"]) for e in events] == [("build", "start"), ("build", "done")]
    assert offset == log.stat().st_size
    assert view.events(rid, offset) == ([], offset)                     # at EOF
    assert view.events(rid, offset + 100) == ([], offset + 100)         # past EOF

    with open(log, "ab") as f:                                          # a line still being written
        f.write(b'{"phase": "discover", "event": "st')
    assert view.events(rid, offset) == ([], offset)
    with open(log, "ab") as f:
        f.write(b'art", "detail": "fleet"}\n')
    events, offset2 = view.events(rid, offset)
    assert [e["detail"] for e in events] == ["fleet"] and offset2 == log.stat().st_size

    with open(log, "ab") as f:                       # junk is skipped but consumed; CRLF is fine
        f.write(b'not json\n[1, 2]\n\n{"event": "usage", "detail": "{}"}\r\n')
    events, offset3 = view.events(rid, offset2)
    assert events == [{"event": "usage", "detail": "{}"}] and offset3 == log.stat().st_size


def test_events_respect_the_window_and_skip_a_line_longer_than_it(view, root):
    d = _run(root, "s1", TS1, events=[cli._event("build", "start", detail="a"),
                                      cli._event("build", "done", detail="b")])
    rid, log = f"s1~{TS1}", d / "progress.jsonl"
    first = log.read_bytes().index(b"\n") + 1
    events, offset = view.events(rid, 0, limit_bytes=first + 3)       # room for one line and a bit
    assert [e["detail"] for e in events] == ["a"] and offset == first
    events, offset = view.events(rid, offset, limit_bytes=first + 3)
    assert [e["detail"] for e in events] == ["b"] and offset == log.stat().st_size

    with open(log, "ab") as f:
        f.write(b'{"detail": "' + b"x" * 300 + b'"}\n{"detail": "after"}\n')
    skipped, offset2 = view.events(rid, offset, limit_bytes=100)      # the long line: skipped, not stalled
    assert skipped == [] and offset2 == offset + 300 + 15
    events, offset3 = view.events(rid, offset2, limit_bytes=100)
    assert [e["detail"] for e in events] == ["after"] and offset3 == log.stat().st_size

    with open(log, "ab") as f:                       # a long line not finished yet: wait for it
        f.write(b'{"detail": "' + b"y" * 300)
    assert view.events(rid, offset3, limit_bytes=100) == ([], offset3)


def test_events_without_a_log_and_bad_arguments(view, root):
    _run(root, "s1", TS1)
    rid = f"s1~{TS1}"
    assert view.events(rid) == ([], 0)
    assert view.events(rid, 50) == ([], 0)
    assert view.events(rid, "0") == ([], 0)                             # a query parameter as text
    for bad in (-1, True, "x", 1.5, None):
        with pytest.raises(ValueError):
            view.events(rid, bad)
    with pytest.raises(ValueError):
        view.events(rid, 0, limit_bytes=0)
    with pytest.raises(KeyError):
        view.events(f"s9~{TS1}")


# ── downloads ───────────────────────────────────────────────────────────────────────────────────
def test_file_path_serves_only_the_allowlisted_files_present(view, root, tmp_path):
    d = _run(root, "s1", TS1, leads=[_lead(0)], repo="r", report="the report")
    (d / "other.txt").write_text("x")
    rid = f"s1~{TS1}"
    assert view.file_path(rid, "report.txt") == (d / "report.txt").resolve()
    assert view.file_path(rid, "leads.json").read_bytes() == (d / "leads.json").read_bytes()
    for name in ("other.txt", "results.sarif", "progress.jsonl", "../leads.json", "./report.txt",
                 "REPORT.TXT", "report.txt\n", "", None, 3):
        with pytest.raises(KeyError):
            view.file_path(rid, name)
    with pytest.raises(KeyError):
        view.file_path(f"s9~{TS1}", "report.txt")
    (tmp_path / "secret.txt").write_text("secret")
    symlink_or_skip(d / "results.sarif", tmp_path / "secret.txt")   # a link planted in the run dir
    with pytest.raises(KeyError):
        view.file_path(rid, "results.sarif")
    assert "results.sarif" not in view.detail(rid)["files"]


# ── the code view ───────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixtures" / "NodeGoat"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "x.js").write_text("".join(f"line {i}\n" for i in range(1, 31)), encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret\n")
    return repo


@pytest.fixture
def rid(root: Path, repo: Path) -> str:
    _run(root, "s1", TS1, leads=[_lead(0, file="app/x.js", line_start=10)], repo="fixtures/NodeGoat")
    return f"s1~{TS1}"


def test_code_shows_the_cited_lines_with_context(view, rid):
    out = view.code(rid, "app/x.js", 10)
    assert (out["file"], out["start"], out["end"], out["highlight"]) == ("app/x.js", 2, 18, [10, 10])
    assert out["lines"][0] == {"n": 2, "text": "line 2"} and out["lines"][-1] == {"n": 18, "text": "line 18"}
    assert [x["n"] for x in out["lines"]] == list(range(2, 19))
    out = view.code(rid, "app/x.js", 12, end=14, context=0)
    assert [x["text"] for x in out["lines"]] == ["line 12", "line 13", "line 14"]
    assert out["highlight"] == [12, 14]
    out = view.code(rid, "app/x.js", "12", end="14", context="1")       # query parameters as text
    assert (out["start"], out["end"]) == (11, 15)
    assert view.code(rid, "app/x.js", 10, end="")["highlight"] == [10, 10]


def test_code_clips_the_window_to_the_file(view, rid):
    out = view.code(rid, "app/x.js", 2, context=8)
    assert (out["start"], out["end"]) == (1, 10)
    out = view.code(rid, "app/x.js", 29, context=8)
    assert (out["start"], out["end"]) == (21, 30)
    out = view.code(rid, "app/x.js", 25, end=99)                       # an end past EOF is clipped
    assert out["highlight"] == [25, 30] and out["end"] == 30
    out = view.code(rid, "app/x.js", 25, end=12, context=1)            # an end before the start too
    assert out["highlight"] == [25, 25] and (out["start"], out["end"]) == (24, 26)


@pytest.mark.parametrize("cited", [
    "app/x.js", "./app/x.js", "././app/x.js", "fixtures/NodeGoat/app/x.js", "some/where/else/app/x.js",
    "a/b/c/d/e/f/g/app/x.js",                        # the 8th try finds it
])
def test_code_finds_a_path_cited_from_another_directory(view, rid, cited):
    assert view.code(rid, cited, 1)["file"] == "app/x.js"


def test_code_tries_at_most_eight_prefixes(view, rid):
    with pytest.raises(KeyError):
        view.code(rid, "a/b/c/d/e/f/g/h/app/x.js", 1)                  # would need a 9th try


@pytest.mark.parametrize("cited", ["../secret.txt", "app/../../../secret.txt", "../NodeGoat/app/x.js"])
def test_code_refuses_dot_dot(view, rid, cited):
    with pytest.raises(PermissionError):
        view.code(rid, cited, 1)


def test_code_takes_an_absolute_path_only_inside_the_repo(view, rid, repo, tmp_path):
    assert view.code(rid, str(repo / "app" / "x.js"), 3)["file"] == "app/x.js"
    for outside in (str(tmp_path / "secret.txt"), "/etc/passwd", str(tmp_path)):
        with pytest.raises(PermissionError):
            view.code(rid, outside, 1)


@pytest.mark.skipif(os.name != "nt", reason="drive-qualified path parts exist only on Windows")
def test_code_refuses_a_drive_like_part_that_would_switch_drives(view, rid, tmp_path):
    # A part naming tmp_path's OWN drive joins harmlessly inside the root; another drive would leave it.
    other = "Z:" if tmp_path.drive.upper() != "Z:" else "Y:"
    for cited in (f"app/{other}evil.js", f"x/{other}/Windows/win.ini", f"{other}x.js", f"{other}/x.js"):
        with pytest.raises(PermissionError):
            view.code(rid, cited, 1)


def test_code_refuses_a_symlink_out_of_the_repo(view, rid, repo, tmp_path):
    symlink_or_skip(repo / "app" / "leak.js", tmp_path / "secret.txt")
    with pytest.raises(PermissionError):
        view.code(rid, "app/leak.js", 1)


def test_code_refuses_a_linked_directory_out_of_the_repo(view, rid, repo, tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "lib.js").write_text("stolen\n")
    _dir_link_or_skip(repo / "vendor", tmp_path / "outside")
    with pytest.raises(PermissionError):
        view.code(rid, "vendor/lib.js", 1)


def test_looping_links_degrade_to_absent_never_an_exception(view, rid, root, repo, tmp_path):
    # Python 3.12's Path.resolve() raises RuntimeError (not OSError) on a symlink loop, even non-strict.
    symlink_or_skip(repo / "app" / "loop.js", repo / "app" / "loop.js")
    with pytest.raises(KeyError):
        view.code(rid, "app/loop.js", 1)
    symlink_or_skip(root / "s8", root / "s8")
    with pytest.raises(KeyError):
        view.run_dir(f"s8~{TS1}")
    loop = tmp_path / "loop"
    symlink_or_skip(loop, loop)
    _run(root, "s7", TS2, leads=[], repo=str(loop))
    with pytest.raises(KeyError):
        view.code(f"s7~{TS2}", "x.js", 1)
    assert view.compare(rid, f"s7~{TS2}")["same_repo"] is False
    assert view.rid_for(loop / "a" / "b") is None
    assert [r["id"] for r in view.list(active={str(loop): "running"})] == [f"s7~{TS2}", rid]
    assert Runs(loop, base_dir=tmp_path).list() == []


def test_code_follows_a_symlink_that_stays_inside_the_repo(view, rid, repo):
    symlink_or_skip(repo / "alias.js", repo / "app" / "x.js")
    assert view.code(rid, "alias.js", 1)["file"] == "app/x.js"           # the real file is named


def test_code_size_cap_and_binary_files(view, rid, repo, monkeypatch):
    (repo / "big.js").write_bytes(b"x\n" * 30)                           # 60 bytes
    monkeypatch.setattr(runs, "CODE_MAX_BYTES", 60)
    assert view.code(rid, "big.js", 1)["file"] == "big.js"               # exactly at the cap
    monkeypatch.setattr(runs, "CODE_MAX_BYTES", 59)
    with pytest.raises(ValueError, match="larger than"):
        view.code(rid, "big.js", 1)
    monkeypatch.undo()
    (repo / "blob.bin").write_bytes(b"MZ\x90\x00\x03" + b"\n" * 10)
    with pytest.raises(ValueError, match="binary"):
        view.code(rid, "blob.bin", 1)
    (repo / "late.js").write_bytes(b"a\n" * 5000 + b"\x00\n")            # a NUL past the first 8 KB
    assert view.code(rid, "late.js", 1, context=0)["lines"] == [{"n": 1, "text": "a"}]


@pytest.mark.parametrize("line", [0, 31, -1, True, "abc", None, 2.0])
def test_code_refuses_a_line_outside_the_file(view, rid, line):
    with pytest.raises(ValueError):
        view.code(rid, "app/x.js", line)


@pytest.mark.parametrize("cited", ["app/x.js\x00", "", "   ", ".", "./", None, 5])
def test_code_refuses_a_bad_file_argument(view, rid, cited):
    with pytest.raises(ValueError):
        view.code(rid, cited, 1)


def test_code_bad_end_or_context(view, rid):
    with pytest.raises(ValueError):
        view.code(rid, "app/x.js", 1, end="soon")
    with pytest.raises(ValueError):
        view.code(rid, "app/x.js", 1, context=-1)


def test_code_missing_file_or_repo_is_a_key_error(view, rid, root, repo, tmp_path):
    for cited in ("app/nope.js", "app"):                                  # absent, and a directory
        with pytest.raises(KeyError):
            view.code(rid, cited, 1)
    _run(root, "s2", TS1)                                                 # no leads.json
    with pytest.raises(KeyError):
        view.code(f"s2~{TS1}", "app/x.js", 1)
    _run(root, "s3", TS1, leads=[], repo=None)                            # a --scan-id run: no repo
    with pytest.raises(KeyError):
        view.code(f"s3~{TS1}", "app/x.js", 1)
    _run(root, "s4", TS1, leads=[], repo=str(tmp_path / "gone"))         # the checkout was deleted
    with pytest.raises(KeyError):
        view.code(f"s4~{TS1}", "app/x.js", 1)


def test_code_line_ends_and_bad_utf8(view, rid, root, repo):
    (repo / "crlf.py").write_bytes(b"first\r\nsecond \xff\r\n\r\nlast")
    out = view.code(rid, "crlf.py", 2, context=5)
    assert out["lines"] == [{"n": 1, "text": "first"}, {"n": 2, "text": "second \ufffd"},
                            {"n": 3, "text": ""}, {"n": 4, "text": "last"}]
    (repo / "sep.js").write_text("a = '\u2028'\nb\x0c\nc\n", encoding="utf-8")   # not line breaks here
    assert [x["text"] for x in view.code(rid, "sep.js", 3, context=5)["lines"]] == ["a = '\u2028'", "b\x0c", "c"]
    (repo / "empty.js").write_bytes(b"")
    with pytest.raises(ValueError):
        view.code(rid, "empty.js", 1)
    _run(root, "s2", TS2, leads=[], repo=str(repo))                       # an absolute repo path
    assert view.code(f"s2~{TS2}", "app/x.js", 1)["file"] == "app/x.js"


# ── compare ─────────────────────────────────────────────────────────────────────────────────────
def test_location_and_fingerprint_mirror_the_contracts():
    base = dict(file="app/a.js", cwe="CWE-89", function="getUser", line_start=10, line_end=12)
    cases = [
        Verdict(_lead(0, **base), "CONFIRM", "r"),                                       # verifier silent
        Verdict(_lead(0, **base), "CONFIRM", "r", line_start=11, cwe="CWE-564"),         # same file
        Verdict(_lead(0, **base), "CONFIRM", "r", file="app/b.js"),                      # moved: no lines
        Verdict(_lead(0, **base), "CONFIRM", "r", file="app/b.js", line_start=4),
        Verdict(_lead(0, **base), "CONFIRM", "r", file="app/a.js", line_start=20),       # "moved" to itself
        Verdict(_lead(0), "REJECT", "r"),                                                # no location
        Verdict(_lead(0, file="app/a.js", cwe="CWE-79"), "REJECT", "r"),                 # no function/line
    ]
    for v in cases:
        loc = runs._location(dataclasses.asdict(v))
        assert loc == v.location()
        probe = Lead(index=0, shape="A", text="", evidence="", confidence="LOW", file=loc["file"],
                     cwe=loc["cwe"], function=loc["function"], line_start=loc["line_start"])
        assert runs._fingerprint(loc) == probe.fingerprint()


def test_compare_appeared_vanished_changed_unchanged(view, root, tmp_path):
    sqli = dict(file="app/dao.js", cwe="CWE-89", function="getUser", line_start=10)
    a_verdicts = [
        Verdict(_lead(0, "SQL injection in getUser", **sqli), "CONFIRM", "r"),
        Verdict(_lead(1, "Missing   Helmet\theaders"), "REJECT", "r"),                         # no location
        Verdict(_lead(2, "XSS in x", file="app/x.js", cwe="CWE-79", line_start=5), "INCONCLUSIVE", "r"),
        Verdict(_lead(3, "path traversal", file="app/a.js", cwe="CWE-22", function="read", line_start=7),
                "CONFIRM", "r", file="app/b.js", line_start=9),                                 # corrected
    ]
    b_verdicts = [
        Verdict(_lead(0, "getUser builds SQL from req.body", **sqli), "INCONCLUSIVE", "r"),    # changed
        Verdict(_lead(1, "missing helmet headers"), "REJECT", "r"),                            # same text
        Verdict(_lead(2, "traversal in b", file="app/b.js", cwe="CWE-22", line_start=9), "CONFIRM", "r"),
        Verdict(_lead(3, "cmd injection", file="app/new.js", cwe="CWE-78", function="run"), "REJECT", "r"),
        Verdict(_lead(4, "cmd injection again", file="app/new.js", cwe="CWE-78", function="run"),
                "CONFIRM", "r", severity="HIGH"),                                              # duplicate key
    ]
    _run(root, "sa", TS1, leads=[v.lead for v in a_verdicts], repo="fixtures/NodeGoat", final=a_verdicts)
    _run(root, "sb", TS2, leads=[v.lead for v in b_verdicts], repo=str(tmp_path / "fixtures" / "NodeGoat"),
         logged=b_verdicts)                                                 # b is still mid-verify
    _run(root, "sc", TS3, leads=[], repo="fixtures/PyGoat", final=[])

    out = view.compare(f"sa~{TS1}", f"sb~{TS2}")
    assert out["a"]["id"] == f"sa~{TS1}" and out["b"]["id"] == f"sb~{TS2}" and out["b"]["state"] == "stopped"
    assert out["same_repo"] is True                         # one relative, one absolute: the same dir
    assert out["unchanged"] == 2                             # the helmet text + the corrected location
    [appeared] = out["appeared"]
    assert appeared == {"key": "app/new.js | CWE-78 | run", "file": "app/new.js", "line": None, "line_end": None,
                        "function": "run", "cwe": "CWE-78", "decision": "CONFIRM", "severity": "HIGH",
                        "text": "cmd injection again"}                     # the most severe duplicate
    [vanished] = out["vanished"]
    assert vanished["key"] == "app/x.js | CWE-79 | 5"
    assert (vanished["decision"], vanished["line"]) == ("INCONCLUSIVE", 5)
    [changed] = out["changed"]
    assert changed["key"] == "app/dao.js | CWE-89 | getUser" and changed["match"] == "fingerprint"
    assert out["relocated"] == []                            # nothing left for the second pass to pair
    assert (changed["a"]["decision"], changed["b"]["decision"]) == ("CONFIRM", "INCONCLUSIVE")
    assert changed["b"]["text"] == "getUser builds SQL from req.body"
    json.dumps(out)

    back = view.compare(f"sb~{TS2}", f"sa~{TS1}")
    assert [r["key"] for r in back["vanished"]] == ["app/new.js | CWE-78 | run"]
    assert [r["key"] for r in back["appeared"]] == ["app/x.js | CWE-79 | 5"]
    assert view.compare(f"sa~{TS1}", f"sc~{TS3}")["same_repo"] is False
    same = view.compare(f"sa~{TS1}", f"sa~{TS1}")
    assert same["unchanged"] == 4 and not (same["appeared"] or same["vanished"] or same["changed"])
    with pytest.raises(KeyError):
        view.compare(f"sa~{TS1}", f"zz~{TS1}")
    with pytest.raises(ValueError):
        view.compare(f"sa~{TS1}", "../x")


def test_compare_text_fallback_never_meets_a_fingerprint(view, root):
    # A lead whose TEXT reads like a fingerprint label still matches only other texts.
    a = [Verdict(_lead(0, "app/a.js | CWE-89 | f"), "CONFIRM", "r")]
    b = [Verdict(_lead(0, "elsewhere", file="app/a.js", cwe="CWE-89", function="f"), "CONFIRM", "r")]
    _run(root, "sa", TS1, leads=[v.lead for v in a], repo="r", final=a)
    _run(root, "sb", TS2, leads=[v.lead for v in b], repo="r", final=b)
    out = view.compare(f"sa~{TS1}", f"sb~{TS2}")
    assert out["unchanged"] == 0 and len(out["appeared"]) == 1 and len(out["vanished"]) == 1
    assert out["vanished"][0]["key"] == "text | app/a.js | cwe-89 | f"
    assert out["same_repo"] is True                                   # "r" and "r": one repo, two scans


def test_settings_keep_only_known_keys_of_the_right_type(view, root):
    d = _run(root, "s1", TS1, leads=[_lead(0)])
    meta = json.loads((d / "leads.json").read_text(encoding="utf-8"))
    meta["settings"] = {"verify_routing": True, "verify_context": "yes", "semantic_mode": "hybrid", "fail_on": 1,
                        "endpoint": False, "extra": "x"}
    (d / "leads.json").write_text(json.dumps(meta), encoding="utf-8")
    row = view.summary(f"s1~{TS1}")
    assert row["settings"] == {"verify_routing": True, "semantic_mode": "hybrid", "endpoint": False}


def test_a_run_another_process_holds_is_running_elsewhere(view, root):
    from orion import runlock
    d = _run(root, "s1", TS1, leads=[_lead(0)])
    rid = f"s1~{TS1}"
    assert (view.summary(rid)["state"], view.summary(rid)["resumable"]) == ("stopped", True)
    held = runlock.acquire(d)
    try:
        row = view.summary(rid)
        assert (row["state"], row["elsewhere"], row["resumable"]) == ("running", True, False)
        assert view.summary(rid, active_state="stopping")["elsewhere"] is False   # this UI's own job wins
        assert [r["state"] for r in view.list()] == ["running"]
    finally:
        held.close()
    assert view.summary(rid)["state"] == "stopped"


def _cmp(view, root, a_verdicts, b_verdicts):
    _run(root, "sa", TS1, leads=[v.lead for v in a_verdicts], repo="r", final=a_verdicts)
    _run(root, "sb", TS2, leads=[v.lead for v in b_verdicts], repo="r", final=b_verdicts)
    return view.compare(f"sa~{TS1}", f"sb~{TS2}")


def test_compare_pairs_the_same_finding_cited_differently(view, root):
    """The real NodeGoat pair (2026-10-29): two runs named one $where injection's function and lines differently,
    so the exact fingerprint kept them apart. Same file, CWE and overlapping lines: one finding."""
    a = [Verdict(_lead(0, "where injection", file="app/data/allocations-dao.js", cwe="CWE-943",
                       function="getByUserIdAndThreshold.searchCriteria", line_start=77, line_end=86), "CONFIRM", "r"),
         Verdict(_lead(1, "redirect", file="./app\\routes\\index.js", cwe="cwe-0601", function="index",
                       line_start=70, line_end=73), "CONFIRM", "r"),
         Verdict(_lead(2, "csrf", file="server.js", cwe="CWE-352", line_start=104, line_end=113), "REJECT", "r")]
    b = [Verdict(_lead(0, "where injection again", file="fixtures/NodeGoat/app/data/allocations-dao.js",
                       cwe="CWE-943", function="getByUserIdAndThreshold", line_start=78), "CONFIRM", "r"),
         Verdict(_lead(1, "open redirect", file="app/routes/index.js", cwe="CWE-601", function="app.get /learn",
                       line_start=70, line_end=73), "CONFIRM", "r"),
         Verdict(_lead(2, "csrf", file="server.js", cwe="CWE-352", function="bootstrap", line_start=104,
                       line_end=113), "CONFIRM", "r")]
    out = _cmp(view, root, a, b)
    assert out["appeared"] == [] and out["vanished"] == [] and out["unchanged"] == 2
    assert [(p["a"]["line"], p["a"]["line_end"], p["b"]["line"]) for p in out["relocated"]] == [(70, 73, 70), (77, 86, 78)]
    [changed] = out["changed"]                                  # paired by location, and its decision changed
    assert changed["match"] == "location" and (changed["a"]["decision"], changed["b"]["decision"]) == ("REJECT", "CONFIRM")
    json.dumps(out)


def test_compare_second_pass_needs_file_cwe_and_overlapping_lines(view, root):
    base = dict(file="app/x.js", cwe="CWE-79", line_start=10, line_end=20)
    a = [Verdict(_lead(0, "xss", **base), "CONFIRM", "r")]
    for other in (dict(base, file="app/y.js"),                           # another file
                  dict(base, file="lib/app/x.js2"),                      # not a folder prefix of the same name
                  dict(base, cwe="CWE-80"),                              # another CWE
                  dict(base, line_start=21, line_end=30),                # lines next to, not overlapping
                  dict(base, line_start=None, line_end=None, function="render")):   # no lines to compare
        for d in (root / "sa", root / "sb"):
            if d.exists():
                import shutil
                shutil.rmtree(d)
        out = _cmp(view, root, a, [Verdict(_lead(0, "xss", **other), "CONFIRM", "r")])
        assert (len(out["appeared"]), len(out["vanished"]), out["relocated"]) == (1, 1, []), other


def test_compare_second_pass_is_one_to_one_largest_overlap_first(view, root):
    a = [Verdict(_lead(0, "one", file="f.js", cwe="CWE-89", function="a", line_start=10, line_end=12), "CONFIRM", "r"),
         Verdict(_lead(1, "two", file="f.js", cwe="CWE-89", function="b", line_start=11, line_end=30), "CONFIRM", "r")]
    b = [Verdict(_lead(0, "wide", file="f.js", cwe="CWE-89", function="c", line_start=12, line_end=28), "CONFIRM", "r")]
    out = _cmp(view, root, a, b)
    [pair] = out["relocated"]
    assert pair["a"]["function"] == "b"                         # 17 shared lines beat 1
    assert [r["function"] for r in out["vanished"]] == ["a"] and out["appeared"] == []

