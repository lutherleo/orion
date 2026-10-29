"""One scan per run dir: `orion scan` holds an exclusive OS lock on `<run_dir>/.lock` while it writes the dir.

The OS drops the lock when the process ends, however it ends (finished, Ctrl-C, crashed, killed), so a held lock
means a live scan and a free one means none. File times cannot tell: a run dir stays quiet for minutes while one
verifier session runs. `orion scan --resume` refuses a dir another scan holds, and `orion ui` shows such a run as
running (elsewhere) and will not resume it. The lock file stays behind, empty: it is only ever locked, never read.

Stdlib only: fcntl.flock on POSIX, msvcrt.locking (one byte at offset 0) on Windows. A filesystem that cannot lock
at all never blocks a scan: `acquire` then guards nothing and `held` says False.
"""
from __future__ import annotations

import errno
import os
import time
from pathlib import Path
from typing import BinaryIO

LOCK_NAME = ".lock"
_TRIES, _PAUSE = 10, 0.05     # acquire: ride out another process's brief `held` probe (it locks, then unlocks)

if os.name == "nt":
    import msvcrt

    def _try_lock(f: BinaryIO) -> bool:
        """True when locked now, False when another holder has it; OSError when this file cannot be locked."""
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True

    def _unlock(f: BinaryIO) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(f: BinaryIO) -> bool:
        """True when locked now, False when another holder has it; OSError when this file cannot be locked."""
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(f: BinaryIO) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def acquire(run_dir: str | os.PathLike) -> BinaryIO | None:
    """Lock `run_dir` for this process, creating the lock file. Returns the open handle -- the lock lasts as long
    as it stays open (closing it, or the process ending, releases it) -- or None when another process holds it."""
    f = open(Path(run_dir) / LOCK_NAME, "a+b")          # creates it; never truncates
    try:
        for attempt in range(_TRIES):
            if _try_lock(f):
                return f
            if attempt < _TRIES - 1:
                time.sleep(_PAUSE)
    except OSError:
        return f              # this filesystem cannot lock: the scan goes on unguarded
    f.close()
    return None


def held(run_dir: str | os.PathLike) -> bool:
    """True when another process holds `run_dir`'s lock right now. Never creates the lock file; a dir without
    one (written before scans took the lock) or one that cannot be read or locked is not provably held."""
    try:
        f = open(Path(run_dir) / LOCK_NAME, "r+b")
    except OSError:
        return False
    with f:
        try:
            if not _try_lock(f):
                return True
        except OSError:
            return False
        try:
            _unlock(f)
        except OSError:
            pass
        return False
