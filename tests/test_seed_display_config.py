"""First-run display-config seeding (quiet fmsg progress noise).

fmsg has no message-edit API, so gateway progress heartbeats
("Working - N min", tool-progress bubbles, busy-ack iteration detail)
land as permanent, separate fmsg messages instead of in-place edits.
Hermes core's display_config.py has no built-in tier for the "fmsg"
platform (a third-party plugin), so without this seeding every install
inherits the noisiest global defaults. See plugin/adapter.py's
_seed_display_config docstring and the comment above _QUIET_DISPLAY_DEFAULTS.
"""

import logging
import sys
import types

import pytest

import plugin.adapter as adapter_mod


class FakeConfigModule:
    """Stand-in for hermes_cli.config with an in-memory config.yaml."""

    def __init__(self, initial=None):
        self.data = initial if initial is not None else {}
        self.save_calls = []

    def load_config(self):
        import copy

        return copy.deepcopy(self.data)

    def save_config(self, config, **kwargs):
        self.save_calls.append(config)
        self.data = config


@pytest.fixture
def fake_hermes_cli_config(monkeypatch):
    """Install a fake hermes_cli.config module for the duration of a test."""
    fake = FakeConfigModule()
    hermes_cli_pkg = types.ModuleType("hermes_cli")
    hermes_cli_pkg.__path__ = []
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config = fake.load_config
    config_mod.save_config = fake.save_config
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_mod)
    return fake


def test_seed_writes_quiet_defaults_when_unset(fake_hermes_cli_config, caplog):
    with caplog.at_level(logging.INFO):
        seeded = adapter_mod._seed_display_config()

    assert seeded is True
    assert len(fake_hermes_cli_config.save_calls) == 1
    written = fake_hermes_cli_config.data["display"]["platforms"]["fmsg"]
    assert written == adapter_mod._QUIET_DISPLAY_DEFAULTS
    assert written["busy_ack_detail"] is False
    assert written["tool_progress"] == "off"
    assert any("Seeded display.platforms.fmsg" in r.message for r in caplog.records)


def test_seed_noop_when_fmsg_platform_config_already_present(fake_hermes_cli_config):
    fake_hermes_cli_config.data = {
        "display": {"platforms": {"fmsg": {"busy_ack_detail": True}}}
    }

    seeded = adapter_mod._seed_display_config()

    assert seeded is False
    assert fake_hermes_cli_config.save_calls == []
    # The user's explicit choice is untouched.
    assert fake_hermes_cli_config.data["display"]["platforms"]["fmsg"] == {
        "busy_ack_detail": True
    }


def test_seed_noop_when_display_config_missing_entirely(fake_hermes_cli_config):
    fake_hermes_cli_config.data = {}

    seeded = adapter_mod._seed_display_config()

    assert seeded is True
    assert fake_hermes_cli_config.data["display"]["platforms"]["fmsg"] == (
        adapter_mod._QUIET_DISPLAY_DEFAULTS
    )


def test_seed_preserves_other_platform_display_config(fake_hermes_cli_config):
    fake_hermes_cli_config.data = {
        "display": {"platforms": {"telegram": {"busy_ack_detail": False}}}
    }

    seeded = adapter_mod._seed_display_config()

    assert seeded is True
    platforms = fake_hermes_cli_config.data["display"]["platforms"]
    assert platforms["telegram"] == {"busy_ack_detail": False}
    assert platforms["fmsg"] == adapter_mod._QUIET_DISPLAY_DEFAULTS


def test_seed_returns_false_and_logs_when_hermes_cli_config_unavailable(
    monkeypatch, caplog
):
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    monkeypatch.delitem(sys.modules, "hermes_cli.config", raising=False)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "hermes_cli.config" or name == "hermes_cli":
            raise ImportError("hermes_cli not installed in this environment")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with caplog.at_level(logging.DEBUG):
        seeded = adapter_mod._seed_display_config()

    assert seeded is False


def test_seed_swallows_save_failure(fake_hermes_cli_config, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        sys.modules["hermes_cli.config"], "save_config", boom
    )

    with caplog.at_level(logging.WARNING):
        seeded = adapter_mod._seed_display_config()

    assert seeded is False
    assert any("Failed to seed display.platforms.fmsg" in r.message for r in caplog.records)


def test_register_calls_seed_display_config(monkeypatch, fake_hermes_cli_config):
    calls = []

    class FakeCtx:
        def register_platform(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setenv("FMSG_ALLOWED_USERS", "@alice@example.com")
    monkeypatch.delenv("FMSG_ALLOW_ALL_USERS", raising=False)

    adapter_mod.register(FakeCtx())

    assert len(calls) == 1
    assert fake_hermes_cli_config.data["display"]["platforms"]["fmsg"] == (
        adapter_mod._QUIET_DISPLAY_DEFAULTS
    )
