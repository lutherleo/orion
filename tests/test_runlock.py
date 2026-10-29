"""One scan per run dir (orion/runlock.py): a scan holds an OS lock on <run_dir>/.lock while it writes the dir,
and the OS drops it however the process ends -- finished or killed. Token-free; the cross-process cases run a
tiny Python child."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

from orion import runlock

HOLDER = textwrap.dedent("""
    import sys, time
    from orion import runlock
    handle = runlock.acquire(sys.argv[1])
    print("held" if handle is not None else "busy", flush=True)
    time.sleep(60)
""")


def test_held_while_a_handle_is_open_and_free_after(tmp_path):
    assert runlock.held(tmp_path) is False                       # no lock file yet: never created by held()
    assert not (tmp_path / runlock.LOCK_NAME).exists()
    handle = runlock.acquire(tmp_path)
    assert handle is not None and (tmp_path / runlock.LOCK_NAME).exists()
    assert runlock.held(tmp_path) is True
    assert runlock.acquire(tmp_path) is None                     # a second writer is refused
    handle.close()
    assert runlock.held(tmp_path) is False
    again = runlock.acquire(tmp_path)
    assert again is not None
    again.close()


def test_a_killed_scan_releases_its_lock(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert runlock.held(tmp_path) is True                    # another process is writing this run dir
        assert runlock.acquire(tmp_path) is None
    finally:
        child.kill()                                             # SIGKILL / TerminateProcess: no cleanup runs
        child.wait(30)
    deadline = time.monotonic() + 10
    while runlock.held(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runlock.held(tmp_path) is False
    handle = runlock.acquire(tmp_path)
    assert handle is not None
    handle.close()


def test_held_survives_an_unreadable_dir(tmp_path):
    assert runlock.held(tmp_path / "missing") is False


def test_held_needs_no_write_access(tmp_path):
    """The UI may read a run dir it cannot write: a live scan must still show as running."""
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        os.chmod(tmp_path / runlock.LOCK_NAME, 0o444)
        assert runlock.held(tmp_path) is True
    finally:
        child.kill()
        child.wait(30)
        os.chmod(tmp_path / runlock.LOCK_NAME, 0o644)


def test_a_dir_without_room_for_the_lock_file_never_blocks_a_scan(tmp_path):
    handle = runlock.acquire(tmp_path / "no-such-dir")       # cannot create the file: no crash, no refusal
    assert handle is not None
    handle.close()


def test_a_lock_file_this_user_cannot_write_still_locks(tmp_path):
    """A scan must not run unguarded beside a live one just because the lock file is not writable here:
    a read handle locks too."""
    lock = tmp_path / runlock.LOCK_NAME
    lock.write_bytes(b"")
    os.chmod(lock, 0o444)
    try:
        handle = runlock.acquire(tmp_path)
        assert handle is not None and not isinstance(handle, runlock._NoLock)
        assert runlock.acquire(tmp_path) is None                   # a second writer is still refused
        handle.close()
    finally:
        os.chmod(lock, 0o644)
