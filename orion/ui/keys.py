"""Model access for the scans `orion ui` starts: the credential the NEXT scan's child process gets, where a
bring-your-own key is kept, and the scrubber that keeps it out of everything the UI shows or streams.

One mode at a time (one model per scan: discovery and the verifier share it):
  login          nothing added: `claude -p` uses its own login.
  anthropic_key  ANTHROPIC_API_KEY=<key>: scans bill that API account instead of the login.
  endpoint       (experimental) ANTHROPIC_BASE_URL + the token, as ANTHROPIC_AUTH_TOKEN ("bearer") or
                 ANTHROPIC_API_KEY ("x-api-key") -- the placeholder NO_TOKEN when the endpoint needs none, since
                 with no credential claude would fall back to its own login and send THAT to the endpoint --
                 ORION_MODEL=<model>, and every model alias claude reads
                 (ALIASES) pinned to that same model, so fp-check's nested sub-agents cannot escape to Anthropic's
                 default models (the open-weight study's wiring); experimental beta headers are turned off for
                 gateways that only speak the stable API. Unproven until a NodeGoat scan passes through one.
`child_env` first removes every ACCESS_VARS name from the inherited environment (in the key and endpoint modes the
login's CLAUDE_CODE_OAUTH_TOKEN too, and the CLAUDE_CODE_USE_BEDROCK / _VERTEX / _FOUNDRY switches, which would send
the scan to that provider whatever key or URL is set), case-insensitively (a Windows env block is), so a variable
the server happened to start with can never override, or ride along with, the mode picked on the page. A secret
goes only into that env, never into argv (visible to `ps`).

Kept in the OS keychain through `keyring` (service "orion-ui": Windows Credential Manager, macOS Keychain, Linux
Secret Service), never in a file: keyring's fail/null backends (all WSL has), any `keyrings.alt` backend (plaintext
or encrypted files), a priority <= 0, or a chainer holding one of these is refused (`backend_ok`), and the setting
is then held in this process's memory only, which `reason` says plainly. A keychain error never reaches the caller:
the operation falls back to memory and `reason` says what failed. A key already in the server's environment is used
(source "env") and never copied into the keychain. Saving one mode deletes the other modes' secrets, and a refused
write deletes the older saved value, so the keychain never holds a secret the page does not show and a restart
cannot bring an old key back.

What leaves this module: `public()` -- mode, source, a hint ("...abcd", only for secrets of 12+ characters), where
it is kept, notes; never a secret, and scrubbed once more as a last guard -- and `child_env()`, for the child only.
`scrub` redacts every held secret of MIN_SECRET_LEN+ characters in one longest-first pass, JSON-escaped forms
included, because the server scrubs lines it has already encoded as JSON. Shorter "secrets" are left alone: Ollama's
dummy token "ollama" would otherwise vanish from every log line. AccessError messages never echo what was typed: a
pasted URL can carry a password, and a key pasted into the wrong field is still a key.

Standard library only; `keyring` is imported lazily, so the UI still runs (memory only) where it is not installed.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import re
import threading
import unicodedata
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

SERVICE = "orion-ui"                       # keyring service name
MODES = ("login", "anthropic_key", "endpoint")
MIN_SECRET_LEN = 8                         # shorter "secrets" (e.g. Ollama's dummy token "ollama") are not scrubbed
ALIASES = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
           "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")
ACCESS_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *ALIASES,
               "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS")
# The claude login's own credential, dropped from the env in the key and endpoint modes (it is the login).
LOGIN_VARS = ("CLAUDE_CODE_OAUTH_TOKEN",)
# Switches that send claude to another provider (Amazon Bedrock, Google Vertex, Microsoft Foundry) whatever key or
# URL is set: dropped in the key and endpoint modes. In login mode they are the claude CLI's own setup and stay.
PROVIDER_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# Sent as the endpoint's credential when it needs no token. With none set at all, claude falls back to its
# own login -- and would send that login's token to the endpoint.
NO_TOKEN = "none"
AUTHS = ("bearer", "x-api-key")            # how an endpoint token is sent
KEY_MAX, TOKEN_MAX, URL_MAX = 512, 4096, 2048
HINT_MIN_LEN = 12                          # a hint (the last 4 characters) only for secrets at least this long
REDACTED = "[redacted]"

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@-]{1,120}$")
# Keychain items: keyring "usernames" under SERVICE. The settings item is JSON and holds no secret; its
# "token_set" flag tells "this endpoint needs no token" apart from "its token went missing from the keychain".
_SETTINGS, _KEY, _TOKEN = "access", "anthropic_api_key", "endpoint_token"
_LABELS = {_SETTINGS: "the saved access setting", _KEY: "the Anthropic key", _TOKEN: "the endpoint token"}
_CHAINER = ("keyring.backends.chainer", "ChainerBackend")
_NO_KEYCHAIN_MODULES = ("keyring.backends.fail", "keyring.backends.null")
_FILE_MODULES = ("keyrings.alt",)
_DEFAULT = object()                        # Keychain(): ask keyring for this machine's backend


class AccessError(ValueError):
    """A model-access setting is refused. The message is for the page and never echoes what was typed."""


# ── keychain ─────────────────────────────────────────────────────────────────────────────────────────

def _class_name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__name__}"


def _full_name(backend: Any) -> str:
    return "none" if backend is None else _class_name(type(backend))


def backend_ok(backend: Any) -> tuple[bool, str]:
    """(usable, reason) for a keyring backend; reason is "" when usable. Only a real OS keychain is usable: None,
    keyring's fail/null backends, any keyrings.alt backend, a class named like Plaintext/Null/Fail (its own name
    or a base class's: a subclass of a file backend is still one), and a priority that is not > 0 or raises are
    refused. A ChainerBackend is usable iff it chains at least one backend and every one of them is: it reads
    from all of them and writes to the first that takes the value, so one file backend in the chain is enough to
    put a key on disk."""
    return _backend_ok(backend, depth=0)


def _backend_ok(backend: Any, depth: int) -> tuple[bool, str]:
    if backend is None:
        return False, "no keychain backend on this machine"
    name = _full_name(backend)
    mro = type(backend).__mro__
    if any((cls.__module__, cls.__name__) == _CHAINER for cls in mro):
        try:
            members = list(backend.backends)
        except Exception as exc:  # noqa: BLE001 -- a third-party property: any failure means "not usable"
            return False, f"keychain backend not usable ({name}: {type(exc).__name__})"
        if not members:
            return False, f"no keychain backend on this machine ({name} with no backends)"
        if depth >= 3:
            return False, f"keychain backend not usable ({name} nested too deeply)"
        for member in members:
            ok, why = _backend_ok(member, depth + 1)
            if not ok:
                return False, f"{why}, chained in {name}"
        return True, ""                    # a chainer's own priority means nothing until it has members
    for cls in mro:
        module, lowered = cls.__module__ or "", cls.__name__.lower()
        via = "" if cls is type(backend) else f", a {_class_name(cls)}"
        if "plaintext" in lowered:
            return False, f"plaintext backend refused ({name}{via})"
        if module.startswith(_FILE_MODULES):
            return False, f"keyrings.alt backend refused ({name}{via})"
        if module.startswith(_NO_KEYCHAIN_MODULES) or "null" in lowered or "fail" in lowered:
            return False, f"no keychain backend on this machine ({name}{via})"
    try:
        priority = backend.priority
    except Exception as exc:  # noqa: BLE001 -- keyring backends raise here to say "not on this platform"
        return False, f"keychain backend not usable ({name}: {type(exc).__name__})"
    number = isinstance(priority, (int, float)) and not isinstance(priority, bool)
    if not (number and priority > 0):     # NaN fails `> 0` too
        shown = f"priority {priority:g}" if number else "no numeric priority"
        return False, f"keychain backend not usable ({name}, {shown})"
    return True, ""


def _describe(exc: BaseException, secret: str | None = None) -> str:
    """`Type: message` for a keychain error, safe to show: the value being written is cut out (a backend may echo
    it), control characters dropped, whitespace collapsed, at most 200 characters of message."""
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 -- an exception whose __str__ fails still gets a reason
        text = ""
    if secret and len(secret) >= MIN_SECRET_LEN:
        text = text.replace(secret, REDACTED)
    text = " ".join("".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in text).split())
    if len(text) > 200:
        text = text[:200] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _system_backend() -> tuple[Any, str]:
    """keyring's own pick for this machine, imported lazily (keyring is optional), and why there is none."""
    try:
        import keyring
    except ImportError:
        return None, "keyring is not installed"
    except Exception as exc:  # noqa: BLE001 -- a broken install must not stop the UI from starting
        return None, f"keyring could not load ({_describe(exc)})"
    try:
        return keyring.get_keyring(), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"the keychain could not start ({_describe(exc)})"


def _is_missing(exc: BaseException) -> bool:
    """keyring.errors.PasswordDeleteError, matched by name so the check needs no keyring import here."""
    return any(cls.__name__ == "PasswordDeleteError" for cls in type(exc).__mro__)


def _label(name: str) -> str:
    return _LABELS.get(name, f"the item {name!r}")


class Keychain:
    """The OS keychain when a real backend exists (`backend_ok`); otherwise memory only, said plainly in `reason`.

    Every value is also held in memory, so a refused or failed keychain write still leaves the setting usable until
    `orion ui` stops. No keychain error reaches the caller (a backend raises whatever its platform raises: keyring's
    KeyringError, OSError, pywin32's own error type, D-Bus errors), so get / set / delete catch everything and record
    why in `reason`. A recorded failure is cleared once a write or delete of that item succeeds (a read clears only a
    read failure: reading an item back does not undo a failed delete). `stored(name)` says whether the item's current
    value is in the OS keychain. Readers of `reason` / `stored` never wait on a slow keychain call."""

    def __init__(self, backend: Any = _DEFAULT) -> None:
        self._lock = threading.RLock()
        self._memory: dict[str, str] = {}
        self._stored: frozenset[str] = frozenset()
        self._errors: dict[str, tuple[str, str]] = {}      # item -> (operation, why it failed)
        self._error_text = ""
        why = ""
        if backend is _DEFAULT:
            backend, why = _system_backend()
        self._backend = backend
        self.backend_name: str = _full_name(backend)
        self.available, refused = backend_ok(backend)
        self._unavailable = "" if self.available else (why or refused)

    @property
    def reason(self) -> str:
        """Empty when the keychain is usable and nothing failed; else why values are, or were, held in memory only."""
        return self._error_text if self.available else self._unavailable

    def stored(self, name: str) -> bool:
        return name in self._stored

    def _mark(self, name: str, stored: bool) -> None:
        self._stored = self._stored | {name} if stored else self._stored - {name}

    def _record(self, name: str, op: str, problem: str) -> None:
        self._errors[name] = (op, problem)
        self._error_text = "; ".join(dict.fromkeys(why for _, why in self._errors.values()))

    def _clear(self, name: str, *ops: str) -> None:
        if name in self._errors and (not ops or self._errors[name][0] in ops):
            del self._errors[name]
            self._error_text = "; ".join(dict.fromkeys(why for _, why in self._errors.values()))

    def get(self, name: str) -> str | None:
        with self._lock:
            if name in self._memory:
                return self._memory[name]
            if not self.available:
                return None
            try:
                value = self._backend.get_password(SERVICE, name)
            except Exception as exc:  # noqa: BLE001 -- locked keychain, D-Bus gone, platform error...
                self._record(name, "read", f"could not read {_label(name)} from the keychain ({_describe(exc)})")
                return None
            self._clear(name, "read")
            if not isinstance(value, str):
                self._mark(name, False)
                return None
            self._memory[name] = value
            self._mark(name, True)
            return value

    def set(self, name: str, value: str) -> bool:
        """True = written to the keychain; False = held in memory only (no keychain, or it refused)."""
        with self._lock:
            self._memory[name] = value
            if not self.available:
                self._mark(name, False)
                return False
            try:
                self._backend.set_password(SERVICE, name, value)
            except Exception as exc:  # noqa: BLE001
                self._mark(name, False)
                self._record(name, "save", f"could not save {_label(name)} in the keychain ({_describe(exc, value)})")
                self._drop_from_backend(name)       # never leave an older value there for the next start to load
                return False
            self._mark(name, True)
            self._clear(name)
            return True

    def delete(self, name: str) -> None:
        """Remove the item from memory and the keychain; a missing item is fine."""
        with self._lock:
            self._memory.pop(name, None)
            self._mark(name, False)
            if not self.available:
                return
            try:
                self._backend.delete_password(SERVICE, name)
            except Exception as exc:  # noqa: BLE001
                # keyring raises PasswordDeleteError for a missing item, but some backends (macOS) raise it for a
                # real failure too, so a delete only counts as done once the item is really gone.
                if not _is_missing(exc) or self._still_there(name):
                    self._record(name, "delete",
                                 f"could not delete {_label(name)} from the keychain ({_describe(exc)})")
                    return
            self._clear(name)

    def _still_there(self, name: str) -> bool:
        try:
            return self._backend.get_password(SERVICE, name) is not None
        except Exception:  # noqa: BLE001 -- cannot tell: assume it is still there
            return True

    def _drop_from_backend(self, name: str) -> None:
        try:
            self._backend.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001 -- best effort; the failure that brought us here is already recorded
            pass


# ── validation (messages never echo the input) ──────────────────────────────────────────────────────────

def _has_bad_char(text: str) -> bool:
    """Whitespace or any Unicode "other" character (control, format such as U+202E, a lone surrogate, private use,
    unassigned): none belongs in a key, a token or a URL, and a lone surrogate cannot even be put in an env."""
    return any(ch.isspace() or unicodedata.category(ch)[0] == "C" for ch in text)


def _check_key(key: Any) -> str:
    if not isinstance(key, str):
        raise AccessError("the API key must be text")
    key = key.strip()
    if not key:
        raise AccessError("the API key is empty")
    if len(key) > KEY_MAX:
        raise AccessError(f"the API key is longer than {KEY_MAX} characters")
    if _has_bad_char(key):
        raise AccessError("the API key contains a space or a control character")
    return key


def _check_token(token: Any) -> str:
    if token is None:
        return ""                          # no token: a local Ollama needs none
    if not isinstance(token, str):
        raise AccessError("the endpoint token must be text")
    token = token.strip()
    if len(token) > TOKEN_MAX:
        raise AccessError(f"the endpoint token is longer than {TOKEN_MAX} characters")
    if _has_bad_char(token):
        raise AccessError("the endpoint token contains a space or a control character")
    return token


def _check_model(model: Any) -> str:
    # Not starting with "-": the name reaches `claude --model` (through ORION_MODEL), where it would read as a flag.
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model.strip()) or model.strip().startswith("-"):
        raise AccessError("the model name must be 1-120 letters, digits or . _ : / @ - characters, not starting "
                          "with -")
    return model.strip()


def _check_auth(auth: Any) -> str:
    choice = "bearer" if auth is None else auth.strip().lower() if isinstance(auth, str) else ""
    if choice not in AUTHS:
        raise AccessError('the token header must be "bearer" or "x-api-key"')
    return choice


def _check_base_url(url: Any) -> str:
    if not isinstance(url, str):
        raise AccessError("the base URL must be text")
    url = url.strip()
    if not url:
        raise AccessError("the base URL is empty")
    if len(url) > URL_MAX:
        raise AccessError(f"the base URL is longer than {URL_MAX} characters")
    # A backslash is a path separator to claude's (WHATWG) URL parser but not to urllib's, so the two would
    # disagree about the host; a bare "?" or "#" would be lost by urlsplit and then glued onto every API path.
    if _has_bad_char(url) or "\\" in url:
        raise AccessError("the base URL contains a space, a backslash or a control character")
    if "?" in url or "#" in url:
        raise AccessError("the base URL cannot have a query (?...) or a fragment (#...)")
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port                     # ValueError for a port that is not a number in 0-65535
    except ValueError:
        raise AccessError("the base URL is not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise AccessError("the base URL must start with http:// or https://")
    if "@" in parts.netloc:
        raise AccessError("the base URL cannot hold a user name or password (user:pass@); "
                          "put the token in the token field")
    if not parts.hostname:
        raise AccessError("the base URL has no host")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _remote_http(base_url: str | None) -> bool:
    """Plain http to a host that is not this machine: the token and the code travel unencrypted."""
    parts = urllib.parse.urlsplit(base_url or "")
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


# ── access ───────────────────────────────────────────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class _Setting:
    """One access setting, swapped in whole so readers never see half of one. The secrets stay out of repr()."""
    mode: str = "login"
    source: str = "none"                                    # "ui" | "env" | "none"
    key: str | None = dataclasses.field(default=None, repr=False)
    base_url: str | None = None
    model: str | None = None
    auth: str = "bearer"
    token: str = dataclasses.field(default="", repr=False)
    items: tuple[str, ...] = ()                             # keychain items holding it: persisted = all stored
    notes: tuple[str, ...] = ()                             # what load() had to say about the saved setting


def _child_vars(st: _Setting) -> dict[str, str]:
    """What a mode puts into a scan's environment, after every ACCESS_VARS name has been removed."""
    if st.mode == "anthropic_key" and st.key:
        return {"ANTHROPIC_API_KEY": st.key}
    if st.mode != "endpoint":
        return {}
    add = {"ANTHROPIC_BASE_URL": st.base_url or ""}
    # Always a credential (NO_TOKEN when the endpoint needs none): never the login's, sent to the endpoint.
    add["ANTHROPIC_AUTH_TOKEN" if st.auth == "bearer" else "ANTHROPIC_API_KEY"] = st.token or NO_TOKEN
    add["ORION_MODEL"] = st.model or ""
    add.update(dict.fromkeys(ALIASES, st.model or ""))
    add["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    return add


def _hint(secret: str | None) -> str | None:
    return "..." + secret[-4:] if secret and len(secret) >= HINT_MIN_LEN else None


def _redactor(secrets: tuple[str, ...]) -> re.Pattern[str]:
    """One alternation, longest first, so a secret that contains another is cut out whole and nothing already
    replaced is scanned again. Each secret is also matched as JSON escapes it, once and twice, ASCII-only or not:
    the server scrubs lines it has already JSON-encoded, and a usage event's detail is JSON inside JSON."""
    forms: set[str] = set()
    for secret in secrets:
        forms.add(secret)
        for ensure_ascii in (True, False):
            form = secret
            for _ in range(2):
                form = json.dumps(form, ensure_ascii=ensure_ascii)[1:-1]
                forms.add(form)
    return re.compile("|".join(re.escape(f) for f in sorted(forms, key=lambda f: (-len(f), f))))


def _scrub_tree(value: Any, scrub: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_tree(v, scrub) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_tree(v, scrub) for v in value]
    return value


def _on(environ: Mapping[str, str], name: str) -> bool:
    """A switch variable set to something other than an off value (any letter case of the name)."""
    for key, value in environ.items():
        if key.upper() == name and isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
    return False


def _names(names: list[str]) -> tuple[str, str, str]:
    """("A, B", "are", "them") / ("A", "is", "it") for a note's sentence."""
    return ", ".join(names), ("is" if len(names) == 1 else "are"), ("it" if len(names) == 1 else "them")


class Access:
    """The model access the NEXT scan uses (one model per scan: discovery and the verifier share it).

    The constructor loads the saved setting; `load()` reads it again. Writers (load / set_* / forget) run one at a
    time; readers (public, child_env, secrets, scrub, endpoint_model) read one immutable snapshot, so a scan's
    output scrubber never waits on a slow keychain write."""

    def __init__(self, keychain: Keychain, environ: Mapping[str, str] | None = None) -> None:
        self._keychain = keychain
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._lock = threading.RLock()
        self._setting = _Setting()
        self._scrubber: tuple[tuple[str, ...], re.Pattern[str] | None] = ((), None)
        self.load()

    # -- reading and writing the setting --

    def load(self) -> None:
        """A saved setting wins. With none (or one whose secret is missing, or that does not validate), a
        non-empty ANTHROPIC_API_KEY in the environment is used as is (source "env"; it is never copied into the
        keychain); else the claude login (source "none"). Never writes."""
        with self._lock:
            notes: list[str] = []
            setting = self._saved(notes)
            if setting is None:
                value = self._environ.get("ANTHROPIC_API_KEY")
                key = value.strip() if isinstance(value, str) else ""
                setting = _Setting("anthropic_key", "env", key=key) if key else _Setting()
            self._setting = dataclasses.replace(setting, notes=tuple(notes))

    def _saved(self, notes: list[str]) -> _Setting | None:
        raw = self._keychain.get(_SETTINGS)
        if raw is None:
            return None
        try:
            saved = json.loads(raw)
            mode = saved["mode"]
            if mode not in MODES:
                raise ValueError("unknown mode")
        except (ValueError, TypeError, KeyError):
            notes.append("the saved access setting could not be read, so it was not used")
            return None
        if mode == "login":
            return _Setting("login", "ui", items=(_SETTINGS,))
        if mode == "anthropic_key":
            key = self._saved_secret(_KEY, _check_key)
            if not key:
                notes.append("the saved Anthropic key is missing from the keychain, so the saved setting was not used")
                return None
            return _Setting("anthropic_key", "ui", key=key, items=(_SETTINGS, _KEY))
        try:
            base_url, model = _check_base_url(saved.get("base_url")), _check_model(saved.get("model"))
            auth = _check_auth(saved.get("auth"))
        except AccessError:
            notes.append("the saved endpoint setting is not valid, so it was not used")
            return None
        token_set = saved.get("token_set")     # absent from an item written by hand: use a token item if present
        token = "" if token_set is False else (self._saved_secret(_TOKEN, _check_token) or "")
        if token_set is True and not token:
            notes.append("the saved endpoint token is missing from the keychain, so the saved setting was not used")
            return None
        return _Setting("endpoint", "ui", base_url=base_url, model=model, auth=auth, token=token,
                        items=(_SETTINGS, _TOKEN) if token else (_SETTINGS,))

    def _saved_secret(self, name: str, check: Callable[[Any], str]) -> str | None:
        value = self._keychain.get(name)
        if not value:
            return None
        try:
            return check(value)
        except AccessError:
            return None

    def _save(self, settings: dict, *, drop: tuple[str, ...], secret: tuple[str, str] | None = None) -> None:
        """Other modes' secrets deleted, this mode's secret written, the settings item last. A secret the keychain
        refuses is held in memory; the keychain then has the settings without it, which load() treats as
        "secret missing" -- so a restart falls back instead of using anything older."""
        for name in drop:
            self._keychain.delete(name)
        if secret is not None:
            self._keychain.set(*secret)
        self._keychain.set(_SETTINGS, json.dumps(settings, sort_keys=True))

    def set_login(self) -> dict:
        """Scans use the claude login; the saved key and token are deleted."""
        with self._lock:
            self._save({"mode": "login"}, drop=(_KEY, _TOKEN))
            self._setting = _Setting("login", "ui", items=(_SETTINGS,))
            return self.public()

    def set_anthropic_key(self, key: str) -> dict:
        key = _check_key(key)
        with self._lock:
            self._save({"mode": "anthropic_key"}, drop=(_TOKEN,), secret=(_KEY, key))
            self._setting = _Setting("anthropic_key", "ui", key=key, items=(_SETTINGS, _KEY))
            return self.public()

    def set_endpoint(self, base_url: str, token: str, model: str, auth: str = "bearer") -> dict:
        base_url, token = _check_base_url(base_url), _check_token(token)
        model, auth = _check_model(model), _check_auth(auth)
        settings = {"mode": "endpoint", "base_url": base_url, "model": model, "auth": auth, "token_set": bool(token)}
        with self._lock:
            if token:
                self._save(settings, drop=(_KEY,), secret=(_TOKEN, token))
            else:
                self._save(settings, drop=(_KEY, _TOKEN))
            self._setting = _Setting("endpoint", "ui", base_url=base_url, model=model, auth=auth, token=token,
                                     items=(_SETTINGS, _TOKEN) if token else (_SETTINGS,))
            return self.public()

    def forget(self) -> dict:
        """Delete every saved item (keychain and memory), then load() again: an environment key may apply."""
        with self._lock:
            for name in (_SETTINGS, _KEY, _TOKEN):
                self._keychain.delete(name)
            self.load()
            return self.public()

    # -- what leaves this module --

    def public(self) -> dict:
        """The setting as the page may show it: never a secret (and scrubbed once more as a last guard)."""
        st, kc = self._setting, self._keychain
        endpoint = None
        if st.mode == "endpoint":
            endpoint = {"base_url": st.base_url, "model": st.model, "auth": st.auth,
                        "token_set": bool(st.token), "token_hint": _hint(st.token)}
        out = {
            "mode": st.mode,
            "source": st.source,
            "key_set": st.mode == "anthropic_key" and bool(st.key),
            "key_hint": _hint(st.key) if st.mode == "anthropic_key" else None,
            "endpoint": endpoint,
            "persisted": bool(st.items) and all(kc.stored(name) for name in st.items),
            "keychain": {"available": kc.available, "backend": kc.backend_name, "reason": kc.reason},
            "notes": self._notes(st),
        }
        return _scrub_tree(out, self.scrub)

    def _notes(self, st: _Setting) -> list[str]:
        notes = list(st.notes)
        present = {name.upper() for name, value in self._environ.items() if isinstance(value, str) and value.strip()}
        watched = (ACCESS_VARS + (("ORION_MODEL",) if st.mode == "endpoint" else ())
                   + (LOGIN_VARS + PROVIDER_VARS if st.mode in ("anthropic_key", "endpoint") else ()))
        sets = _child_vars(st)
        provider = [v for v in PROVIDER_VARS if v in present and _on(self._environ, v)]
        if st.mode == "login" and provider:
            names, verb, _ = _names(provider)
            notes.append(f"{names} {verb} set in the server's environment: scans with the claude login go to that "
                         "provider, as the claude CLI itself does")
        inherited = [v for v in watched if v in present and not (st.source == "env" and v == "ANTHROPIC_API_KEY")]
        dropped = [v for v in inherited if v not in sets]
        if dropped:
            names, verb, it = _names(dropped)
            notes.append(f"{names} {verb} set in the server's environment; scans in this mode drop {it}")
        replaced = [v for v in inherited if v in sets]
        if replaced:
            names, verb, it = _names(replaced)
            notes.append(f"{names} {verb} set in the server's environment; "
                         f"scans in this mode replace {it} with the value set here")
        if st.mode == "endpoint" and not st.token:
            notes.append(f'no token is set, so scans send the placeholder "{NO_TOKEN}": never your claude login, '
                         "which claude would otherwise send to the endpoint")
        if st.mode == "endpoint" and _remote_http(st.base_url):
            what = "your code and the token cross" if st.token else "your code crosses"
            notes.append(f"the endpoint is plain http on another machine: {what} the network unencrypted")
        kc = self._keychain
        if kc.available and kc.reason:         # e.g. a forget the keychain refused: the setting is still saved
            notes.append(f"keychain problem: {kc.reason}")
        return notes

    def child_env(self, base: Mapping[str, str]) -> dict[str, str]:
        """A copy of `base` without any ACCESS_VARS name (or a name this mode sets; in the key and endpoint modes
        also the login's LOGIN_VARS and the PROVIDER_VARS switches), in any letter case, plus what the mode adds:
        login nothing; anthropic_key
        ANTHROPIC_API_KEY; endpoint the base URL, the token (NO_TOKEN when it needs none) as ANTHROPIC_AUTH_TOKEN
        ("bearer") or ANTHROPIC_API_KEY ("x-api-key"), ORION_MODEL and every alias = the model,
        CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1."""
        st = self._setting
        add = _child_vars(st)
        own = (*LOGIN_VARS, *PROVIDER_VARS) if st.mode in ("anthropic_key", "endpoint") else ()
        drop = {name.upper() for name in (*ACCESS_VARS, *own, *add)}
        env = {k: v for k, v in base.items() if k.upper() not in drop}
        env.update(add)
        return env

    @property
    def endpoint_model(self) -> str | None:
        """The endpoint's model in endpoint mode (a scan there must use it), else None."""
        st = self._setting
        return st.model if st.mode == "endpoint" else None

    def secrets(self) -> list[str]:
        """Every secret held, environment-sourced ones too (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN of the
        server's environment, used or not), at least MIN_SECRET_LEN characters long."""
        st = self._setting
        out: list[str] = []
        for value in (st.key, st.token, self._environ.get("ANTHROPIC_API_KEY"),
                      self._environ.get("ANTHROPIC_AUTH_TOKEN")):
            value = value.strip() if isinstance(value, str) else ""
            if len(value) >= MIN_SECRET_LEN and value not in out:
                out.append(value)
        return out

    def scrub(self, text: str) -> str:
        """`text` with each secret (longest first, JSON-escaped forms too) replaced by "[redacted]"."""
        if not text:
            return text
        secrets = tuple(self.secrets())
        if not secrets:
            return text
        cached = self._scrubber
        if cached[0] != secrets or cached[1] is None:
            cached = (secrets, _redactor(secrets))
            self._scrubber = cached
        return cached[1].sub(REDACTED, text)
