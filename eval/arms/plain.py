import json
import os
import shutil
import subprocess
from pathlib import Path

from . import ArmResult
from ..usage import normalize_usage
from .env import codex_env, ensure_codex_home, plain_claude_env
from .launch import stream_subprocess
from .prompt import TASK_PROMPT, case_prompt, validate_findings

# The event type that carries a model-call usage summary, per CLI.
_USAGE_EVENT = {"codex": "turn.completed", "claude": "result"}

_HERE = Path(__file__).resolve().parent
# Sandbox settings for the Claude arms (Bash network lockout); see the file's _comment.
CLAUDE_SETTINGS = str(_HERE / "claude_settings.json")
# Clean config homes (CLAUDE_CONFIG_DIR / CODEX_HOME) live here, gitignored; they hold only a
# copy of the login (eval/README.md, one-time step), never the operator's plugins/settings.
CONFIG_ROOT = str(_HERE.parent / ".config")


def config_home(arm: str, root: str = CONFIG_ROOT) -> str:
    return os.path.join(root, "codex" if arm == "plain-gpt" else "claude")


# Everything the Claude arms may call: a code audit needs a shell and the file tools, nothing
# else. --tools is an ALLOWLIST (anything not named is absent, including built-ins added by a
# future CLI version); --disallowedTools is the belt to that brace, naming every built-in from
# the 2.1.302 init tool list that reaches the network or spawns agents outside the Bash sandbox.
CLAUDE_TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"]
CLAUDE_DISALLOWED = ["WebFetch", "WebSearch", "Task", "Agent", "ListAgents", "SendMessage",
                     "TaskStop", "RemoteTrigger", "DesignSync", "PushNotification", "Workflow",
                     "CronCreate", "CronList", "CronDelete", "ScheduleWakeup", "Monitor",
                     "EnterWorktree", "ExitWorktree", "ReportFindings", "Skill", "ToolSearch"]


def _build_cmd(arm: str, repo_dir: str, model_tag: str, effort: str,
               prompt: str = TASK_PROMPT) -> list[str]:
    """The frontier arm's command line. Flags verified on claude 2.1.302 / codex-cli 0.155.1
    and smoke-tested for the network lockout on 2026-10-18 (eval/README.md). `prompt` is
    TASK_PROMPT (whole repo) or an OWASP case's CASE_PROMPT (one file, Task F2)."""
    if arm == "plain-gpt":
        # Codex: non-interactive, JSON events (carry token usage), cwd = repo.
        # workspace-write + network_access=false: the agent can write findings.json into the
        # tree but its shell has no network (read-only also blocks the write -- measured).
        # Plugins/apps are off because account-side remote plugins and the `codex_apps` MCP
        # server load even under a clean CODEX_HOME; web_search is Codex's built-in search
        # tool, which runs OUTSIDE the shell sandbox (measured 2026-10-18: it searched the web
        # with the sandbox on). Both are answer-lookup channels.
        cmd = ["codex", "exec", "--json", "-m", model_tag]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        cmd += ["--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=false",
                "--disable", "plugins", "--disable", "remote_plugin", "--disable", "apps",
                "-c", "web_search=disabled",
                "-c", "approval_policy=never",  # non-interactive: nothing can wait on a prompt
                "--skip-git-repo-check", "--cd", repo_dir, prompt]
        return cmd
    # Claude Code arms (plain-sonnet5 / plain-opus5): pinned model + effort; no user/project/
    # local settings, no MCP servers, no web tools; Bash sandboxed with an empty network
    # allowlist (claude_settings.json).
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", model_tag]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            "--tools", ",".join(CLAUDE_TOOLS),
            "--disallowedTools", *CLAUDE_DISALLOWED,
            "--settings", CLAUDE_SETTINGS,
            "--add-dir", repo_dir, "--permission-mode", "bypassPermissions"]
    return cmd


def _usage_source(arm: str) -> str:
    return "codex" if arm == "plain-gpt" else "claude"


def read_usage(usage_log: str, *, source: str, on_line=None) -> list[dict]:
    """Normalized usage blocks from a run's usage.jsonl: one per usage-carrying event.
    Missing file / no events -> [] (nothing measured, nothing invented). A usage event that
    fails to parse, or a non-empty log with no usage event at all, is reported through
    `on_line` so a token undercount never passes silently."""
    if not os.path.exists(usage_log):
        return []
    warn = on_line or (lambda _l: None)
    want = _USAGE_EVENT[source]
    marker = f'"type":"{want}"'
    blocks, nonempty = [], False
    per_message: dict[str, dict] = {}     # claude: message id -> its usage, for a run with no result
    with open(usage_log) as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            nonempty = True
            try:
                ev = json.loads(line)
            except ValueError:
                # the shim tees everything claude prints; plain text is noise, but a broken
                # usage event means this run's tokens are lost -> say so
                if marker in line.replace(" ", ""):
                    warn(f"warning: usage.jsonl line {n} looks like a {want} event but failed "
                         "to parse; tokens for this call are missing")
                continue
            if isinstance(ev, dict) and ev.get("type") == want:
                blocks.append(normalize_usage(ev, source=source))
            elif source == "claude" and isinstance(ev, dict) and ev.get("type") == "assistant":
                _keep_message_usage(per_message, ev)
    if not blocks and per_message:
        # No `result` event (a hung run killed by the watchdog, a crash): the session's usage is the
        # sum of what the CLI reported per assistant message -- measured, not estimated, but the
        # message in flight when it died may be short, so it is a LOWER BOUND and labelled as one.
        usage = {k: sum(m.get(k) or 0 for m in per_message.values())
                 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                           "output_tokens")}
        block = normalize_usage({"usage": usage}, source="claude")
        block["exit_reason"] = PER_MESSAGE
        blocks.append(block)
        warn(f"warning: usage.jsonl has no {want} event; tokens summed from {len(per_message)} "
             "assistant messages (a lower bound: the last message may be cut off)")
    elif nonempty and not blocks:
        warn(f"warning: usage.jsonl has no usage ({want}) event; tokens for this run are missing")
    return blocks


PER_MESSAGE = "per_message_sum"   # agent_calls.exit_reason of a usage block summed per message


def _keep_message_usage(per_message: dict, ev: dict) -> None:
    """Record one stream-json `assistant` event's usage under its message id. The CLI repeats a
    message once per content block, so each field keeps its highest value for that id (counts only
    grow while a message is written); a message with no id or no usage is skipped, never guessed."""
    msg = ev.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict) or not msg.get("id"):
        return
    seen = per_message.setdefault(str(msg["id"]), {})
    for k, v in msg["usage"].items():
        if isinstance(v, int) and not isinstance(v, bool):
            seen[k] = max(seen.get(k) or 0, v)


def _capture_findings(repo_dir: str, json_out: str, on_line) -> "str | None":
    """Copy the agent's findings.json out of the (soon-deleted) tree. Returns the per-run
    path when the file exists and validates; None (a miss) otherwise -- never raises."""
    src = os.path.join(repo_dir, "findings.json")
    if not os.path.exists(src):
        return None
    shutil.copy2(src, json_out)  # raw artifact is kept verbatim even if it fails validation
    try:
        obj = json.loads(Path(json_out).read_text())
    except ValueError as e:
        on_line(f"warning: findings.json is not valid JSON ({e}); scored as a miss")
        return None
    if not isinstance(obj, dict):
        on_line("warning: findings.json is not a JSON object; scored as a miss")
        return None
    items = obj.get("findings")
    if not isinstance(items, list):
        on_line("warning: findings.json has no 'findings' list; scored as a miss")
        return None
    # Each finding is checked ON ITS OWN (2026-10-29, eval/CHANGELOG.md): before, one bad finding
    # (say "function": null) voided the whole run -- a miss on the vulnerable commit and a "clean"
    # patched one. A finding that fails is dropped and counted; the valid ones are scored. The raw
    # file is kept verbatim next to the cleaned one (findings.raw.json).
    bad = [(i, p) for i, it in enumerate(items) if (p := validate_findings({"findings": [it]}))]
    if bad and len(bad) == len(items):
        detail = "; ".join(f"finding {i}: {p[0].split(' ', 2)[-1]}" for i, p in bad[:5])
        on_line(f"warning: findings.json: none of its {len(items)} findings passed the schema check "
                f"({detail}); scored as a miss")
        return None
    if bad:
        raw_out = os.path.join(os.path.dirname(json_out), "findings.raw.json")
        shutil.copy2(json_out, raw_out)
        dropped = {i for i, _ in bad}
        keep = [it for i, it in enumerate(items) if i not in dropped]
        Path(json_out).write_text(json.dumps({**obj, "findings": keep}, indent=2))
        detail = "; ".join(f"finding {i}: {p[0].split(' ', 2)[-1]}" for i, p in bad[:5])
        on_line(f"warning: findings.json: dropped {len(bad)} of {len(items)} findings that failed the "
                f"schema check ({detail}); the {len(keep)} valid ones are scored (raw kept in {raw_out})")
    return json_out


def plain_arm(*, arm, repo_dir, model_tag, effort, json_out, usage_log, shim_dir, base_env,
              on_line, popen=subprocess.Popen, on_start=None, config_root=CONFIG_ROOT,
              case=None) -> ArmResult:
    """One frontier-arm run. `case` (an OWASP case from eval/dataset/owasp_sample.json) switches the
    task to CASE_PROMPT for that case's file/entry/category; None is the whole-repo TASK_PROMPT."""
    os.makedirs(os.path.dirname(json_out), exist_ok=True)
    source = _usage_source(arm)
    prompt = case_prompt(case) if case else TASK_PROMPT
    cmd = _build_cmd(arm, repo_dir, model_tag, effort, prompt)
    home = config_home(arm, config_root)
    log = None
    try:
        if source == "codex":
            # Codex reports usage in its own --json stream; no claude shim. Nothing tees its
            # stdout either, so the arm keeps the raw event stream itself.
            env = codex_env(base_env, home=ensure_codex_home(home))
            os.makedirs(os.path.dirname(usage_log), exist_ok=True)
            log = open(usage_log, "w")

            def _on_line(line):
                log.write(line + "\n")
                log.flush()
                on_line(line)
        else:
            os.makedirs(home, exist_ok=True)
            env = plain_claude_env(base_env, usage_log=usage_log, shim_dir=shim_dir,
                                   config_dir=home)
            _on_line = on_line
        status = stream_subprocess(cmd, env=env, cwd=repo_dir, on_line=_on_line,
                                   on_start=on_start, popen=popen)
    finally:
        if log is not None:
            log.close()
    findings_path = _capture_findings(repo_dir, json_out, on_line)
    return ArmResult(status=status, findings_path=findings_path,
                     usage=read_usage(usage_log, source=source, on_line=on_line))
