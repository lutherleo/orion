"""Windows only. The first Stop through orion.ui.jobs itself: does anything the scan started in a process group
of its own (the runtime target, as HttpDriver.start makes it) outlive the scan? Token-free: the "scan" is this
script's child role, which starts a target and sleeps.

    python reports/review-bbcd2ab/repros/repro_winstop_jobs.py [ORION_CHECKOUT]

A stop that ends the whole tree leaves the target gone once the job reads "exited".
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == "--child":
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                              creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    print(f"target {target.pid}", flush=True)
    time.sleep(60)
    sys.exit(0)

checkout = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(checkout))
from orion.ui import jobs   # noqa: E402

runner = jobs.Jobs(cwd=Path(tempfile.mkdtemp(prefix="winstop-jobs-")))
job = runner.start([sys.executable, str(Path(__file__).resolve()), "--child"], dict(os.environ),
                   kind="scan", repo=None, engine="claude")
target_pid = None
deadline = time.monotonic() + 20
while target_pid is None and time.monotonic() < deadline:
    for line in job.output(0)[0]:
        if line.startswith("target "):
            target_pid = int(line.split()[1])
    time.sleep(0.1)
print("orion code:", Path(sys.modules["orion"].__file__).parent)
print("scan pid", job._proc.pid, "target pid", target_pid, "| in a job object:", getattr(job, "_tree", None) is not None)
time.sleep(1.0)
print("first Stop:", runner.stop()["signal"])
print("exited within 20 s:", job.wait(20), "| exit note:", job.public()["exit_note"])
time.sleep(1.0)
alive = subprocess.run(["tasklist", "/FI", f"PID eq {target_pid}", "/NH"], capture_output=True, text=True).stdout
print("target still running after the scan exited:", str(target_pid) in alive)
subprocess.run(["taskkill", "/PID", str(target_pid), "/F"], capture_output=True)      # clean up either way
