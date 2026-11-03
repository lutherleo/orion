"""Does PR #5's eval.repo.remove_tree keep the old rmtree(ignore_errors=True) promise on POSIX?
Run from the repo root as: python <this file> (needs a non-root user; chmod 000 means nothing to root)."""
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.getcwd())
from eval.repo import remove_tree

print("platform", sys.platform, "uid", os.getuid() if hasattr(os, "getuid") else None)

# 1. An unreadable subfolder: rmtree reports os.open, which _retry_writable then calls with one argument.
root = tempfile.mkdtemp(prefix="rt_repro_")
sub = os.path.join(root, "locked")
os.mkdir(sub)
open(os.path.join(sub, "f"), "w").close()
os.chmod(sub, 0)
try:
    remove_tree(root)
    print("1. unreadable subfolder: no exception; root still there:", os.path.exists(root))
except BaseException as exc:
    print("1. unreadable subfolder: remove_tree RAISED", type(exc).__name__, exc)
finally:
    if os.path.exists(sub):
        os.chmod(sub, 0o700)
    shutil.rmtree(root, ignore_errors=True)

# 2. A symlink in a read-only folder: unlink is refused, then chmod follows the link to a file outside the tree.
outside = tempfile.NamedTemporaryFile(prefix="rt_outside_", delete=False)
outside.close()
os.chmod(outside.name, 0o644)
root = tempfile.mkdtemp(prefix="rt_repro_")
ro = os.path.join(root, "ro")
os.mkdir(ro)
os.symlink(outside.name, os.path.join(ro, "link"))
os.chmod(ro, 0o500)
try:
    remove_tree(root)
except BaseException as exc:
    print("2. raised", type(exc).__name__, exc)
print("2. target outside the tree: mode before 0o644, after", oct(stat.S_IMODE(os.stat(outside.name).st_mode)))
os.chmod(ro, 0o700)
shutil.rmtree(root, ignore_errors=True)
os.unlink(outside.name)
