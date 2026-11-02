"""A run recorded WITHOUT adaptive verification / code context, resumed from an `orion ui` whose own environment
has ORION_VERIFY_ROUTING=1 and ORION_VERIFY_CONTEXT=1. Token-free: popen is faked, nothing runs.

    python reports/review-bbcd2ab/repros/repro_resume_env.py [ORION_CHECKOUT]

A resume that keeps the recorded settings leaves both off: no flag in argv AND both variables "0" (or absent) in
the child's environment.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

checkout = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(checkout))
from orion.ui import keys, server   # noqa: E402

cwd = Path(tempfile.mkdtemp(prefix="repro-resume-"))
run = cwd / ".orion" / "runs" / "s1" / "20261029T120000Z"
run.mkdir(parents=True)
(cwd / "repo").mkdir()
settings = {"verify_routing": False, "verify_context": False, "semantic_mode": "vector", "fail_on": "none",
            "endpoint": False}
(run / "leads.json").write_text(json.dumps({"scan_id": "s1", "repo": str(cwd / "repo"), "engine": "claude",
                                            "model": "sonnet", "settings": settings,
                                            "leads": [{"index": 0, "shape": "A", "text": "x", "evidence": "e",
                                                       "confidence": "HIGH"}]}), encoding="utf-8")
seen = {}


class FakeProc:
    """A child that has already exited, so the UI's shutdown has nothing to stop."""
    pid, returncode, stdout = 1, None, None

    def poll(self): return 0
    def send_signal(self, s): pass
    def kill(self): pass
    def wait(self, timeout=None): return 0


def popen(argv, **kw):
    seen["argv"], seen["env"] = argv, kw["env"]
    return FakeProc()


env = dict(os.environ, ORION_VERIFY_ROUTING="1", ORION_VERIFY_CONTEXT="1")
ui = server.make_server(port=0, cwd=cwd, keychain=keys.Keychain(backend=None), environ=env, popen=popen,
                        token="t", lm_available=False)
try:
    ui.resume("s1~20261029T120000Z")
finally:
    ui.shutdown(timeout=0.1)
print("orion code:", Path(sys.modules["orion"].__file__).parent)
print("recorded settings:", settings)
print("resume argv tail:", seen["argv"][3:])
print("child env ORION_VERIFY_ROUTING =", seen["env"].get("ORION_VERIFY_ROUTING"),
      "| ORION_VERIFY_CONTEXT =", seen["env"].get("ORION_VERIFY_CONTEXT"))
