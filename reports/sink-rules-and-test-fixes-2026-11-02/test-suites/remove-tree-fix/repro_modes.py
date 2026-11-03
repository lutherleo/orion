"""What does a remove_tree do to modes on POSIX? Run with: python repro_modes.py <folder holding eval/repo.py>
(needs a non-root user). Prints every mode before and after, in a temp folder of its own."""
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, sys.argv[1])
from eval.repo import remove_tree


def mode(p):
    return oct(stat.S_IMODE(os.stat(p).st_mode))


base = tempfile.mkdtemp(prefix="rt_modes_")
outside = os.path.join(base, "outside_file")
open(outside, "w").close()
os.chmod(outside, 0o644)
outside_dir = os.path.join(base, "outside_dir")
os.mkdir(outside_dir)
os.chmod(outside_dir, 0o755)
tree = os.path.join(base, "tree")
ro = os.path.join(tree, "ro")
os.makedirs(ro)
inner = os.path.join(ro, "file")
open(inner, "w").close()
os.chmod(inner, 0o644)
os.symlink(outside, os.path.join(ro, "link"))
os.chmod(ro, 0o500)
os.chmod(tree, 0o700)
link = os.path.join(base, "link_to_dir")
os.symlink(outside_dir, link)

print("before: outside file", mode(outside), "| outside dir", mode(outside_dir), "| tree", mode(tree), "| ro", mode(ro),
      "| file in ro", mode(inner))
remove_tree(tree)
remove_tree(link)
tree_after = mode(tree)
os.chmod(tree, 0o700)                # to read what is inside, whatever it became
ro_after = mode(ro)
os.chmod(ro, 0o700)
print("after:  outside file", mode(outside), "| outside dir", mode(outside_dir), "| tree", tree_after, "| ro", ro_after,
      "| file in ro", mode(inner))
os.chmod(outside_dir, 0o700)
shutil.rmtree(base)
