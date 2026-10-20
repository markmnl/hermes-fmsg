"""Compatibility with newer Hermes hooks, without breaking older Hermes.

Older Hermes raises TypeError on unknown ``register_platform`` kwargs, so
each newer ``PlatformEntry`` hook is passed only when the field exists:

- ``parse_target_ref_fn`` (v0.20.1+): explicit targets such as
  ``hermes send --to fmsg:@alice@example.com`` (hermes-agent#67941).
- ``display_tier`` (v0.21.6+): built-in quiet display defaults, replacing
  the plugin's own config.yaml seeding.

Profile-scoped settings (v0.21.1+) are read through ``get_scoped_secret``.
"""

import sys
import types
from dataclasses import make_dataclass
from pathlib import Path

import pytest

import plugin.adapter as adapter_mod


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("@alice@example.com", ("@alice@example.com", None)),
        ("  @alice@example.com  ", ("@alice@example.com", None)),
        ("@Alice_Hermes@Example.com", ("@Alice_Hermes@Example.com", None)),
        ("@bob@localhost", ("@bob@localhost", None)),
    ],
)
def test_parse_target_ref_accepts_fmsg_addresses(raw, expected):
    assert adapter_mod.parse_target_ref(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "alice@example.com",  # missing leading @
        "@alice",  # missing domain
        "@alice@",
        "@@example.com",
        "@alice@example.com@evil.com",
        "@al ice@example.com",
        "Friendly name",  # left to channel-directory resolution
    ],
)
def test_parse_target_ref_rejects_other_strings(raw):
    assert adapter_mod.parse_target_ref(raw) is None


class _Ctx:
    def __init__(self):
        self.kwargs = None

    def register_platform(self, **kwargs):
        self.kwargs = kwargs


def _install_registry(monkeypatch, *fields):
    """Fake ``gateway.platform_registry`` whose PlatformEntry has ``fields``."""
    mod = types.ModuleType("gateway.platform_registry")
    mod.PlatformEntry = make_dataclass(
        "PlatformEntry", [("name", str, "")] + [(f, object, None) for f in fields]
    )
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", mod)


@pytest.fixture
def ctx(monkeypatch):
    seeded = []
    monkeypatch.setattr(adapter_mod, "_seed_access_env", lambda: None)
    monkeypatch.setattr(adapter_mod, "_seed_display_config", lambda: seeded.append(True))
    c = _Ctx()
    c.seeded = seeded
    return c


def test_latest_hermes_gets_all_hooks_and_no_config_write(monkeypatch, ctx):
    _install_registry(monkeypatch, "parse_target_ref_fn", "display_tier")

    adapter_mod.register(ctx)

    assert ctx.kwargs["parse_target_ref_fn"] is adapter_mod.parse_target_ref
    assert ctx.kwargs["display_tier"] == "low"
    assert ctx.seeded == []
    hint = ctx.kwargs["platform_hint"]
    assert "`fmsg:@alice@example.com`" in hint
    assert "cannot parse" not in hint


def test_parser_without_display_tier_still_seeds_config(monkeypatch, ctx):
    _install_registry(monkeypatch, "parse_target_ref_fn")

    adapter_mod.register(ctx)

    assert "parse_target_ref_fn" in ctx.kwargs
    assert "display_tier" not in ctx.kwargs
    assert ctx.seeded == [True]


def test_oldest_hermes_gets_no_new_kwargs(monkeypatch, ctx):
    _install_registry(monkeypatch)

    adapter_mod.register(ctx)

    assert "parse_target_ref_fn" not in ctx.kwargs
    assert "display_tier" not in ctx.kwargs
    assert ctx.seeded == [True]
    assert "cannot parse an arbitrary fmsg address" in ctx.kwargs["platform_hint"]


def test_registry_unavailable_gets_no_new_kwargs(monkeypatch, ctx):
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", None)

    adapter_mod.register(ctx)

    assert "parse_target_ref_fn" not in ctx.kwargs
    assert "display_tier" not in ctx.kwargs


def test_env_prefers_profile_scoped_secret(monkeypatch):
    shared = types.ModuleType("gateway.platforms._shared")
    scoped = {"FMSG_API_KEY": "fmsgk_profile_key"}
    shared.get_scoped_secret = lambda name, default=None: scoped.get(name, default)
    monkeypatch.setitem(sys.modules, "gateway.platforms._shared", shared)
    monkeypatch.setenv("FMSG_API_KEY", "fmsgk_default_profile_key")
    monkeypatch.setenv("FMSG_DEFAULT_TOPIC", "from default profile")

    assert adapter_mod._env("FMSG_API_KEY") == "fmsgk_profile_key"
    # A scoped miss must not borrow the default profile's process env.
    assert adapter_mod._env("FMSG_DEFAULT_TOPIC", "fallback") == "fallback"


def test_env_falls_back_to_process_env_on_older_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway.platforms._shared", None)
    monkeypatch.setenv("FMSG_API_KEY", "fmsgk_env_key")

    assert adapter_mod._env("FMSG_API_KEY") == "fmsgk_env_key"
    assert adapter_mod._env("FMSG_UNSET_FOR_TEST", "d") == "d"


def test_hermes_home_uses_active_profile(monkeypatch, tmp_path):
    consts = types.ModuleType("hermes_constants")
    consts.get_hermes_home = lambda: tmp_path / "profiles" / "work"
    monkeypatch.setitem(sys.modules, "hermes_constants", consts)

    assert adapter_mod._hermes_home() == tmp_path / "profiles" / "work"


def test_hermes_home_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert adapter_mod._hermes_home() == Path(tmp_path)
