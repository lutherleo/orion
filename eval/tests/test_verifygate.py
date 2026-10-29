"""Gate 2 (merge plan Phase 6): the dev measurement of Orion-LM's opt-in verifier. Token-free, scripted model."""
import json

from eval import verifygate
from orion.lm import format as fmt
from orion.lm.model import ContextOverflow, GenResult
from train import devscore


def _case(code):
    return {"flow_id": "F0", "rank": 0, "sink_category": None, "source_uid": None, "sink_uid": None, "nodes": [],
            "snippets": [{"role": "target", "file": "a.py", "function": "f", "start": 1, "end": 2, "code": code}]}


LEAD = {"flow_id": "F0", "file": "a.py", "function": "f", "line": 1, "cwe": "CWE-89", "confidence": "HIGH"}


def _dev(n, *, negatives=0):
    rows = []
    for k in range(n):
        rows.append({"pair_id": f"P{k}", "label": "vulnerable", "case": _case(f"vuln {k}"), "lead": LEAD,
                     "split": "dev"})
        rows.append({"pair_id": f"P{k}", "label": "patched", "case": _case(f"safe {k}"), "lead": None, "split": "dev"})
    for k in range(negatives):
        rows.append({"pair_id": f"N{k}", "label": "negative", "case": _case(f"other {k}"), "lead": None, "split": "dev"})
    return rows


def _reply(obj):
    return GenResult(text=obj if isinstance(obj, str) else json.dumps(obj), prompt_tokens=10, completion_tokens=2,
                     cached_tokens=0)


class Model:
    """Discovery flags every row; the verifier answers by `verify_rule(task_text)`."""

    def __init__(self, verify_rule):
        self.verify_rule = verify_rule
        self.seeds = 0
        self.sampling = {"temperature": 0.0, "top_p": 0.95, "top_k": 0}
        self.tasks = []

    def seed_session(self):
        self.seeds += 1

    def generate(self, messages):
        task = messages[1]["content"]
        self.tasks.append(task)
        if task.startswith("TASK: An earlier pass flagged"):
            return self.verify_rule(task)
        return _reply({"action": "final", "leads": [LEAD]})


def _confirm_vulns_reject_rest(task):
    if "vuln " in task:
        return _reply({"action": "final", "leads": [LEAD]})
    return _reply({"action": "final", "leads": []})


def test_a_verifier_that_keeps_every_vuln_and_drops_every_false_alarm_is_default_on():
    m = Model(_confirm_vulns_reject_rest)
    out = verifygate.run(m, _dev(30, negatives=6), sample_n=200)
    assert out["n"] == 66 and out["n_pairs"] == 36
    assert out["discovery"]["vuln_recall"] == 1.0 and out["discovery"]["patched_silence"] == 0.0
    assert out["verified"]["vuln_recall"] == 1.0 and out["verified"]["nonvuln_silence"] == 1.0
    assert out["verifier_on_real_vulns"] == {"CONFIRM": 30, "INCONCLUSIVE": 0, "REJECT": 0, "ERROR": 0,
                                             "found": 30, "lost": 0, "lost_rate": 0.0}
    assert out["verifier_on_false_alarms"]["REJECT"] == 36 and out["verifier_on_false_alarms"]["removed_rate"] == 1.0
    assert out["decision"] == "DEFAULT_ON"
    assert m.seeds == 66 * 2                     # re-seeded before every session, discovery and verify
    assert out["tokens"]["discovery"] == 66 * 12 and out["tokens"]["verify"] == 66 * 12
    assert "Gate 2: --lm-verify on" in verifygate.text({**out, "model": "m"})


def test_a_verifier_that_rejects_real_vulns_stays_opt_in():
    def rejects_some(task):
        if any(f"vuln {k}\n" in task or task.rstrip().endswith(f"vuln {k}") for k in range(3)):
            return _reply({"action": "final", "leads": []})       # 3 of 30 real vulns rejected: 10% lost
        return _confirm_vulns_reject_rest(task)

    out = verifygate.run(Model(rejects_some), _dev(30), sample_n=200)
    assert out["verifier_on_real_vulns"]["lost"] == 3 and out["decision"] == "KEEP_OPT_IN"
    assert abs(out["diff"]["vuln_recall"] + 0.1) < 1e-9


def test_verifier_messages_and_decision_mapping():
    def low_then_overflow(task):
        if "vuln 0" in task:
            return _reply({"action": "final", "leads": [dict(LEAD, confidence="LOW")]})   # -> INCONCLUSIVE
        raise ContextOverflow(9000, 8192)                                                # -> ERROR

    m = Model(low_then_overflow)
    out = verifygate.run(m, _dev(1), sample_n=10)
    rows = {r["label"]: r for r in out["rows"]}
    assert rows["vulnerable"]["decisions"] == ["INCONCLUSIVE"] and rows["patched"]["decisions"] == ["ERROR"]
    assert out["verifier_on_real_vulns"]["lost"] == 1 and out["decision"] == "INCONCLUSIVE"   # one pair: no CI
    # discovery asks the trained format; the verifier keeps the system turn and swaps only the TASK turn
    disc = devscore.messages_for(_dev(1)[1])          # sample_dev puts a pair's patched row first
    assert m.tasks[0] == disc[1]["content"]
    assert m.tasks[1].startswith("TASK: An earlier pass flagged the candidate below as a real vulnerability "
                                 "(CWE-89 at a.py:1 in f)")
    assert fmt.system_message()["content"]


def test_unflagged_rows_are_never_verified():
    class Quiet(Model):
        def generate(self, messages):
            self.tasks.append(messages[1]["content"])
            return _reply({"action": "final", "leads": []})

    m = Quiet(None)
    out = verifygate.run(m, _dev(4), sample_n=20)
    assert len(m.tasks) == 8 and out["verifier_on_real_vulns"]["found"] == 0 and out["decision"] == "INCONCLUSIVE"
