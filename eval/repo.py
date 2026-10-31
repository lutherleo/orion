import os
import shutil
import stat
import subprocess


def _retry_writable(func, path, _exc) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_tree(path: str) -> None:
    """`shutil.rmtree(path, ignore_errors=True)` that also deletes read-only files. Git writes its pack
    files read-only, and on Windows rmtree cannot unlink those, so every cleared clone was left behind;
    on POSIX the result is the same as before. Never raises."""
    shutil.rmtree(path, onexc=_retry_writable)


def _default_run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def mirror(repo_url: str, mirror_dir: str, *, run=_default_run) -> None:
    """Reusable bare mirror (no working tree): clone once, fetch afterwards."""
    if os.path.isdir(mirror_dir):
        run(["git", "-C", mirror_dir, "fetch", "--all", "--tags", "--prune"])
    else:
        tmp = f"{mirror_dir}.tmp"
        remove_tree(tmp)  # leftover partial clone
        run(["git", "clone", "--bare", "--", repo_url, tmp])
        os.rename(tmp, mirror_dir)


def vulnerable_commit(fix_sha: str, mirror_dir: str, *, run=_default_run) -> str:
    return run(["git", "-C", mirror_dir, "rev-parse", f"{fix_sha}^"]).strip()


def export_tree(mirror_dir: str, sha: str, dest: str, *, run=_default_run) -> None:
    """Fresh working tree of `sha` with NO .git (so the fix commit is not reachable).

    Known limits: export-ignore files and submodules are not exported.
    """
    tar = f"{dest}.tar"
    remove_tree(dest)  # stale tree from an earlier attempt
    os.makedirs(dest, exist_ok=True)
    try:
        run(["git", "-C", mirror_dir, "archive", "--format=tar", "-o", tar, sha])
        run(["tar", "-xf", tar, "-C", dest])
    finally:
        if os.path.exists(tar):
            os.remove(tar)
