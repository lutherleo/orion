"""The run dirs `orion scan` writes, read for the UI: list, summarise, stream, show code, compare.

A run dir is `<root>/<scan_id>/<ts>/` (root = `<cwd>/.orion/runs`; `ts` is `cli._run_dir`'s
`%Y%m%dT%H%M%SZ`) and the CLI fills it as the scan goes: `progress.jsonl` (one ProgressEvent per
line), `leads.json` after discovery, `verdicts.jsonl` (one verdict appended as each lead is verified;
a crash can tear the last line), then `verdicts.json` + `report.txt` + `results.sarif` at the end.
This module only READS those files -- the UI never writes a run dir -- so a scan started from the
browser and one started in a terminal look the same, and a run is judged only by what is on disk.
Pure: stdlib only, no subprocess, no Neo4j, no network.

Load-bearing decisions:
- A run's id is `<scan_id>~<ts>`. Each part must FULLMATCH `PART` (never `.match`: `$` also matches
  before a trailing newline), so no separator, `..`, drive, `~` or newline reaches a path; and the
  resolved dir must be exactly `root/scan_id/ts`, so a linked dir that leads out of the root (or an
  alias such as a Windows trailing dot) is refused, never followed. Inside a run dir only the dir's
  own regular files are read: a symlink there is treated as absent (the CLI never makes one).
- State comes from which files exist: `done` (verdicts.json), `stopped` (leads.json; resumable on the
  claude engine, since `--resume` re-verifies only what is missing), else `incomplete` (crashed or
  stopped before discovery finished). A leads/verdicts file that exists but cannot be read makes the
  run `incomplete` with a `problem` -- never an exception, because one bad dir must not hide the rest
  of the list. `running`/`stopping` come from the caller (the job runner knows which process it
  started), or `running` with `elsewhere` True when another process holds the run dir's lock
  (orion/runlock.py: a scan in a terminal, say); they are never guessed from file times, which go
  quiet for minutes while one verifier session runs.
- Verdict counts read verdicts.json when it exists (the final word), else verdicts.jsonl with the
  LATEST line per lead index winning; unparseable lines (a torn last line) are skipped, as
  `cli._load_run` skips them. What `list()` derives from those files is cached against their
  (mode, inode, size, mtime), so polling the Runs page re-parses only a run that changed.
- `events` hands out only COMPLETE lines from a byte offset, so a reader polling a file the scan is
  still appending to never sees half an event, and its next offset resumes exactly where it stopped.
- `code` shows lines of the scanned repo, which is UNTRUSTED: `..` is refused, an absolute path must
  lie in the repo, and the file's REAL path (symlinks followed) must still be inside the repo's real
  root; size-capped, refused when binary. The prefix retry (`fixtures/NodeGoat/app/x.js` -> `app/x.js`)
  exists because agents cite paths relative to whatever directory they happened to be in.
- `compare` matches verdicts by `Lead.fingerprint()` taken on the effective location
  (`Verdict.location()`, mirrored on dicts here): the same bug however it is worded or whichever
  shape found it -- the dedup identity of `orion/dedup.py`. A verdict without that identity falls
  back to its lowercased, whitespace-collapsed lead text.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import stat
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path

from .. import runlock

PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")   # scan_id and ts parts (always fullmatch)
DOWNLOADS = ("report.txt", "results.sarif", "verdicts.json", "leads.json", "progress.jsonl")
DECISIONS = ("CONFIRM", "INCONCLUSIVE", "REJECT", "ERROR")
CODE_MAX_BYTES = 2_000_000
REPORT_MAX_BYTES = 1_000_000              # detail()'s inline report.txt; the download has all of it
ACTIVE_STATES = ("running", "stopping")   # the job runner's states, the only ones that override disk

_LEADS, _VERDICTS, _VERDICTS_LOG = "leads.json", "verdicts.json", "verdicts.jsonl"
_REPORT, _PROGRESS = "report.txt", "progress.jsonl"
_TS_FORMAT = "%Y%m%dT%H%M%SZ"             # cli._run_dir
_RANK = {"CONFIRM": 3, "INCONCLUSIVE": 2, "ERROR": 1, "REJECT": 0}   # compare: duplicates keep the most severe
_CODE_TRIES = 8
_BINARY_SNIFF = 8192
_CACHE_MAX = 1024
_INT = re.compile(r"\s*-?[0-9]{1,18}\s*")
# What Path.resolve() raises on a broken or looping link: OSError, or RuntimeError for a symlink loop on
# Python 3.12 -- even with strict=False, where it stats the result. Both must be caught where a bad
# link has to degrade to "absent" instead of escaping as an exception.
_LOOP = (OSError, RuntimeError)


class NotFound(KeyError):
    """A KeyError whose str() is its plain message (KeyError's own str() repr-quotes it), so a caller
    can put str(exc) straight into an error reply."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


def run_id(scan_id: str, ts: str) -> str:
    """`<scan_id>~<ts>`, a run's one id in URLs. ValueError unless both parts fullmatch PART (which
    has no `~`, so parse_run_id splits it back unambiguously)."""
    for part in (scan_id, ts):
        if not isinstance(part, str) or not PART.fullmatch(part):
            raise ValueError(f"not a valid run id part: {_show(part)}")
    return f"{scan_id}~{ts}"


def parse_run_id(rid: str) -> tuple[str, str]:
    """(scan_id, ts) from `<scan_id>~<ts>`; ValueError on anything else (a path separator, `..`, an
    empty part, a third part, a trailing newline...)."""
    parts = rid.split("~") if isinstance(rid, str) else []
    if len(parts) != 2 or not all(PART.fullmatch(p) for p in parts):
        raise ValueError(f"not a run id: {_show(rid)}")
    return parts[0], parts[1]


class Runs:
    """Read-only view of every run dir under `root` (see the module docstring). Thread-safe: the
    server calls it from many request threads; the only shared state is the summary cache."""

    def __init__(self, root: Path, *, base_dir: Path) -> None:
        self.base_dir = Path(base_dir).resolve()          # the cwd scans run in
        root = Path(root)
        self.root = root if root.is_absolute() else self.base_dir / root
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[tuple, dict]] = {}

    # ── ids and dirs ───────────────────────────────────────────────────────────────────────────
    def run_dir(self, rid: str) -> Path:
        """The run's resolved dir. KeyError (NotFound) when there is no such dir; ValueError when the
        id is malformed or the dir is a link that does not resolve to exactly root/scan_id/ts."""
        scan_id, ts = parse_run_id(rid)
        want = self._root() / scan_id / ts
        try:
            real = want.resolve(strict=True) if os.path.isdir(want) else None
        except _LOOP:
            real = None
        if real is None:
            raise NotFound(f"no run {rid}")
        if real != want:
            raise ValueError(f"run {rid} does not sit directly under the runs root")
        return real

    def rid_for(self, run_dir: Path) -> str | None:
        """The id of the run dir at `run_dir` (relative = to base_dir), or None when that is not a
        dir directly at root/<scan_id>/<ts>. It need not exist yet."""
        try:
            real, root = self._abs(run_dir), self._root()
        except (*_LOOP, ValueError, TypeError):
            return None
        if real.parent.parent != root:
            return None
        try:
            return run_id(real.parent.name, real.name)
        except ValueError:
            return None

    # ── listing and summaries ──────────────────────────────────────────────────────────────────
    def list(self, *, active: Mapping[str, str] | None = None) -> list[dict]:
        """Every run dir's summary, newest ts first (a ts that is not the CLI's timestamp sorts after
        all that are). `active` maps str(resolved run dir) -> "running"|"stopping" (the job runner's
        `active_dirs()`). Never raises over a bad dir: it is listed `incomplete` with a `problem`."""
        live = self._active(active)
        rows = []
        for scan_id, ts in self._names():
            rid = run_id(scan_id, ts)
            try:
                d = self.run_dir(rid)
            except (KeyError, ValueError):
                continue                    # gone since the listing, or a link out of the root
            try:
                rows.append(self._summary(d, live.get(d)))
            except Exception as exc:  # noqa: BLE001 -- one unreadable run dir must never hide the others
                rows.append(_broken(rid, scan_id, ts, live.get(d), exc))
        rows.sort(key=lambda r: (r["started"] is not None, r["started"] or "", r["ts"], r["scan_id"]),
                  reverse=True)
        return rows

    def summary(self, rid: str, *, active_state: str | None = None) -> dict:
        """{"id", "scan_id", "ts", "started", "repo", "engine", "model", "settings", "state", "elsewhere",
        "resumable", "counts", "n_leads", "n_verdicts", "last_activity", "problem"} -- see the module
        docstring for `state`; `active_state` is honoured only when it is "running" or "stopping".
        `settings` is leads.json's record of the verify options (cli._verify_settings), None before
        2026-10-29."""
        return self._summary(self.run_dir(rid), active_state)

    def detail(self, rid: str, *, active_state: str | None = None) -> dict:
        """summary + {"leads", "verdicts", "files" (the DOWNLOADS present), "report_text" (report.txt,
        CRLF folded to LF, cut at REPORT_MAX_BYTES with a note; None without it)}."""
        d = self.run_dir(rid)
        out = self._summary(d, active_state)
        meta, _ = _load_json(_member(d, _LEADS))
        leads = meta.get("leads") if isinstance(meta, dict) else None
        out["leads"] = [x for x in leads if isinstance(x, dict)] if isinstance(leads, list) else []
        out["verdicts"] = _verdicts_of(d)[0]
        out["files"] = [name for name in DOWNLOADS if _member(d, name) is not None]
        out["report_text"] = _report_text(_member(d, _REPORT))
        return out

    def verdicts(self, rid: str) -> list[dict]:
        """The run's verdicts as stored (`asdict(Verdict)`): verdicts.json when present and readable,
        else verdicts.jsonl with the latest line per lead index, ordered by index."""
        return _verdicts_of(self.run_dir(rid))[0]

    # ── files ──────────────────────────────────────────────────────────────────────────────────
    def events(self, rid: str, offset: int = 0, limit_bytes: int = 1_000_000) -> tuple[list[dict], int]:
        """(events, next_offset): the progress events whose lines START at or after byte `offset` and
        END (with "\\n") within `limit_bytes`. A line still being written is left for the next call;
        unparseable lines are skipped but consumed. Offset at/past EOF -> ([], offset); no log yet ->
        ([], 0). Offsets are raw bytes of the file (CRLF on Windows included)."""
        d = self.run_dir(rid)
        offset, limit = _as_int(offset, "offset"), _as_int(limit_bytes, "limit_bytes")
        if offset < 0 or limit < 1:
            raise ValueError("offset must be >= 0 and limit_bytes >= 1")
        path = _member(d, _PROGRESS)
        if path is None:
            return [], 0
        try:
            f = open(path, "rb")
        except OSError:
            return [], 0
        with f:
            if offset >= os.fstat(f.fileno()).st_size:
                return [], offset
            f.seek(offset)
            data = f.read(limit)
            cut = data.rfind(b"\n")
            if cut < 0:
                return [], _past_long_line(f, offset, data, limit)
        events = []
        for raw in data[:cut].split(b"\n"):
            if not raw.strip():
                continue
            try:
                evt = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if isinstance(evt, dict):
                events.append(evt)
        return events, offset + cut + 1

    def file_path(self, rid: str, name: str) -> Path:
        """The path of downloadable file `name` of the run. KeyError (NotFound) unless `name` is
        exactly one of DOWNLOADS and is the run dir's own regular file."""
        d = self.run_dir(rid)
        path = _member(d, name) if isinstance(name, str) and name in DOWNLOADS else None
        if path is None:
            raise NotFound(f"{_show(name)} is not a downloadable file of run {rid}")
        return path

    def code(self, rid: str, file: str, line: int, end: int | None = None, context: int = 8) -> dict:
        """The cited lines of `file` in the run's repo (leads.json "repo"; relative = to base_dir),
        read-only: {"file" (relative to the repo, of the real path), "start", "end", "highlight":
        [line, end], "lines": [{"n", "text"}]} with `context` lines around [line, end].

        KeyError (NotFound): no readable leads.json, no recorded repo, the repo or the file is not
        there. PermissionError: `..`, an absolute path outside the repo, or a real path (symlinks
        followed) outside the repo's real root. ValueError: a bad argument, a file over
        CODE_MAX_BYTES or with a NUL byte in its first 8 KB, `line` outside 1..n_lines. `end` is
        clipped into [line, n_lines] rather than refused: a verifier-corrected start can land past
        the lead's own end, and a file can shrink after the scan."""
        first = _as_int(line, "line")
        last = None if end is None or end == "" else _as_int(end, "end")
        around = _as_int(context, "context")
        if around < 0:
            raise ValueError("context must not be negative")
        d = self.run_dir(rid)
        lexical, root = self._repo(d, rid)
        real = _locate(file, lexical, root)
        shown = real.relative_to(root).as_posix()
        lines = _read_lines(real, shown)
        n = len(lines)
        if not 1 <= first <= n:
            raise ValueError(f"line {first} is outside {shown}, which has {n} lines")
        last = min(max(last if last is not None else first, first), n)
        start, stop = max(1, first - around), min(n, last + around)
        return {"file": shown, "start": start, "end": stop, "highlight": [first, last],
                "lines": [{"n": i, "text": lines[i - 1]} for i in range(start, stop + 1)]}

    # ── compare ────────────────────────────────────────────────────────────────────────────────
    def compare(self, rid_a: str, rid_b: str) -> dict:
        """What changed from run a to run b: {"a", "b" (summaries), "same_repo", "appeared" (rows in b
        only), "vanished" (rows in a only), "changed" ([{"key", "a": row, "b": row}] whose decision
        differs), "unchanged" (a count)}; row = {"key", "file", "line", "function", "cwe", "decision",
        "severity", "text"}. Matching: see the module docstring; two verdicts of one run with the same
        key keep the most severe decision (CONFIRM > INCONCLUSIVE > ERROR > REJECT)."""
        da, db = self.run_dir(rid_a), self.run_dir(rid_b)
        a, b = self._summary(da, None), self._summary(db, None)
        rows_a, rows_b = _by_key(_verdicts_of(da)[0]), _by_key(_verdicts_of(db)[0])
        changed, unchanged = [], 0
        for key, row in rows_b.items():
            old = rows_a.get(key)
            if old is None:
                continue
            if old["decision"] == row["decision"]:
                unchanged += 1
            else:
                changed.append({"key": row["key"], "a": old, "b": row})
        return {"a": a, "b": b, "same_repo": self._same_repo(a, b),
                "appeared": [row for key, row in rows_b.items() if key not in rows_a],
                "vanished": [row for key, row in rows_a.items() if key not in rows_b],
                "changed": changed, "unchanged": unchanged}

    # ── internals ──────────────────────────────────────────────────────────────────────────────
    def _root(self) -> Path:
        # Resolved per call, not once: the root may not exist until the first scan creates it. A
        # looping root stays unresolved; listing it then finds nothing and every run id is absent.
        try:
            return self.root.resolve()
        except _LOOP:
            return Path(os.path.abspath(self.root))

    def _abs(self, path) -> Path:
        p = Path(path)
        return (p if p.is_absolute() else self.base_dir / p).resolve()

    def _names(self) -> Iterator[tuple[str, str]]:
        root = self._root()
        for scan_id in _subdirs(root):
            for ts in _subdirs(root / scan_id):
                yield scan_id, ts

    def _active(self, active: Mapping[str, str] | None) -> dict[Path, str]:
        # Keyed by resolved Path, not str: Path equality folds case on Windows, a str compare does not.
        out: dict[Path, str] = {}
        for key, state in (active or {}).items():
            if state in ACTIVE_STATES:
                try:
                    out[self._abs(key)] = state
                except (*_LOOP, ValueError, TypeError):
                    continue
        return out

    def _summary(self, d: Path, active_state: str | None) -> dict:
        snap = self._snapshot(d)
        scan_id, ts = d.parent.name, d.name
        live = active_state if active_state in ACTIVE_STATES else None
        elsewhere = live is None and runlock.held(d)       # live, not cached: a lock changes with no file write
        state = live or ("running" if elsewhere else snap["state"])
        return {"id": run_id(scan_id, ts), "scan_id": scan_id, "ts": ts, "started": _started(ts),
                "repo": snap["repo"], "engine": snap["engine"], "model": snap["model"],
                "settings": snap["settings"], "state": state, "elsewhere": elsewhere,
                "resumable": state == "stopped" and snap["engine"] == "claude",
                "counts": dict(snap["counts"]), "n_leads": snap["n_leads"], "n_verdicts": snap["n_verdicts"],
                "last_activity": _last_activity(d), "problem": snap["problem"]}

    def _snapshot(self, d: Path) -> dict:
        # The key is taken BEFORE the files are read, so a write racing the read can only leave a
        # NEWER result under an older key -- which the next call's fresh key then replaces.
        key = tuple(_stat_key(d / name) for name in (_LEADS, _VERDICTS, _VERDICTS_LOG))
        with self._lock:
            hit = self._cache.get(d)
        if hit is not None and hit[0] == key:
            return hit[1]
        snap = _read_snapshot(d)
        with self._lock:
            if len(self._cache) >= _CACHE_MAX:
                self._cache.clear()
            self._cache[d] = (key, snap)
        return snap

    def _repo(self, d: Path, rid: str) -> tuple[Path, Path]:
        """(lexical absolute repo path, its real path) of the run, or NotFound."""
        meta, _ = _load_json(_member(d, _LEADS))
        if not isinstance(meta, dict):
            raise NotFound(f"run {rid} has no readable {_LEADS}, so its repo is unknown")
        repo = meta.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise NotFound(f"run {rid} recorded no repo path")
        p = Path(repo)
        lexical = Path(os.path.abspath(p if p.is_absolute() else self.base_dir / p))
        try:
            root = lexical.resolve(strict=True)
        except _LOOP:
            root = None
        if root is None or not root.is_dir():
            raise NotFound(f"the repo of run {rid} is not on this machine any more: {repo}")
        return lexical, root

    def _same_repo(self, a: dict, b: dict) -> bool:
        if a["scan_id"] == b["scan_id"]:
            return True                     # a scan_id hashes the repo's absolute path + commit
        if not (a["repo"] and b["repo"]):
            return False
        try:
            return self._abs(a["repo"]) == self._abs(b["repo"])
        except (*_LOOP, ValueError):
            return a["repo"] == b["repo"]


# ── reading a run dir ───────────────────────────────────────────────────────────────────────────
def _subdirs(path: Path) -> list[str]:
    """Sorted names of the PART-shaped directories in `path`; [] when it cannot be listed."""
    names = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if PART.fullmatch(entry.name) and entry.is_dir():
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(names)


def _member(d: Path, name: str) -> Path | None:
    """`d/name` when it is a regular file of the run dir itself, else None."""
    path = d / name
    try:
        return path if stat.S_ISREG(os.lstat(path).st_mode) else None
    except (OSError, ValueError):
        return None


def _stat_key(path: Path) -> tuple | None:
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return None
    return (st.st_mode, st.st_ino, st.st_size, st.st_mtime_ns)


def _load_json(path: Path | None) -> tuple[object, str | None]:
    """(parsed, None); (None, None) when there is no such file; (None, problem) when it exists but
    cannot be read or parsed (RecursionError too: deeply nested junk must not escape `list()`)."""
    if path is None:
        return None, None
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{path.name} cannot be read ({_why(exc)})"
    try:
        return json.loads(data), None
    except (ValueError, RecursionError) as exc:      # JSONDecodeError, UnicodeDecodeError
        return None, f"{path.name} is not valid JSON ({_why(exc)})"


def _read_verdict_log(path: Path | None) -> tuple[list[dict] | None, str | None]:
    """verdicts.jsonl as one verdict per lead index -- the LATEST line for each wins -- ordered by
    index; (None, None) without the file."""
    if path is None:
        return None, None
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{path.name} cannot be read ({_why(exc)})"
    latest: dict[int, dict] = {}
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            v = json.loads(raw)
        except (ValueError, RecursionError):
            continue                        # a line torn by a crash mid-write
        lead = v.get("lead") if isinstance(v, dict) else None
        index = lead.get("index") if isinstance(lead, dict) else None
        if isinstance(index, int) and not isinstance(index, bool):
            latest[index] = v
    return [latest[i] for i in sorted(latest)], None


def _verdicts_of(d: Path) -> tuple[list[dict], str | None, list[str]]:
    """(verdicts, the file they came from or None, problems): verdicts.json when it exists and is a
    list, else the deduplicated verdicts.jsonl."""
    problems: list[str] = []
    final, problem = _load_json(_member(d, _VERDICTS))
    if problem:
        problems.append(problem)
    elif final is not None:
        if isinstance(final, list):
            # The page may rely on `lead` being an object; a non-dict entry is no verdict at all.
            return [v if isinstance(v.get("lead"), dict) else {**v, "lead": {}}
                    for v in final if isinstance(v, dict)], _VERDICTS, []
        problems.append(f"{_VERDICTS} is not a list of verdicts")
    logged, problem = _read_verdict_log(_member(d, _VERDICTS_LOG))
    if problem:
        problems.append(problem)
    return logged or [], (_VERDICTS_LOG if logged is not None else None), problems


def _read_snapshot(d: Path) -> dict:
    """Everything a summary derives from leads.json / verdicts.json / verdicts.jsonl."""
    problems: list[str] = []
    meta, problem = _load_json(_member(d, _LEADS))
    leads_ok, n_leads, repo, engine, model, settings = False, None, None, None, None, None
    if problem:
        problems.append(problem)
    elif meta is not None:
        if isinstance(meta, dict):
            repo, engine, model = (_text(meta.get(k)) for k in ("repo", "engine", "model"))
            settings = _settings(meta.get("settings"))
            if isinstance(meta.get("leads"), list):
                leads_ok, n_leads = True, len(meta["leads"])
        if not leads_ok:
            problems.append(f"{_LEADS} holds no list of leads")
    verdicts, source, more = _verdicts_of(d)
    problems += more
    counts = dict.fromkeys(DECISIONS, 0)
    for v in verdicts:
        counts[_decision(v)] += 1
    if problems:
        state = "incomplete"
    elif source == _VERDICTS:
        state = "done"
    else:
        state = "stopped" if leads_ok else "incomplete"
    return {"repo": repo, "engine": engine or "claude", "model": model, "settings": settings,
            "n_leads": n_leads, "counts": counts, "n_verdicts": len(verdicts), "state": state,
            "problem": "; ".join(problems) or None}


def _settings(x: object) -> dict | None:
    """leads.json's "settings" (cli._verify_settings) keeping only its known keys with the right types; None
    when the run recorded none."""
    if not isinstance(x, dict):
        return None
    out = {k: x[k] for k in ("verify_routing", "verify_context", "endpoint") if isinstance(x.get(k), bool)}
    out.update({k: x[k] for k in ("semantic_mode", "fail_on") if isinstance(x.get(k), str)})
    return out


def _decision(v: dict) -> str:
    """The verdict's decision; a missing or unknown one counts as ERROR (cli._load_run's rule)."""
    d = v.get("decision")
    return d if isinstance(d, str) and d in DECISIONS else "ERROR"


def _broken(rid: str, scan_id: str, ts: str, live: str | None, exc: BaseException) -> dict:
    return {"id": rid, "scan_id": scan_id, "ts": ts, "started": _started(ts), "repo": None,
            "engine": "claude", "model": None, "settings": None, "state": live or "incomplete",
            "elsewhere": False, "resumable": False,
            "counts": dict.fromkeys(DECISIONS, 0), "n_leads": None, "n_verdicts": 0,
            "last_activity": None, "problem": f"this run dir cannot be read ({_why(exc)})"}


def _last_activity(d: Path) -> str | None:
    """ISO time of the newest file in the run dir (the dir's own time when it holds none)."""
    newest = None
    try:
        with os.scandir(d) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                        newest = mtime if newest is None else max(newest, mtime)
                except OSError:
                    continue
    except OSError:
        pass
    if newest is None:
        try:
            newest = d.stat().st_mtime
        except OSError:
            return None
    return _iso(newest)


def _report_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(REPORT_MAX_BYTES + 1)
    except OSError:
        return None
    text = data[:REPORT_MAX_BYTES].decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(data) > REPORT_MAX_BYTES:
        text += f"\n[report.txt is longer than {REPORT_MAX_BYTES:,} bytes; download it for the rest]"
    return text


def _past_long_line(f, offset: int, data: bytes, limit: int) -> int:
    """The next offset when the window held no complete line. Normally the last line is still being
    written, so stay at `offset`; but ONE line longer than the whole window would stall every reader
    forever, so it is skipped (its end found by reading on in chunks) once it is complete."""
    if len(data) < limit:
        return offset
    pos = offset + len(data)
    while chunk := f.read(65536):
        i = chunk.find(b"\n")
        if i >= 0:
            return pos + i + 1
        pos += len(chunk)
    return offset


# ── the code view ───────────────────────────────────────────────────────────────────────────────
def _locate(file: object, lexical: Path, root: Path) -> Path:
    """The real path of `file` inside the repo (see Runs.code for the rules and errors)."""
    if not isinstance(file, str):
        raise ValueError("file must be a string")
    if "\x00" in file:
        raise ValueError("the file name contains a NUL byte")
    name = file.strip()
    while name.startswith("./"):
        name = name[2:]
    path = Path(name)
    if not path.parts:
        raise ValueError("no file named")
    if ".." in path.parts:
        raise PermissionError(f"{_show(file)} climbs out of the repo ('..' is refused)")
    if path.anchor:
        # Checked LEXICALLY before anything touches the disk, so an outside path (a UNC share on
        # Windows included) is never even stat'ed. `anchor`, not is_absolute(): on Windows "/etc/x"
        # is rooted but not absolute, and would otherwise be joined onto the repo's drive below.
        absolute = Path(os.path.abspath(path))
        if not (absolute.is_relative_to(lexical) or absolute.is_relative_to(root)):
            raise PermissionError(f"{_show(file)} is outside the run's repo")
        candidates = [absolute]
    else:
        parts = path.parts
        candidates = [root.joinpath(*parts[i:]) for i in range(min(len(parts), _CODE_TRIES))]
        if not all(c.is_relative_to(root) for c in candidates):
            # Only a drive-like part ("c:x", Windows) makes a join leave the root; no repo path has one.
            raise PermissionError(f"{_show(file)} is outside the run's repo")
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            real = cand.resolve(strict=True)
        except _LOOP:
            continue
        # The first file that exists decides: one that leads out is refused, never skipped past.
        if not real.is_relative_to(root):
            raise PermissionError(f"{_show(file)} leads out of the run's repo")
        return real
    raise NotFound(f"{_show(file)} is not in the run's repo")


def _read_lines(path: Path, shown: str) -> list[str]:
    """The file's lines without their newline ("\\n"; a CRLF's "\\r" dropped too), UTF-8 with
    replacement. Lines split on "\\n" only, as editors and tracebacks number them (str.splitlines
    would also split on \\f, \\v, U+2028... and shift every line after one)."""
    try:
        with open(path, "rb") as f:
            data = f.read(CODE_MAX_BYTES + 1)       # bounded: the file may grow after any stat
    except PermissionError:
        raise
    except OSError as exc:
        raise NotFound(f"{shown} cannot be read ({_why(exc)})") from None
    if len(data) > CODE_MAX_BYTES:
        raise ValueError(f"{shown} is larger than {CODE_MAX_BYTES:,} bytes; open it in an editor")
    if b"\x00" in data[:_BINARY_SNIFF]:
        raise ValueError(f"{shown} looks like a binary file")
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines[-1] == "":
        lines.pop()                         # the newline ending the last line starts no line
    return [x[:-1] if x.endswith("\r") else x for x in lines]


# ── compare ─────────────────────────────────────────────────────────────────────────────────────
def _text(x: object) -> str | None:
    return x if isinstance(x, str) and x else None


def _line(x: object) -> int | None:
    return x if isinstance(x, int) and not isinstance(x, bool) and x >= 1 else None


def _location(v: dict) -> dict:
    """contracts.Verdict.location() on a verdict DICT: the verifier's file / lines / cwe win; a
    verifier-corrected file resets the lead's lines and function. Malformed values count as absent."""
    lead = v.get("lead") if isinstance(v.get("lead"), dict) else {}
    vfile, lfile = _text(v.get("file")), _text(lead.get("file"))
    moved = bool(vfile and vfile != lfile)
    return {"file": vfile or lfile,
            "line_start": _line(v.get("line_start")) or (None if moved else _line(lead.get("line_start"))),
            "line_end": _line(v.get("line_end")) or (None if moved else _line(lead.get("line_end"))),
            "function": None if moved else _text(lead.get("function")),
            "cwe": _text(v.get("cwe")) or _text(lead.get("cwe"))}


def _fingerprint(loc: dict) -> tuple[str, str, str] | None:
    """contracts.Lead.fingerprint() on an effective location: (file, cwe, function-or-line), or None
    unless all three are known."""
    where = loc["function"] or (str(loc["line_start"]) if loc["line_start"] else "")
    return (loc["file"], loc["cwe"], where) if (loc["file"] and loc["cwe"] and where) else None


def _row(v: dict) -> tuple[tuple, dict]:
    """(match key, display row) of one verdict. The key is a tagged tuple, so a fingerprint and a
    lead text can never collide; the row's "key" is its readable form."""
    loc = _location(v)
    lead = v.get("lead") if isinstance(v.get("lead"), dict) else {}
    text = lead.get("text") if isinstance(lead.get("text"), str) else ""
    fp = _fingerprint(loc)
    if fp is not None:
        key, label = ("fingerprint", *fp), " | ".join(fp)
    else:
        norm = " ".join(text.lower().split())
        key, label = ("text", norm), f"text | {norm}"
    return key, {"key": label, "file": loc["file"], "line": loc["line_start"], "function": loc["function"],
                 "cwe": loc["cwe"], "decision": _decision(v), "severity": _text(v.get("severity")),
                 "text": text}


def _by_key(verdicts: list[dict]) -> dict[tuple, dict]:
    """Rows by match key in first-seen order; a duplicate key keeps the most severe decision."""
    rows: dict[tuple, dict] = {}
    for v in verdicts:
        key, row = _row(v)
        kept = rows.get(key)
        if kept is None or _RANK[row["decision"]] > _RANK[kept["decision"]]:
            rows[key] = row
    return rows


# ── small helpers ───────────────────────────────────────────────────────────────────────────────
def _as_int(value: object, name: str) -> int:
    """An int argument, also accepted as its decimal string (query parameters arrive as text); never
    a bool, which Python counts as an int but no caller means as a number."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _INT.fullmatch(value):
        return int(value)
    raise ValueError(f"{name} must be a whole number, not {_show(value)}")


def _started(ts: str) -> str | None:
    """ISO 8601 UTC of a CLI run-dir timestamp, or None for a name that is not one."""
    try:
        when = datetime.datetime.strptime(ts, _TS_FORMAT)
    except ValueError:
        return None
    return when.replace(tzinfo=datetime.timezone.utc).isoformat(timespec="seconds")


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat(timespec="seconds")


def _why(exc: BaseException) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return f"{exc.msg} at line {exc.lineno}, column {exc.colno}"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


def _show(value: object) -> str:
    """A short repr for error messages: a caller-supplied name can be arbitrarily long."""
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."
