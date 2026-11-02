import os
import stat
import subprocess

import pytest

from eval import repo

posix_non_root = pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX modes, which root ignores")
windows_only = pytest.mark.skipif(os.name != "nt", reason="the read-only flag and junctions are Windows only")


def test_vulnerable_commit_uses_mirror_C_flag():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return "PARENT\n"
    assert repo.vulnerable_commit("FIX", "/m", run=run) == "PARENT"
    assert calls[0] == ["git", "-C", "/m", "rev-parse", "FIX^"]


def test_export_tree_archives_without_git(tmp_path):
    seen = []

    def run(cmd):
        seen.append(cmd)
        return ""
    repo.export_tree("/m", "SHA", str(tmp_path / "t"), run=run)
    assert seen[0][:4] == ["git", "-C", "/m", "archive"] and "SHA" in seen[0]
    assert seen[1][0] == "tar"                      # extract is a second argv, not a shell pipe
    assert not any("clone" in c for c in seen)      # never a working clone


def test_mirror_clones_when_absent_and_fetches_when_present(tmp_path):
    seen = []

    def run(cmd):
        seen.append(cmd)
        return ""
    m = tmp_path / "m"
    def run_ok(cmd):
        seen.append(cmd)
        os.makedirs(cmd[-1])
        return ""
    repo.mirror("https://x/y.git", str(m), run=run_ok)
    assert m.is_dir() and not (tmp_path / "m.tmp").exists()    # renamed on success
    assert seen[-1] == ["git", "clone", "--bare", "--", "https://x/y.git", f"{m}.tmp"]   # a "-..." url is never an option
    repo.mirror("https://x/y.git", str(m), run=run)
    assert seen[-1][:4] == ["git", "-C", str(m), "fetch"]


def test_export_tree_real_git_has_no_dotgit(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "a.js").write_text("x\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "c"], check=True)
    repo.mirror(str(src), str(tmp_path / "m"))
    sha = subprocess.run(["git", "-C", str(tmp_path / "m"), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    repo.export_tree(str(tmp_path / "m"), sha, str(tmp_path / "t"))
    assert (tmp_path / "t" / "a.js").exists() and not (tmp_path / "t" / ".git").exists()
    assert not (tmp_path / "t.tar").exists()


def test_mirror_partial_clone_is_not_a_mirror(tmp_path):
    m = tmp_path / "m"

    def run_fail(cmd):
        os.makedirs(cmd[-1])
        raise RuntimeError("network")
    try:
        repo.mirror("u", str(m), run=run_fail)
    except RuntimeError:
        pass
    assert not m.exists()                                      # partial stays at m.tmp only
    seen = []
    def run_ok(c):
        seen.append(c)
        os.makedirs(c[-1])
        return ""
    repo.mirror("u", str(m), run=run_ok)   # retry re-clones (stale .tmp cleared), not fetch
    assert seen[0][1] == "clone" and m.is_dir()


@posix_non_root
def test_remove_tree_does_not_raise_on_an_unreadable_folder(tmp_path):
    locked = tmp_path / "tree" / "locked"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x")
    locked.chmod(0)
    try:
        repo.remove_tree(str(tmp_path / "tree"))         # rmtree reports os.open, which needs more than a path
    finally:
        locked.chmod(0o700)
    assert (locked / "f").exists()


@posix_non_root
def test_remove_tree_changes_no_mode_on_posix(tmp_path):
    """A read-only folder refuses each unlink. A retry's chmod would follow the link to a file outside the tree."""
    outside = tmp_path / "outside"
    outside.write_text("x")
    outside.chmod(0o644)
    tree = tmp_path / "tree"
    ro = tree / "ro"
    ro.mkdir(parents=True)
    (ro / "file").write_text("x")
    (ro / "file").chmod(0o644)
    (ro / "link").symlink_to(outside)
    ro.chmod(0o500)
    tree.chmod(0o700)
    try:
        repo.remove_tree(str(tree))
        assert stat.S_IMODE(outside.stat().st_mode) == 0o644
        assert stat.S_IMODE(tree.stat().st_mode) == 0o700 and stat.S_IMODE(ro.stat().st_mode) == 0o500
        assert stat.S_IMODE((ro / "file").stat().st_mode) == 0o644
    finally:                                             # even a failing run must leave a tree pytest can delete
        tree.chmod(0o700)
        ro.chmod(0o700)


@posix_non_root
def test_remove_tree_on_a_symlink_leaves_its_target_alone(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(target)
    try:
        repo.remove_tree(str(link))                      # rmtree refuses a symlink; so did ignore_errors=True, quietly
        assert stat.S_IMODE(target.stat().st_mode) == 0o755 and link.is_symlink()
    finally:
        target.chmod(0o755)


@windows_only
def test_remove_tree_deletes_read_only_files_and_folders_on_windows(tmp_path):
    tree = tmp_path / "tree"
    folder = tree / "folder"
    folder.mkdir(parents=True)
    (folder / "f").write_text("x")
    (folder / "f").chmod(stat.S_IREAD)
    folder.chmod(stat.S_IREAD)
    repo.remove_tree(str(tree))
    assert not tree.exists()


@windows_only
def test_the_retry_skips_a_junction(tmp_path):
    import _winapi
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(target), str(junction))
    repo._retry_writable(os.unlink, str(junction), PermissionError())
    assert os.path.isjunction(junction) and target.exists()


def test_the_retry_is_only_for_a_refused_delete(tmp_path, monkeypatch):
    """The Windows branch, on any OS: CI runs Linux only."""
    monkeypatch.setattr(repo, "_WINDOWS", True)
    f = tmp_path / "f"
    f.write_text("x")
    repo._retry_writable(os.open, str(f), PermissionError())   # another call: its one-argument retry would raise
    repo._retry_writable(os.unlink, str(f), OSError())         # another failure
    assert f.exists()
    repo._retry_writable(os.unlink, str(f), PermissionError())
    assert not f.exists()


def test_the_retry_skips_a_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "_WINDOWS", True)
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    repo._retry_writable(os.unlink, str(link), PermissionError())
    assert link.is_symlink()
