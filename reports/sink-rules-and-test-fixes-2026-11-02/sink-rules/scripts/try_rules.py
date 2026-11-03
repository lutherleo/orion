"""Candidate sink rules vs the pickled batches: counts per category, plus which flows each rule drops and
whether the real sink inside a dropped wrapper keeps a flow of its own. Neo4j-free, token-free."""
import collections
import json
import pickle
import re
import sys
from pathlib import Path

from orion.graph import pathfind, profiles, schema

d = Path(sys.argv[1])
detail = len(sys.argv) > 2 and sys.argv[2] == "detail"

LOG_RX = re.compile(r"(?<![A-Za-z\d_$])LOG(?:GER)?(?![A-Za-z\d_$])"
                    r"|(?:(?<![A-Za-z\d_$])log|(?<![A-Z])Log)(?:ger|ging)?(?!In\b|Out\b|On\b)(?![a-z\d_$])")
POPEN_RX = re.compile(r"^(?:[\w.]+\.)?[Pp]open\s*\(")
SHELL_RX = re.compile(r"^(?:[\w.]*subprocess\.)(?:run|call|check_call|check_output)\s*\(")
SHELL_TRUE = re.compile(r"\bshell\s*=\s*True\b")
FN_LITERAL = re.compile(r"=>|\bfunction\b\s*[\w$]*\s*\(|\blambda\b")


def _own_text(raw_name: str, raw_code: str) -> tuple[str, str]:
    if raw_name.startswith("<operator>"):
        m = FN_LITERAL.search(raw_code)
        return "", raw_code[:m.start()] if m else raw_code
    head = raw_code.split("(", 1)
    return raw_name, head[0] + "(" if len(head) == 2 else raw_code


def classify(props, profile, own=False, wrappers=False, log=False, popen=False):
    if profile is None:
        return None
    raw_name = str(props.get("name") or "")
    raw_code = str(props.get("code") or "")
    is_op = raw_name.startswith("<operator>")
    if popen and profile.name.startswith("generic") and not is_op:
        if POPEN_RX.search(raw_code) or (SHELL_RX.search(raw_code) and SHELL_TRUE.search(raw_code)):
            return "code_exec"
    name_txt, code_txt = ("" if is_op else raw_name), raw_code
    if own and not is_op:
        name_txt, code_txt = _own_text(raw_name, raw_code)
    if wrappers and is_op:
        name_txt, code_txt = _own_text(raw_name, raw_code)
    best, best_sev = None, -1
    for cat, hints in profile.sink_hints.items():
        for hint in hints:
            rx = getattr(hint, "rx", None)
            if rx is not None:
                hit = bool(rx.search(name_txt) or rx.search(code_txt))
            elif log and cat == "log":
                hit = bool(LOG_RX.search(name_txt) or LOG_RX.search(code_txt))
            else:
                h = hint.lower()
                hit = bool(h) and (h in name_txt.lower() or h in code_txt.lower())
            if hit:
                sev = pathfind._severity(cat)
                if sev > best_sev:
                    best, best_sev = cat, sev
                break
    return best


RULES = {
    "base": dict(),
    "+popen": dict(popen=True),
    "+log": dict(popen=True, log=True),
    "+callee": dict(popen=True, log=True, own=True),
    "+wrappers": dict(popen=True, log=True, own=True, wrappers=True),
}


def run(fx, prof, **kw):
    data = pickle.loads((d / f"{fx}.batch.pkl").read_bytes())
    b = schema.Batch("golden")
    for l, p in data["nodes"]:
        b.emit_node(l, p)
    b.edges.extend(data["edges"])
    orig = pathfind._classify_sink
    pathfind._classify_sink = lambda p, pr: classify(p, pr, **kw)
    try:
        s = pathfind.pathfind(b, prof)
    finally:
        pathfind._classify_sink = orig
    calls = {p["uid"]: p for l, p in b.nodes if l == "CpgCall"}
    rows = [dict(p, _c=calls[p["sink_uid"]]) for l, p in b.nodes if l == "CandidateFlow"]
    return s, sorted(rows, key=lambda r: r["rank"]), calls


FX = {"nodegoat": profiles.EXPRESS, "pygoat": profiles.GENERIC}
ORDER = ["code_exec", "deserialize", "sql", "nosql", "ssrf", "template", "xxe", "prototype_pollution", "path",
         "redirect", "render", "log"]
for fx, base in FX.items():
    for var, prof in (("default", base), ("lm", profiles.with_lm_sinks(base))):
        print(f"\n== {fx} {var}")
        print("rule".ljust(10), "total".rjust(5), *[c[:6].rjust(6) for c in ORDER])
        for rn, kw in RULES.items():
            s, rows, _ = run(fx, prof, **kw)
            cnt = collections.Counter(r["sink_category"] for r in rows)
            print(rn.ljust(10), str(len(rows)).rjust(5), *[str(cnt.get(c, 0) or "-").rjust(6) for c in ORDER])

def enclosing(rows):
    """(file, first, last, category) of every multi-line operator wrapper in `rows`."""
    out = []
    for r in rows:
        c = r["_c"]
        code = str(c.get("code") or "")
        if str(c.get("name")).startswith("<operator>") and "\n" in code:
            out.append((c.get("file_path"), c.get("line") or 0, (c.get("line") or 0) + code.count("\n"), r["sink_category"]))
    return out


for fx, base in FX.items():
    _, a_before, _ = run(fx, base, **RULES["+log"])
    _, a_after, _ = run(fx, base, **RULES["+callee"])
    _, b_after, _ = run(fx, base, **RULES["+wrappers"])
    a_kept = {r["sink_uid"] for r in a_after}
    b_kept = {r["sink_uid"] for r in b_after}
    wrappers = enclosing(a_after)

    def own_flow(r, kept_rows):
        c = r["_c"]
        line = c.get("line") or 0
        last = line + str(c.get("code") or "").count("\n")
        return any(k["_c"].get("file_path") == c.get("file_path") and k["sink_uid"] != r["sink_uid"]
                   and line <= (k["_c"].get("line") or 0) <= last and k["sink_category"] == r["sink_category"]
                   for k in kept_rows)

    dropped_a = [r for r in a_before if r["sink_uid"] not in a_kept]
    no_inner = [r for r in dropped_a if not own_flow(r, a_after)]
    print(f"\n-- {fx}: outer-call rule drops {len(dropped_a)}; no flow of their own inside: {len(no_inner)}")
    for r in no_inner:
        c = r["_c"]
        cover = [w for w in wrappers if w[0] == c.get("file_path") and w[1] <= (c.get("line") or 0) <= w[2]]
        code = str(c.get("code") or "")
        print(f'   {c.get("file_path")}:{c.get("line")} {r["sink_category"]} multi={"\n" in code} enclosing-wrapper={[(w[1], w[3]) for w in cover]}'
              f' | {code.splitlines()[0][:70]}')
    dropped_b = [r for r in a_after if r["sink_uid"] not in b_kept]
    no_inner_b = [r for r in dropped_b if not own_flow(r, b_after)]
    print(f"-- {fx}: wrapper rule drops {len(dropped_b)} more; no flow of their own inside: {len(no_inner_b)}")
    print(f"-- {fx}: top flows under the outer-call rule")
    for r in a_after[:14]:
        c = r["_c"]
        print(f'   {r["rank"]:>3} {r["sink_category"]:<10}{c.get("file_path")}:{c.get("line")} {str(c.get("name"))[:22]:<22} | {str(c.get("code")).splitlines()[0][:70]}')
    log_rows = [r for r in a_after if r["sink_category"] == "log"]
    print(f"-- {fx}: log rows left {len(log_rows)}: "
          f'real={sum(1 for r in log_rows if r["_c"].get("name") == "log")} '
          f'field={sum(1 for r in log_rows if r["_c"].get("name") == "<operator>.fieldAccess")} '
          f'wrappers={sum(1 for r in log_rows if "\n" in str(r["_c"].get("code") or ""))}')

if detail:
    for fx, base in FX.items():
        _, before, calls = run(fx, base)
        _, after, _ = run(fx, base, **RULES["+wrappers"])
        kept = {r["sink_uid"]: r["sink_category"] for r in after}
        prev = {r["sink_uid"]: r["sink_category"] for r in before}
        print(f"\n===== {fx} default: dropped/changed/added")
        kept_lines = collections.defaultdict(set)
        for r in after:
            c = r["_c"]
            kept_lines[c.get("file_path")].add((c.get("line"), r["sink_category"]))
        for r in before:
            c = r["_c"]
            uid = r["sink_uid"]
            if kept.get(uid) == r["sink_category"]:
                continue
            code = str(c.get("code") or "")
            line = c.get("line") or 0
            span = code.count("\n")
            inner = sorted(x for x in kept_lines[c.get("file_path")]
                           if line <= (x[0] or 0) <= line + span and x[1] == r["sink_category"])
            state = "changed->" + str(kept[uid]) if uid in kept else "dropped"
            multi = "wrapper" if span else "oneline"
            print(f'{state:<20}{r["sink_category"]:<10}{c.get("file_path")}:{line} {multi} '
                  f'name={str(c.get("name"))[:22]} own-flows-inside={[x[0] for x in inner]} | {code.splitlines()[0][:70] if code else ""}')
        for r in after:
            if r["sink_uid"] not in prev:
                c = r["_c"]
                print(f'added               {r["sink_category"]:<10}{c.get("file_path")}:{c.get("line")} rank={r["rank"]} '
                      f'name={c.get("name")} | {str(c.get("code")).splitlines()[0][:90]}')
