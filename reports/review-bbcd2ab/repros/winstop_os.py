"""Windows only. The OS behaviour behind the Stop finding, without Orion: Ctrl-Break to a child's process group
ends a Python child at once (no `finally` runs), and a grandchild in a process group of its own (as
runtime/http_driver.HttpDriver.start makes the target on Windows) keeps running.

    python reports/review-bbcd2ab/repros/winstop_os.py

The script plays both roles: run with no argument it is the parent (the UI); `--child DIR` is the scan.
"""
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if len(sys.argv) > 2 and sys.argv[1] == "--child":
    marks = Path(sys.argv[2])
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                              creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    print(target.pid, flush=True)
    try:
        time.sleep(60)
    finally:
        (marks / "finally.txt").write_text("ran")
        target.kill()
    sys.exit(0)

marks = Path(tempfile.mkdtemp(prefix="winstop-"))
child = subprocess.Popen([sys.executable, __file__, "--child", str(marks)], stdout=subprocess.PIPE, text=True,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
target_pid = int(child.stdout.readline().strip())
print("scan pid", child.pid, "target pid", target_pid)
time.sleep(1.0)
try:
    child.send_signal(signal.CTRL_BREAK_EVENT)          # what jobs._send_stop sends on the first Stop
    print("Ctrl-Break sent")
except OSError as exc:
    print("Ctrl-Break could not be sent here (no shared console):", exc)
    child.kill()
rc = child.wait(20)
print("scan exit code", hex(rc & 0xFFFFFFFF), "| its finally ran:", (marks / "finally.txt").exists())
time.sleep(1.0)
alive = subprocess.run(["tasklist", "/FI", f"PID eq {target_pid}", "/NH"], capture_output=True, text=True).stdout
print("target still running after the scan exited:", str(target_pid) in alive)
subprocess.run(["taskkill", "/PID", str(target_pid), "/F"], capture_output=True)      # clean up
