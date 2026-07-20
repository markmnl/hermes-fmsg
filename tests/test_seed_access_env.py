"""First-run access-control seeding (allowlist / home channel)."""

import logging

import plugin.adapter as adapter_mod


HOME = "@alice@example.com"
OTHER = "@bob@example.com"


def test_seed_copies_home_into_allowlist_when_empty(monkeypatch, caplog):
    monkeypatch.delenv("FMSG_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("FMSG_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("FMSG_HOME_CHANNEL", HOME)

    with caplog.at_level(logging.WARNING):
        seeded = adapter_mod._seed_access_env()

    assert seeded == HOME
    assert adapter_mod.os.environ["FMSG_ALLOWED_USERS"] == HOME
    assert any("defaulting to FMSG_HOME_CHANNEL" in r.message for r in caplog.records)


def test_seed_noop_when_allowlist_already_set(monkeypatch):
    monkeypatch.setenv("FMSG_ALLOWED_USERS", OTHER)
    monkeypatch.setenv("FMSG_HOME_CHANNEL", HOME)
    monkeypatch.delenv("FMSG_ALLOW_ALL_USERS", raising=False)

    assert adapter_mod._seed_access_env() is None
    assert adapter_mod.os.environ["FMSG_ALLOWED_USERS"] == OTHER


def test_seed_noop_when_allow_all_enabled(monkeypatch):
    monkeypatch.delenv("FMSG_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("FMSG_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("FMSG_HOME_CHANNEL", HOME)

    assert adapter_mod._seed_access_env() is None
    assert not (adapter_mod.os.environ.get("FMSG_ALLOWED_USERS") or "").strip()


def test_seed_errors_when_nothing_configured(monkeypatch, caplog):
    monkeypatch.delenv("FMSG_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("FMSG_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("FMSG_HOME_CHANNEL", raising=False)

    with caplog.at_level(logging.ERROR):
        assert adapter_mod._seed_access_env() is None

    assert any("rejected as unauthorized" in r.message for r in caplog.records)


def test_register_calls_seed_before_platform_registration(monkeypatch):
    calls = []

    class FakeCtx:
        def register_platform(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.delenv("FMSG_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("FMSG_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("FMSG_HOME_CHANNEL", HOME)

    adapter_mod.register(FakeCtx())

    assert adapter_mod.os.environ["FMSG_ALLOWED_USERS"] == HOME
    assert len(calls) == 1
    assert "FMSG_HOME_CHANNEL" in calls[0]["required_env"]
    assert "FMSG_ALLOWED_USERS" in calls[0]["required_env"]
