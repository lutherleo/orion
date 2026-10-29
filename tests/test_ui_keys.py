"""orion/ui/keys.py: a bring-your-own key leaves only as a hint, lands nowhere but the OS keychain (or memory), and
every scan's child env carries exactly the mode picked on the page. Token-free, and no test touches this machine's
real keychain: every Keychain gets a fake backend, None, or a fake `keyring` module."""
from __future__ import annotations

import json
import sys
import types

import pytest

from orion.ui import keys
from orion.ui.keys import Access, AccessError, Keychain, backend_ok

# Fake values shaped like the real thing. All are >= 12 characters, so they are scrubbed and hinted.
KEY = "sk-ant-test-0000-1111-2222-wxyz"
KEY2 = "sk-ant-test-3333-4444-5555-qrst"
TOKEN = "tok-test-6666-7777-8888-abcd"
ENV_KEY = "sk-ant-env-9999-0000-1111-efgh"
ENV_TOKEN = "tok-env-2222-3333-4444-ijkl"
SECRETS = (KEY, KEY2, TOKEN, ENV_KEY, ENV_TOKEN)
GW = "https://gw.example.com"


class PasswordDeleteError(Exception):
    """Named like keyring.errors.PasswordDeleteError: keys.py matches it by name, so the fakes need no keyring."""


class FakeStore:
    """An in-memory keyring backend (priority 1), keyed by (service, item). `fail` lists operations that raise --
    "set", or "set:<item>" for one item -- with a message that echoes the value, as a careless backend might."""
    priority = 1

    def __init__(self, **items: str) -> None:
        self.items = {(keys.SERVICE, name): value for name, value in items.items()}
        self.calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def _maybe_fail(self, op: str, username: str, value: str | None = None) -> None:
        self.calls.append((op, username))
        if op in self.fail or f"{op}:{username}" in self.fail:
            raise RuntimeError(f"keychain refused {op} of {username}: {value}")

    def get_password(self, service, username):
        self._maybe_fail("get", username)
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self._maybe_fail("set", username, password)
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self._maybe_fail("delete", username)
        if (service, username) not in self.items:
            raise PasswordDeleteError(username)
        del self.items[(service, username)]

    def saved(self, name: str) -> str | None:
        return self.items.get((keys.SERVICE, name))


def backend(module: str, name: str, base: type = FakeStore, **attrs):
    """An instance of a fake backend class that claims to be `module.name` (backend_ok judges by class)."""
    return type(name, (base,), {"__module__": module, **attrs})()


def chainer(members, **attrs):
    return backend("keyring.backends.chainer", "ChainerBackend", **{"backends": members, "priority": 10, **attrs})


def _raises(self):
    raise RuntimeError("Requires Windows and pywin32")


def fresh(store: FakeStore | None = None, **environ: str) -> Access:
    """An Access on `store` (a new FakeStore by default) with exactly `environ` -- never the real one."""
    return Access(Keychain(FakeStore() if store is None else store), environ=environ)


def restart(store: FakeStore, **environ: str) -> Access:
    """A new process on the same keychain: nothing in memory, whatever the keychain holds."""
    return Access(Keychain(store), environ=environ)


# ── backend_ok ───────────────────────────────────────────────────────────────────────────────────────

def test_no_backend_is_refused():
    assert backend_ok(None) == (False, "no keychain backend on this machine")


@pytest.mark.parametrize("module, name, why", [
    ("keyring.backends.fail", "Keyring", "no keychain backend on this machine (keyring.backends.fail.Keyring)"),
    ("keyring.backends.null", "Keyring", "no keychain backend on this machine (keyring.backends.null.Keyring)"),
    ("keyrings.alt.file", "PlaintextKeyring", "plaintext backend refused (keyrings.alt.file.PlaintextKeyring)"),
    ("keyrings.alt.file", "EncryptedKeyring", "keyrings.alt backend refused (keyrings.alt.file.EncryptedKeyring)"),
    ("keyrings.alt.Windows", "RegistryKeyring", "keyrings.alt backend refused (keyrings.alt.Windows.RegistryKeyring)"),
    ("corp.store", "PlaintextStore", "plaintext backend refused (corp.store.PlaintextStore)"),
    ("corp.store", "NullStore", "no keychain backend on this machine (corp.store.NullStore)"),
    ("corp.store", "FailStore", "no keychain backend on this machine (corp.store.FailStore)"),
])
def test_backends_that_are_not_a_real_keychain_are_refused(module, name, why):
    assert backend_ok(backend(module, name)) == (False, why)


def test_a_subclass_of_a_refused_backend_is_refused_too():
    plain = type("PlaintextKeyring", (FakeStore,), {"__module__": "keyrings.alt.file"})
    ok, why = backend_ok(type("TeamStore", (plain,), {"__module__": "corp.store"})())
    assert not ok and why == "plaintext backend refused (corp.store.TeamStore, a keyrings.alt.file.PlaintextKeyring)"


@pytest.mark.parametrize("priority, shown", [(0, "priority 0"), (-1, "priority -1"), (0.0, "priority 0"),
                                             (float("nan"), "priority nan"), (None, "no numeric priority"),
                                             ("5", "no numeric priority"), (True, "no numeric priority")])
def test_the_priority_must_be_a_positive_number(priority, shown):
    ok, why = backend_ok(backend("keyring.backends.Windows", "WinVaultKeyring", priority=priority))
    assert (ok, why) == (False, f"keychain backend not usable (keyring.backends.Windows.WinVaultKeyring, {shown})")


def test_a_priority_that_raises_or_is_missing_is_refused():
    ok, why = backend_ok(backend("keyring.backends.Windows", "WinVaultKeyring", priority=property(_raises)))
    assert (ok, why) == (False, "keychain backend not usable (keyring.backends.Windows.WinVaultKeyring: RuntimeError)")
    ok, why = backend_ok(backend("corp.store", "Store", base=object))
    assert not ok and "AttributeError" in why


@pytest.mark.parametrize("module, name, priority", [
    ("keyring.backends.Windows", "WinVaultKeyring", 5),
    ("keyring.backends.macOS", "Keyring", 5),
    ("keyring.backends.SecretService", "Keyring", 4.8),
    ("keyring.backends.kwallet", "DBusKeyring", 4.9),
])
def test_real_keychain_backends_are_usable(module, name, priority):
    assert backend_ok(backend(module, name, priority=priority)) == (True, "")


def test_a_chainer_is_usable_only_when_every_member_is():
    secret_service = backend("keyring.backends.SecretService", "Keyring")
    kwallet = backend("keyring.backends.kwallet", "DBusKeyring")
    assert backend_ok(chainer([secret_service, kwallet])) == (True, "")
    # keyring's own chainer reports priority -1 with one member; its members decide, not that number.
    assert backend_ok(chainer([secret_service], priority=-1)) == (True, "")
    ok, why = backend_ok(chainer([secret_service, backend("keyrings.alt.file", "PlaintextKeyring")]))
    assert not ok and why == ("plaintext backend refused (keyrings.alt.file.PlaintextKeyring), "
                              "chained in keyring.backends.chainer.ChainerBackend")
    assert backend_ok(chainer([])) == (
        False, "no keychain backend on this machine (keyring.backends.chainer.ChainerBackend with no backends)")
    ok, why = backend_ok(chainer(None, backends=property(_raises)))
    assert not ok and "RuntimeError" in why
    assert backend_ok(chainer([chainer([chainer([chainer([secret_service])])])]))[0] is False   # nested too deep


def test_keyrings_own_fail_and_null_backends_are_refused():
    fail = pytest.importorskip("keyring.backends.fail")
    null = pytest.importorskip("keyring.backends.null")
    assert backend_ok(fail.Keyring()) == (False, "no keychain backend on this machine (keyring.backends.fail.Keyring)")
    assert backend_ok(null.Keyring()) == (False, "no keychain backend on this machine (keyring.backends.null.Keyring)")


# ── Keychain ─────────────────────────────────────────────────────────────────────────────────────────

def test_a_keychain_round_trip_and_a_restart():
    store = FakeStore()
    kc = Keychain(store)
    assert (kc.available, kc.reason) == (True, "") and kc.backend_name.endswith(".FakeStore")
    assert kc.set("anthropic_api_key", KEY) is True and kc.stored("anthropic_api_key")
    assert store.saved("anthropic_api_key") == KEY and kc.get("anthropic_api_key") == KEY
    again = Keychain(store)                     # nothing in memory: the value comes back from the keychain
    assert again.get("anthropic_api_key") == KEY and again.stored("anthropic_api_key")
    again.delete("anthropic_api_key")
    assert store.items == {} and again.get("anthropic_api_key") is None and not again.stored("anthropic_api_key")
    again.delete("anthropic_api_key")           # deleting a missing item is fine
    again.delete("never-saved")
    assert again.reason == ""


def test_without_a_backend_values_live_in_memory_only():
    kc = Keychain(None)
    assert (kc.available, kc.backend_name, kc.reason) == (False, "none", "no keychain backend on this machine")
    assert kc.set("access", '{"mode": "login"}') is False and not kc.stored("access")
    assert kc.get("access") == '{"mode": "login"}'
    kc.delete("access")
    assert kc.get("access") is None


def test_a_refused_backend_is_never_called():
    store = backend("keyring.backends.fail", "Keyring")
    kc = Keychain(store)
    assert (kc.available, kc.backend_name) == (False, "keyring.backends.fail.Keyring")
    assert kc.reason == "no keychain backend on this machine (keyring.backends.fail.Keyring)"
    assert kc.set("anthropic_api_key", KEY) is False and kc.get("anthropic_api_key") == KEY
    kc.delete("anthropic_api_key")
    assert kc.get("other") is None and store.calls == [] and store.items == {}


def test_a_failing_write_degrades_to_memory_and_says_why_without_the_secret():
    store = FakeStore()
    store.fail = {"set"}
    kc = Keychain(store)
    assert kc.set("anthropic_api_key", KEY) is False and not kc.stored("anthropic_api_key")
    assert kc.get("anthropic_api_key") == KEY   # still usable until the UI stops
    assert kc.reason.startswith("could not save the Anthropic key in the keychain (RuntimeError: ")
    assert KEY not in kc.reason and "[redacted]" in kc.reason      # the backend echoed it; the reason does not
    store.fail = set()
    assert kc.set("anthropic_api_key", KEY) is True and kc.reason == ""


def test_a_refused_write_removes_the_older_value():
    store = FakeStore(anthropic_api_key=KEY)
    store.fail = {"set"}
    assert Keychain(store).set("anthropic_api_key", KEY2) is False
    assert store.saved("anthropic_api_key") is None      # the next start cannot load the old key


def test_read_and_delete_failures_never_raise():
    store = FakeStore(access='{"mode": "login"}')
    store.fail = {"get", "delete"}
    kc = Keychain(store)
    assert kc.get("access") is None and kc.reason.startswith("could not read the saved access setting")
    kc.delete("access")
    assert "could not delete the saved access setting" in kc.reason


def test_a_password_delete_error_that_leaves_the_item_is_reported():
    class Sticky(FakeStore):
        def delete_password(self, service, username):          # macOS raises this for real failures too
            raise PasswordDeleteError("Can't delete password in keychain")

    kc = Keychain(Sticky(anthropic_api_key=KEY))
    kc.delete("anthropic_api_key")
    assert kc.reason.startswith("could not delete the Anthropic key from the keychain (PasswordDeleteError")


def test_keyrings_own_password_delete_error_means_missing():
    errors = pytest.importorskip("keyring.errors")

    class Store(FakeStore):
        def delete_password(self, service, username):
            raise errors.PasswordDeleteError(username)

    kc = Keychain(Store())
    kc.delete("endpoint_token")
    assert kc.reason == ""


def test_without_keyring_installed_the_keychain_is_memory_only(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)          # `import keyring` now raises ImportError
    kc = Keychain()
    assert (kc.available, kc.backend_name, kc.reason) == (False, "none", "keyring is not installed")
    assert kc.set("access", "{}") is False and kc.get("access") == "{}"


def test_the_default_backend_is_keyrings_own_pick(monkeypatch):
    store = FakeStore()
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_keyring=lambda: store))
    kc = Keychain()
    assert kc.available and kc.set("access", "{}") and store.saved("access") == "{}"


def test_a_keyring_that_cannot_start_is_memory_only(monkeypatch):
    def broken():
        raise RuntimeError("no D-Bus session")

    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_keyring=broken))
    kc = Keychain()
    assert not kc.available and kc.reason == "the keychain could not start (RuntimeError: no D-Bus session)"


def test_a_wsl_style_fail_backend_from_keyring_is_memory_only(monkeypatch):
    fail = backend("keyring.backends.fail", "Keyring")
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_keyring=lambda: fail))
    pub = Access(Keychain(), environ={}).set_anthropic_key(KEY)
    assert pub["persisted"] is False and pub["keychain"] == {
        "available": False, "backend": "keyring.backends.fail.Keyring",
        "reason": "no keychain backend on this machine (keyring.backends.fail.Keyring)"}
    assert fail.calls == []


# ── Access: load precedence ────────────────────────────────────────────────────────────────────────────

def test_nothing_saved_and_no_env_key_means_the_login():
    pub = fresh().public()
    assert (pub["mode"], pub["source"], pub["key_set"], pub["key_hint"], pub["endpoint"], pub["persisted"]) == (
        "login", "none", False, None, None, False)
    assert pub["notes"] == []


def test_an_env_key_is_used_but_never_copied_into_the_keychain():
    store = FakeStore()
    acc = fresh(store, ANTHROPIC_API_KEY=f"  {ENV_KEY}\n")
    pub = acc.public()
    assert (pub["mode"], pub["source"], pub["key_set"], pub["key_hint"], pub["persisted"]) == (
        "anthropic_key", "env", True, "..." + ENV_KEY[-4:], False)
    assert store.items == {} and not [call for call in store.calls if call[0] == "set"]
    assert acc.child_env({})["ANTHROPIC_API_KEY"] == ENV_KEY and ENV_KEY in acc.secrets()
    assert pub["notes"] == []                   # the env key is the one in use: nothing dropped


def test_a_blank_env_key_is_ignored():
    pub = fresh(ANTHROPIC_API_KEY="   ").public()
    assert (pub["mode"], pub["source"]) == ("login", "none")


def test_a_saved_setting_wins_over_the_env_key():
    store = FakeStore()
    fresh(store).set_anthropic_key(KEY)
    acc = restart(store, ANTHROPIC_API_KEY=ENV_KEY)
    pub = acc.public()
    assert (pub["mode"], pub["source"], pub["key_hint"], pub["persisted"]) == (
        "anthropic_key", "ui", "..." + KEY[-4:], True)
    assert acc.child_env({"ANTHROPIC_API_KEY": ENV_KEY})["ANTHROPIC_API_KEY"] == KEY
    assert pub["notes"] == ["ANTHROPIC_API_KEY is set in the server's environment; "
                            "scans in this mode replace it with the value set here"]
    fresh(store).set_login()
    pub = restart(store, ANTHROPIC_API_KEY=ENV_KEY).public()
    assert (pub["mode"], pub["source"], pub["persisted"], pub["key_set"]) == ("login", "ui", True, False)
    assert pub["notes"] == ["ANTHROPIC_API_KEY is set in the server's environment; scans in this mode drop it"]


def test_a_saved_key_whose_secret_is_missing_falls_back():
    store = FakeStore(access=json.dumps({"mode": "anthropic_key"}))
    pub = restart(store, ANTHROPIC_API_KEY=ENV_KEY).public()
    assert (pub["mode"], pub["source"]) == ("anthropic_key", "env")
    assert pub["notes"] == ["the saved Anthropic key is missing from the keychain, so the saved setting was not used"]
    pub = restart(store).public()
    assert (pub["mode"], pub["source"]) == ("login", "none")


def test_a_saved_endpoint_whose_token_is_missing_falls_back():
    store = FakeStore()
    fresh(store).set_endpoint(GW, TOKEN, "m1")
    del store.items[(keys.SERVICE, "endpoint_token")]
    pub = restart(store).public()
    assert (pub["mode"], pub["source"]) == ("login", "none")
    assert pub["notes"] == ["the saved endpoint token is missing from the keychain, so the saved setting was not used"]


def test_a_saved_endpoint_needing_no_token_loads():
    store = FakeStore()
    fresh(store).set_endpoint("http://localhost:11434/", "", "qwen3:8b")
    pub = restart(store).public()
    assert (pub["mode"], pub["source"], pub["persisted"]) == ("endpoint", "ui", True)
    assert pub["endpoint"] == {"base_url": "http://localhost:11434", "model": "qwen3:8b", "auth": "bearer",
                               "token_set": False, "token_hint": None}


def test_a_hand_written_item_in_the_documented_shape_loads():
    store = FakeStore(access=json.dumps({"mode": "endpoint", "base_url": GW, "model": "m1", "auth": "x-api-key"}),
                      endpoint_token=TOKEN)
    acc = restart(store)
    assert acc.endpoint_model == "m1" and acc.child_env({})["ANTHROPIC_API_KEY"] == TOKEN
    assert acc.public()["persisted"] is True


@pytest.mark.parametrize("raw", [
    "not json", "[]", '"login"', "{}", '{"mode": "root"}', '{"mode": ["login"]}',
    json.dumps({"mode": "endpoint", "base_url": "file:///etc", "model": "m1"}),
    json.dumps({"mode": "endpoint", "base_url": GW, "model": "bad model"}),
])
def test_an_unreadable_saved_setting_is_ignored(raw):
    pub = restart(FakeStore(access=raw)).public()
    assert (pub["mode"], pub["source"]) == ("login", "none") and len(pub["notes"]) == 1


def test_a_saved_endpoint_survives_a_restart_and_its_item_holds_no_secret():
    store = FakeStore()
    first = fresh(store)
    pub = first.set_endpoint(GW + "/v1/", TOKEN, "m1", auth="x-api-key")
    assert pub["persisted"] is True and TOKEN not in store.saved("access")
    assert json.loads(store.saved("access")) == {"mode": "endpoint", "base_url": GW + "/v1", "model": "m1",
                                                 "auth": "x-api-key", "token_set": True}
    assert restart(store).public() == first.public()


def test_the_default_environment_is_the_process_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_KEY)
    pub = Access(Keychain(None)).public()
    assert (pub["mode"], pub["source"]) == ("anthropic_key", "env")


# ── Access: validation ───────────────────────────────────────────────────────────────────────────────

BAD_KEYS = ["", "   ", "sk-ant bad", "sk-ant\tbad", "sk-ant\nbad", "sk\x00ant", "sk\x7fant", "sk‮ant",
            "sk​ant", "sk ant", "x" * 513, None, 123, b"sk-ant"]


@pytest.mark.parametrize("bad", BAD_KEYS)
def test_bad_keys_are_refused_and_change_nothing(bad):
    store = FakeStore()
    acc = fresh(store)
    before = acc.public()
    with pytest.raises(AccessError) as err:
        acc.set_anthropic_key(bad)
    assert acc.public() == before and store.items == {} and acc.secrets() == []
    if isinstance(bad, str) and bad.strip():
        assert bad.strip() not in str(err.value)            # never echoed back to the page


def test_a_key_is_stripped_and_may_be_512_characters():
    acc = fresh()
    acc.set_anthropic_key(f"  {KEY}\n")
    assert acc.child_env({})["ANTHROPIC_API_KEY"] == KEY
    acc.set_anthropic_key("k" * 512)
    assert acc.child_env({})["ANTHROPIC_API_KEY"] == "k" * 512


BAD_URLS = ["", "   ", "ftp://h.example", "file:///etc/passwd", "javascript:alert(1)", "localhost:11434", "//h.example",
            "http://", "http:///v1", "http://alice:hunter2-secret@h.example", "http://alice@h.example",
            "http://h.example:11434@evil.example", "http://h.example/?x=1",
            "http://h.example?", "http://h.example/#f", "http://h.example:99999", "http://h.example:port",
            "http://h.example/a b", "http://h.example\\@evil.example", "http://[::1", "http://h.example/\x00",
            "http://" + "a" * 2050, None, 5]


@pytest.mark.parametrize("bad", BAD_URLS)
def test_bad_base_urls_are_refused_and_change_nothing(bad):
    store = FakeStore()
    acc = fresh(store)
    before = acc.public()
    with pytest.raises(AccessError) as err:
        acc.set_endpoint(bad, TOKEN, "m1")
    assert acc.public() == before and store.items == {}
    message = str(err.value)
    assert message and "hunter2" not in message and "alice" not in message and TOKEN not in message
    if isinstance(bad, str) and bad.strip():
        assert bad.strip() not in message       # never echoed back to the page


@pytest.mark.parametrize("url, kept", [
    ("http://localhost:11434/", "http://localhost:11434"),
    ("  https://gw.example.com/anthropic/v1/  ", "https://gw.example.com/anthropic/v1"),
    ("HTTPS://GW.example.com", "https://GW.example.com"),
    ("http://[::1]:11434", "http://[::1]:11434"),
    ("http://127.0.0.1:8080//", "http://127.0.0.1:8080"),
])
def test_good_base_urls_lose_only_the_trailing_slash(url, kept):
    acc = fresh()
    assert acc.set_endpoint(url, "", "m1")["endpoint"]["base_url"] == kept
    assert acc.child_env({})["ANTHROPIC_BASE_URL"] == kept


@pytest.mark.parametrize("bad", ["", "  ", "bad model", "m" * 121, "a;b", "gpt\nx", "mod$el", "mé", None, 7])
def test_bad_model_names_are_refused(bad):
    with pytest.raises(AccessError):
        fresh().set_endpoint(GW, TOKEN, bad)


@pytest.mark.parametrize("model", ["qwen3:8b", "anthropic/claude-sonnet-4@2025", "gpt-oss_20b.q4", "m" * 120, " m1 ",
                                   "qwen3:8b\n"])
def test_good_model_names_are_kept(model):
    assert fresh().set_endpoint(GW, TOKEN, model)["endpoint"]["model"] == model.strip()


@pytest.mark.parametrize("bad", ["basic", "", "bearer token", 1])
def test_bad_auth_headers_are_refused(bad):
    with pytest.raises(AccessError):
        fresh().set_endpoint(GW, TOKEN, "m1", auth=bad)


def test_the_auth_header_is_case_insensitive_and_defaults_to_bearer():
    assert fresh().set_endpoint(GW, TOKEN, "m1", auth=" X-API-Key ")["endpoint"]["auth"] == "x-api-key"
    assert fresh().set_endpoint(GW, TOKEN, "m1", auth=None)["endpoint"]["auth"] == "bearer"


@pytest.mark.parametrize("bad", ["tok en", "tok\x00en", "tok‮en", "t" * 4097, 12, b"tok"])
def test_bad_tokens_are_refused(bad):
    store = FakeStore()
    with pytest.raises(AccessError) as err:
        fresh(store).set_endpoint(GW, bad, "m1")
    assert store.items == {}
    if isinstance(bad, str):
        assert bad not in str(err.value)


def test_a_token_is_stripped_and_may_be_empty():
    acc = fresh()
    assert acc.set_endpoint(GW, f" {TOKEN}\n", "m1")["endpoint"]["token_set"] is True
    assert acc.child_env({})["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    for empty in ("", "   ", None):
        assert acc.set_endpoint(GW, empty, "m1")["endpoint"]["token_set"] is False
    assert acc.set_endpoint(GW, "t" * 4096, "m1")["endpoint"]["token_set"] is True


# ── Access: what leaves the module ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secret, hint", [("a" * 11, None), ("abcdefgh" + "wxyz", "...wxyz"), (KEY, "...wxyz")])
def test_hints_show_four_characters_of_long_secrets_only(secret, hint):
    acc = fresh()
    assert acc.set_anthropic_key(secret)["key_hint"] == hint
    assert acc.set_endpoint(GW, secret, "m1")["endpoint"]["token_hint"] == hint
    assert acc.set_endpoint(GW, "", "m1")["endpoint"]["token_hint"] is None


def test_public_never_contains_a_secret():
    store = FakeStore()
    acc = fresh(store, ANTHROPIC_API_KEY=ENV_KEY, ANTHROPIC_AUTH_TOKEN=ENV_TOKEN)
    seen = [acc.public(), acc.set_anthropic_key(KEY), acc.set_anthropic_key(KEY2), acc.set_endpoint(GW, TOKEN, "m1"),
            acc.set_endpoint(GW, TOKEN, "m1", auth="x-api-key"), acc.set_login(), acc.forget(), acc.public()]
    text = json.dumps(seen)
    for secret in SECRETS:
        assert secret not in text
    assert all(isinstance(pub["notes"], list) for pub in seen)


def test_public_is_scrubbed_even_when_a_secret_lands_in_the_wrong_field():
    pub = fresh().set_endpoint(GW, TOKEN, TOKEN)            # the token pasted as the model too
    assert TOKEN not in json.dumps(pub) and pub["endpoint"]["model"] == "[redacted]"


def test_a_keychain_error_that_echoes_the_secret_is_redacted_and_the_key_still_works():
    store = FakeStore()
    store.fail = {"set"}
    acc = fresh(store)
    pub = acc.set_anthropic_key(KEY)
    assert pub["persisted"] is False and pub["keychain"]["reason"].startswith("could not save the Anthropic key")
    assert pub["notes"] and pub["notes"][-1].startswith("keychain problem: could not save the Anthropic key")
    assert KEY not in json.dumps(pub)
    assert acc.child_env({})["ANTHROPIC_API_KEY"] == KEY    # held in memory: scans still get it


def test_a_secret_the_keychain_refuses_is_never_brought_back_by_a_restart():
    store = FakeStore()
    acc = fresh(store)
    acc.set_anthropic_key(KEY)
    store.fail = {"set:endpoint_token"}                     # e.g. Windows refuses a token over its size limit
    pub = acc.set_endpoint(GW, TOKEN, "m1")
    assert (pub["mode"], pub["persisted"]) == ("endpoint", False)
    assert pub["keychain"]["reason"].startswith("could not save the endpoint token")
    assert store.saved("anthropic_api_key") is None and store.saved("endpoint_token") is None
    store.fail = set()
    pub = restart(store).public()
    assert (pub["mode"], pub["key_set"]) == ("login", False)  # never the old KEY
    assert "missing" in pub["notes"][0]


def test_saving_one_mode_deletes_the_other_modes_secrets():
    store = FakeStore()
    acc = fresh(store)
    acc.set_anthropic_key(KEY)
    acc.set_endpoint(GW, TOKEN, "m1")
    assert store.saved("anthropic_api_key") is None and store.saved("endpoint_token") == TOKEN
    acc.set_endpoint("http://localhost:11434", "", "qwen3")
    assert store.saved("endpoint_token") is None
    acc.set_anthropic_key(KEY)
    acc.set_login()
    assert set(store.items) == {(keys.SERVICE, "access")} and json.loads(store.saved("access")) == {"mode": "login"}
    assert acc.secrets() == []


def test_the_mode_constants():
    assert keys.SERVICE == "orion-ui" and keys.MODES == ("login", "anthropic_key", "endpoint")
    assert keys.ALIASES == ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_SMALL_FAST_MODEL")
    assert keys.ACCESS_VARS == ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", *keys.ALIASES,
                                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS")


# ── Access: child_env ────────────────────────────────────────────────────────────────────────────────

INHERITED = {
    "PATH": "/usr/bin", "HOME": "/home/u", "ORION_MODEL": "opus",
    "ANTHROPIC_API_KEY": ENV_KEY, "ANTHROPIC_AUTH_TOKEN": ENV_TOKEN, "ANTHROPIC_BASE_URL": "https://elsewhere.example",
    **dict.fromkeys(keys.ALIASES, "claude-opus-x"),
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "0",
    "anthropic_base_url": "https://lowercase.example",     # the same variable to a Windows env block
}
KEPT = {"PATH": "/usr/bin", "HOME": "/home/u", "ORION_MODEL": "opus"}


def _endpoint_env(model: str, **auth: str) -> dict[str, str]:
    return {**KEPT, "ORION_MODEL": model, "ANTHROPIC_BASE_URL": GW, **auth, **dict.fromkeys(keys.ALIASES, model),
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1"}


def test_login_adds_nothing_and_drops_every_inherited_access_variable():
    base = dict(INHERITED)
    assert fresh().child_env(base) == KEPT
    assert base == INHERITED                    # the caller's mapping is left alone


def test_key_mode_sets_only_the_key():
    acc = fresh()
    acc.set_anthropic_key(KEY)
    assert acc.child_env(INHERITED) == {**KEPT, "ANTHROPIC_API_KEY": KEY}


def test_endpoint_bearer_pins_every_model_alias():
    acc = fresh()
    acc.set_endpoint(GW + "/", TOKEN, "qwen3:8b")
    assert acc.child_env(INHERITED) == _endpoint_env("qwen3:8b", ANTHROPIC_AUTH_TOKEN=TOKEN)


def test_endpoint_x_api_key_sends_the_token_as_the_api_key():
    acc = fresh()
    acc.set_endpoint(GW, TOKEN, "m1", auth="x-api-key")
    assert acc.child_env(INHERITED) == _endpoint_env("m1", ANTHROPIC_API_KEY=TOKEN)


def test_endpoint_without_a_token_sends_the_placeholder_never_the_login():
    """With no credential at all, claude falls back to its own login and sends THAT token to the endpoint."""
    acc = fresh()
    acc.set_endpoint(GW, "", "m1")
    env = acc.child_env({**INHERITED, "orion_model": "lower", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-login-token-1234"})
    assert env == _endpoint_env("m1", ANTHROPIC_AUTH_TOKEN=keys.NO_TOKEN)
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    acc.set_endpoint(GW, "", "m1", auth="x-api-key")
    env = acc.child_env(INHERITED)
    assert env == _endpoint_env("m1", ANTHROPIC_API_KEY=keys.NO_TOKEN)
    assert any(keys.NO_TOKEN in n and "login" in n for n in acc.public()["notes"])


def test_the_login_token_is_dropped_outside_login_mode_only():
    login = {**INHERITED, "CLAUDE_CODE_OAUTH_TOKEN": "oauth-login-token-1234"}
    acc = fresh()
    assert acc.child_env(login)["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-login-token-1234"   # login: it IS the access
    acc.set_anthropic_key(KEY)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in acc.child_env(login)
    acc.set_endpoint(GW, TOKEN, "m1")
    assert "claude_code_oauth_token" not in {k.lower() for k in acc.child_env({**login, "claude_code_oauth_token": "x" * 9})}
    # The page is told when the server's own environment carries one that this mode drops.
    seen = fresh(CLAUDE_CODE_OAUTH_TOKEN="oauth-login-token-1234")
    assert not any("CLAUDE_CODE_OAUTH_TOKEN" in n for n in seen.public()["notes"])     # login mode uses it
    seen.set_anthropic_key(KEY)
    assert any("CLAUDE_CODE_OAUTH_TOKEN" in n and "drop" in n for n in seen.public()["notes"])


def test_endpoint_model_is_only_set_in_endpoint_mode():
    acc = fresh()
    assert acc.endpoint_model is None
    acc.set_endpoint("http://localhost:11434", "", "qwen3:8b")
    assert acc.endpoint_model == "qwen3:8b"
    acc.set_anthropic_key(KEY)
    assert acc.endpoint_model is None


# ── Access: notes, secrets, scrub, forget ────────────────────────────────────────────────────────────

def test_notes_name_inherited_variables_never_their_values():
    acc = fresh(ANTHROPIC_BASE_URL="https://corp-proxy.example", ANTHROPIC_AUTH_TOKEN=ENV_TOKEN)
    pub = acc.public()
    assert pub["notes"] == ["ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL are set in the server's environment; "
                            "scans in this mode drop them"]
    assert "corp-proxy" not in json.dumps(pub) and ENV_TOKEN not in json.dumps(pub)
    assert fresh(ANTHROPIC_BASE_URL="x").public()["notes"] == [
        "ANTHROPIC_BASE_URL is set in the server's environment; scans in this mode drop it"]


def test_endpoint_notes_say_what_is_replaced():
    acc = fresh(ANTHROPIC_BASE_URL="https://corp-proxy.example", ORION_MODEL="opus", ANTHROPIC_API_KEY=ENV_KEY)
    notes = acc.set_endpoint(GW, TOKEN, "m1")["notes"]
    assert notes == ["ANTHROPIC_API_KEY is set in the server's environment; scans in this mode drop it",
                     "ANTHROPIC_BASE_URL, ORION_MODEL are set in the server's environment; "
                     "scans in this mode replace them with the value set here"]


def test_plain_http_to_another_machine_is_noted():
    acc = fresh()
    assert acc.set_endpoint("http://localhost:11434", TOKEN, "m1")["notes"] == []
    assert acc.set_endpoint("http://127.0.0.2:8080", TOKEN, "m1")["notes"] == []
    assert acc.set_endpoint("http://[::1]:8080", TOKEN, "m1")["notes"] == []
    assert acc.set_endpoint("http://ollama.localhost", TOKEN, "m1")["notes"] == []
    assert acc.set_endpoint(GW, TOKEN, "m1")["notes"] == []
    assert acc.set_endpoint("http://10.0.0.5:11434", TOKEN, "m1")["notes"] == [
        "the endpoint is plain http on another machine: your code and the token cross the network unencrypted"]
    assert acc.set_endpoint("http://gpu-box.lan:11434", "", "m1")["notes"] == [
        'no token is set, so scans send the placeholder "none": never your claude login, '
        "which claude would otherwise send to the endpoint",
        "the endpoint is plain http on another machine: your code crosses the network unencrypted"]


def test_secrets_cover_env_sourced_ones_and_skip_short_ones():
    acc = fresh(ANTHROPIC_API_KEY=ENV_KEY, ANTHROPIC_AUTH_TOKEN=ENV_TOKEN)
    acc.set_endpoint(GW, TOKEN, "m1")
    assert acc.secrets() == [TOKEN, ENV_KEY, ENV_TOKEN]
    acc.set_endpoint("http://localhost:11434", "ollama", "qwen3")
    assert acc.secrets() == [ENV_KEY, ENV_TOKEN]
    assert fresh(ANTHROPIC_API_KEY="short").secrets() == []


def test_scrub_is_longest_first_and_leaves_short_tokens_alone():
    inner = "sk-env-1234"                       # 11 characters: scrubbed (>= 8), but no hint (< 12)
    acc = Access(Keychain(None), environ={"ANTHROPIC_API_KEY": inner})
    acc.set_anthropic_key(inner + "-longer")    # contains the env key
    assert acc.secrets() == [inner + "-longer", inner]
    assert acc.scrub(f"a {inner}-longer b {inner} c") == "a [redacted] b [redacted] c"
    acc.set_endpoint("http://localhost:11434", "ollama", "qwen3")
    assert acc.scrub("auth ollama ok") == "auth ollama ok"
    assert acc.scrub("") == "" and fresh().scrub("nothing held") == "nothing held"


def test_scrub_catches_json_escaped_forms():
    tok = 'tok"quoted\\slash-0123'
    acc = fresh()
    acc.set_endpoint(GW, tok, "m1")
    once = json.dumps({"detail": f"401 for {tok}"})
    twice = json.dumps({"line": once})
    escaped_once = json.dumps(tok)[1:-1]
    escaped_twice = json.dumps(escaped_once)[1:-1]
    for text in (once, twice):
        out = acc.scrub(text)
        assert "[redacted]" in out
        assert tok not in out and escaped_once not in out and escaped_twice not in out
    wide = "tök-ünïcode-0123"
    acc.set_endpoint(GW, wide, "m1")
    for text in (json.dumps(wide), json.dumps(wide, ensure_ascii=False), wide):
        assert acc.scrub(text) in ('"[redacted]"', "[redacted]")


def test_forget_deletes_every_saved_item_then_reloads():
    store = FakeStore()
    kc = Keychain(store)
    acc = Access(kc, environ={"ANTHROPIC_API_KEY": ENV_KEY})
    acc.set_endpoint(GW, TOKEN, "m1")
    assert store.items
    pub = acc.forget()
    assert store.items == {} and [kc.get(n) for n in ("access", "anthropic_api_key", "endpoint_token")] == [None] * 3
    assert (pub["mode"], pub["source"], pub["endpoint"]) == ("anthropic_key", "env", None)   # the env key again
    assert TOKEN not in acc.secrets() and acc.endpoint_model is None
    assert restart(store).public()["mode"] == "login"


def test_forget_in_memory_only_mode():
    acc = Access(Keychain(None), environ={})
    acc.set_anthropic_key(KEY)
    pub = acc.forget()
    assert (pub["mode"], pub["source"]) == ("login", "none") and acc.secrets() == []
    assert acc.child_env({"PATH": "/bin"}) == {"PATH": "/bin"}


def test_a_forget_the_keychain_refuses_is_said_not_hidden():
    store = FakeStore()
    acc = fresh(store)
    acc.set_anthropic_key(KEY)
    store.fail = {"delete"}
    pub = acc.forget()
    assert (pub["mode"], pub["persisted"]) == ("anthropic_key", True)    # it IS still in the keychain
    assert pub["keychain"]["reason"].startswith("could not delete the saved access setting")
    assert pub["notes"][-1].startswith("keychain problem: could not delete")
    assert KEY not in json.dumps(pub)


def test_provider_switches_go_in_the_key_and_endpoint_modes_only():
    """CLAUDE_CODE_USE_BEDROCK & co. send claude to that provider whatever key or URL is set."""
    base = {**INHERITED, "CLAUDE_CODE_USE_BEDROCK": "1", "claude_code_use_vertex": "1"}
    acc = fresh(CLAUDE_CODE_USE_BEDROCK="1")
    assert acc.child_env(base)["CLAUDE_CODE_USE_BEDROCK"] == "1"              # login: the CLI's own setup
    assert any("CLAUDE_CODE_USE_BEDROCK" in n and "provider" in n for n in acc.public()["notes"])
    for set_mode in (lambda a: a.set_anthropic_key(KEY), lambda a: a.set_endpoint(GW, TOKEN, "m1")):
        set_mode(acc)
        env = acc.child_env(base)
        assert not {k.upper() for k in env} & set(keys.PROVIDER_VARS)
        assert any("CLAUDE_CODE_USE_BEDROCK" in n and "drop" in n for n in acc.public()["notes"])
    assert not any("provider" in n for n in fresh(CLAUDE_CODE_USE_BEDROCK="0").public()["notes"])


def test_an_endpoint_model_cannot_start_with_a_dash():
    """It reaches `claude --model` through ORION_MODEL, where "-x" would read as a flag."""
    with pytest.raises(AccessError):
        fresh().set_endpoint(GW, TOKEN, "-model")
