"""A src-layout Python repo is parsed from a lifted copy, so absolute imports bind to the real methods, and
its paths go back to repo-relative. Token-free; the last test runs Joern and skips without it."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from orion import graph_build
from orion.graph import joern_adapter as J
from orion.graph import profiles, stream_build
from tests.test_sink_profile import _stub_build


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


PKG = {"src/pkg/__init__.py": "", "src/pkg/core.py": "def work(a):\n    return a\n",
       "src/pkg/sub/__init__.py": "def helper(x):\n    return x\n",
       "src/pkg/user.py": "from pkg.sub import helper\nfrom pkg.core import work\n\n\ndef use(a):\n"
                          "    helper(a)\n    return work(a)\n"}


def test_src_layout_detected(tmp_path):
    assert J.is_python_src_layout(_tree(tmp_path, PKG))


def test_a_flat_repo_is_not(tmp_path):
    assert not J.is_python_src_layout(_tree(tmp_path, {"pkg/a.py": "from pkg.b import f\n"}))


def test_src_as_a_package_is_not(tmp_path):
    assert not J.is_python_src_layout(_tree(tmp_path, {**PKG, "src/__init__.py": ""}))


def test_code_importing_through_src_is_not(tmp_path):
    repo = _tree(tmp_path, {"src/a.py": "def f():\n    pass\n", "main.py": "from src.a import f\n"})
    assert not J.is_python_src_layout(repo)


def test_only_relative_imports_is_not(tmp_path):
    assert not J.is_python_src_layout(_tree(tmp_path, {"src/pkg/a.py": "from .b import f\n", "src/pkg/b.py": ""}))


def test_src_without_python_is_not(tmp_path):
    assert not J.is_python_src_layout(_tree(tmp_path, {"src/main.c": "int main;\n", "a.py": "import os\n"}))


def test_lift_keeps_every_file_and_maps_it_back(tmp_path):
    repo = _tree(tmp_path / "repo", {**PKG, "tests/t.py": "x = 1\n", "conftest.py": "root = 1\n",
                                     "src/conftest.py": "src = 1\n", ".git/hook.py": "no = 1\n"})
    view = tmp_path / "view"
    paths = J._lift_src(repo, view)
    assert (view / "pkg/core.py").read_text() == PKG["src/pkg/core.py"]
    assert (view / "tests/t.py").exists() and "tests/t.py" not in paths     # unmoved: no entry
    assert paths["pkg/core.py"] == "src/pkg/core.py"
    assert (view / "conftest.py").read_text() == "src = 1\n"                # a same-path pair keeps both files
    assert paths[f"{J._ROOT_FILES}/conftest.py"] == "conftest.py"
    assert paths["conftest.py"] == "src/conftest.py"
    assert not (view / ".git").exists()


def test_restore_paths_changes_first_party_names_only():
    env = {"nodes": [
        {"label": "FILE", "id": 1, "props": {"NAME": "pkg/core.py"}},
        {"label": "FILE", "id": 2, "props": {"NAME": "N/A"}},
        {"label": "METHOD", "id": 3, "props": {"FULL_NAME": "pkg/core.py:<module>.work", "FILENAME": "pkg/core.py"}},
        {"label": "METHOD", "id": 4, "props": {"FULL_NAME": "os.py:<module>.system", "FILENAME": "<empty>"}},
        {"label": "CALL", "id": 5, "props": {"METHOD_FULL_NAME": "pkg/core.py:<module>.work", "file_path": "pkg/core.py",
                                             "CODE": "pkg/core.py:<module>"}},
        {"label": "METHOD_PARAMETER_IN", "id": 6, "props": {"method_full_name": "pkg/core.py:<module>.work"}},
    ], "edges": [{"label": "CALL", "out": 5, "in": 3}], "entry_methods": ["pkg/core.py:<module>.work"]}
    out = J.restore_paths(env, {"pkg/core.py": "src/pkg/core.py"})
    props = {n["id"]: n["props"] for n in out["nodes"]}
    assert props[1]["NAME"] == "src/pkg/core.py" and props[2]["NAME"] == "N/A"
    assert props[3] == {"FULL_NAME": "src/pkg/core.py:<module>.work", "FILENAME": "src/pkg/core.py"}
    assert props[4] == {"FULL_NAME": "os.py:<module>.system", "FILENAME": "<empty>"}
    assert props[5]["METHOD_FULL_NAME"] == "src/pkg/core.py:<module>.work"
    assert props[5]["file_path"] == "src/pkg/core.py"
    assert props[5]["CODE"] == "pkg/core.py:<module>"                       # source text is never rewritten
    assert props[6]["method_full_name"] == "src/pkg/core.py:<module>.work"
    assert out["entry_methods"] == ["src/pkg/core.py:<module>.work"] and out["source_root"] == "src"
    assert out["edges"] == env["edges"]


def test_restore_paths_without_moves_is_the_same_object():
    env = {"nodes": [], "edges": []}
    assert J.restore_paths(env, {}) is env


def _fake_parse(monkeypatch, seen):
    monkeypatch.setattr(J, "_joern_bin", lambda name: Path(__file__))     # any file that exists

    def run(cmd, **_):
        tree = Path(cmd[cmd.index("--language") - 1])
        seen.update(tree=tree, language=cmd[cmd.index("--language") + 1],
                    files=sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*.py")))
        Path(cmd[cmd.index("--output") + 1]).write_text("cpg")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(J.subprocess, "run", run)


def test_a_src_layout_parses_a_view_that_is_then_removed(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_parse(monkeypatch, seen)
    repo = _tree(tmp_path / "repo", PKG)
    cpg, moved = J.ensure_cpg(repo, "pythonsrc")
    assert cpg.exists() and seen["tree"] != repo.resolve() and "pkg/core.py" in seen["files"]
    assert moved["pkg/core.py"] == "src/pkg/core.py"
    assert not seen["tree"].exists()


def test_other_repos_parse_the_repo_itself(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_parse(monkeypatch, seen)
    flat = _tree(tmp_path / "flat", {"pkg/a.py": "from pkg.b import f\n"})
    assert J.ensure_cpg(flat, "pythonsrc")[1] == {} and seen["tree"] == flat.resolve()
    js = _tree(tmp_path / "js", PKG)
    assert J.ensure_cpg(js, "jssrc")[1] == {} and seen["tree"] == js.resolve()      # Python only


def test_a_prebuilt_cpg_is_reused_with_nothing_moved(tmp_path):
    repo = _tree(tmp_path, {**PKG, "cpg.bin": "x"})
    assert J.ensure_cpg(repo, "pythonsrc") == (repo.resolve() / "cpg.bin", {})


MOVED_ENV = {"nodes": [{"label": "FILE", "id": 1, "props": {"NAME": "pkg/a.py"}}], "edges": [], "entry_methods": []}


def test_the_stream_build_restores_paths_and_says_so(tmp_path, monkeypatch):
    _stub_build(monkeypatch, tmp_path, [], [])
    seen, events = {}, []
    monkeypatch.setattr(J, "ensure_cpg", lambda r, f: (Path("cpg.bin"), {"pkg/a.py": "src/pkg/a.py"}))
    monkeypatch.setattr(stream_build, "build_envelope", lambda *a, **k: MOVED_ENV)
    monkeypatch.setattr(J, "normalize", lambda env, *a, **k: seen.update(env=env) or SimpleNamespace(nodes=[], edges=[]))
    graph_build.build(str(tmp_path), None, events.append, stream=True, scan_id="s")
    assert seen["env"]["nodes"][0]["props"]["NAME"] == "src/pkg/a.py"
    assert any("src/ as the import root" in e["detail"] for e in events)


def test_a_plain_stream_build_says_nothing(tmp_path, monkeypatch):
    _stub_build(monkeypatch, tmp_path, [], [])
    events: list = []
    monkeypatch.setattr(J, "ensure_cpg", lambda r, f: (Path("cpg.bin"), {}))
    monkeypatch.setattr(stream_build, "build_envelope", lambda *a, **k: MOVED_ENV)
    graph_build.build(str(tmp_path), None, events.append, stream=True, scan_id="s")
    assert not any("import root" in e["detail"] for e in events)


def test_the_legacy_export_restores_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(J, "_parse", lambda repo, lang, out, env: {"pkg/a.py": "src/pkg/a.py"})
    monkeypatch.setattr(J, "_run_export", lambda *a, **k: MOVED_ENV)
    assert J.export_repo(tmp_path, "pythonsrc")["nodes"][0]["props"]["NAME"] == "src/pkg/a.py"


def test_imports_bind_to_real_methods_live(tmp_path, needs_joern):
    repo = _tree(tmp_path / "repo", PKG)
    cpg, moved = J.ensure_cpg(repo, "pythonsrc")
    env = J.restore_paths(stream_build.build_envelope(str(cpg), tmp_path / "work", profiles.select_profile(str(repo), "python")),
                          moved)
    methods = {n["id"]: n["props"] for n in env["nodes"] if n["label"] == "METHOD"}
    calls = {n["id"]: n["props"] for n in env["nodes"] if n["label"] == "CALL"}
    bound = {calls[e["out"]]["NAME"]: methods[e["in"]] for e in env["edges"]
             if e["label"] == "CALL" and calls[e["out"]]["file_path"] == "src/pkg/user.py"}
    assert bound["helper"]["FULL_NAME"] == "src/pkg/sub/__init__.py:<module>.helper"
    assert bound["work"]["FULL_NAME"] == "src/pkg/core.py:<module>.work"
    assert not bound["helper"].get("IS_EXTERNAL") and not bound["work"].get("IS_EXTERNAL")
    for n in env["nodes"]:
        if n["label"] == "FILE" and n["props"]["NAME"] != "N/A":
            assert (repo / n["props"]["NAME"]).is_file()
