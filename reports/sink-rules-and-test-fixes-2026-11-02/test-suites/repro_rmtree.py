"""Does shutil.rmtree remove a real git clone on this OS? (train/snapshot's cleanup uses rmtree.)"""
import os
import shutil
import subprocess
import sys
import tempfile

root = tempfile.mkdtemp(prefix="rmtree_repro_")
src, dst = os.path.join(root, "src"), os.path.join(root, "dst")
env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
           GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
os.makedirs(src)
run = lambda *a: subprocess.run(a, check=True, capture_output=True, env=env)
run("git", "-C", src, "init", "-q")
with open(os.path.join(src, "a.txt"), "w") as f:
    f.write("x\n")
run("git", "-C", src, "add", "a.txt")
run("git", "-C", src, "commit", "-q", "-m", "c")
run("git", "clone", "-q", src, dst)

readonly = [os.path.join(d, n) for d, _, fs in os.walk(dst) for n in fs
            if not os.access(os.path.join(d, n), os.W_OK)]
print(sys.platform, "read-only files in the clone:", len(readonly))
shutil.rmtree(dst, ignore_errors=True)
print("after rmtree(ignore_errors=True), clone still there:", os.path.exists(dst))
try:
    if os.path.exists(dst):
        shutil.rmtree(dst)
except OSError as exc:
    print("plain rmtree raises:", type(exc).__name__, exc)
