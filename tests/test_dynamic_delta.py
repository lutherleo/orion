"""The delta report/events (orion/dynamic/delta). Pure functions over a synthetic summary dict."""
from __future__ import annotations

from orion.dynamic.delta import report_text, to_events

_SUMMARY = {
    "scan_id": "s1",
    "new_methods": 2,
    "observed_calls": 5,
    "observed_dispatches": 3,
    "calls_to_new_methods": 2,
    "method_samples": [{"name": "reflected", "file": "app/dyn.py", "line": 9}],
    "dispatch_samples": [{"site_file": "app/a.py", "site_line": 6, "target": "app.impl.Impl.handle"}],
}


def test_report_names_the_counts_and_samples():
    txt = report_text(_SUMMARY)
    assert "2 runtime-only methods" in txt
    assert "5 OBSERVED_CALL edges" in txt
    assert "3 OBSERVED_DISPATCH edges" in txt
    assert "reflected  app/dyn.py:9" in txt
    assert "app/a.py:6 -> app.impl.Impl.handle" in txt
    assert "LOWER BOUND" in txt          # honesty line present


def test_empty_delta_is_reported_honestly():
    empty = {**_SUMMARY, "new_methods": 0, "observed_calls": 0,
             "method_samples": [], "dispatch_samples": [], "calls_to_new_methods": 0}
    txt = report_text(empty)
    assert "empty delta" in txt


def test_events_are_dynamic_phase_progress_events():
    evs = to_events(_SUMMARY)
    assert evs and all(e["phase"] == "dynamic" for e in evs)
    assert any("runtime-only methods" in e["detail"] for e in evs)
