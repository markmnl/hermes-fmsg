"""Distribution contract for native ``hermes plugins install`` installs."""

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).parent.parent


def test_repository_root_is_a_loadable_plugin_package():
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.fmsg_platform_contract",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert callable(module.register)


def test_native_and_legacy_manifests_stay_in_sync():
    assert (ROOT / "plugin.yaml").read_bytes() == (ROOT / "plugin" / "plugin.yaml").read_bytes()


def test_install_manifest_prompts_for_api_url_with_hosted_default_hint():
    manifest = (ROOT / "plugin.yaml").read_text()

    assert "  - name: FMSG_API_URL\n" in manifest
    assert '    prompt: "fmsg API URL [https://api.fmsg.io]"\n' in manifest
    assert "  - name: FMSG_API_KEY\n" in manifest
    assert "    secret: true\n" in manifest


def test_platform_hint_keeps_api_credentials_inside_adapter():
    source = (ROOT / "plugin" / "adapter.py").read_text()

    assert "Do not call the fmsg Web API, read FMSG_API_KEY" in source
    assert "use FMSG_API_URL and FMSG_API_KEY" not in source


def test_community_skill_has_publishable_hermes_metadata():
    skill_path = ROOT / "skills" / "hermes-fmsg" / "SKILL.md"
    skill = skill_path.read_text()
    frontmatter = skill.split("---", 2)[1]

    assert skill.startswith("---\n")
    assert "name: hermes-fmsg\n" in frontmatter
    assert "version: v0.1.0\n" in frontmatter
    assert "author: markmnl\n" in frontmatter
    assert "license: MIT\n" in frontmatter
    assert "tags: [communication, messaging, fmsg, hermes]\n" in frontmatter
    for trigger in ("installing", "configuring", "sending", "threads", "diagnosing"):
        assert trigger in frontmatter


def test_community_skill_is_concise_and_adapter_first():
    skill = (ROOT / "skills" / "hermes-fmsg" / "SKILL.md").read_text()

    assert len(skill.splitlines()) < 100
    assert "operator needs their own fmsg address" in skill
    assert "separate agent or sub-account address" in skill
    assert "same address as\n   `FMSG_HOME_CHANNEL`" in skill
    assert "Do not read, print, request, or expose `FMSG_API_KEY`" in skill
    assert "Do not call the fmsg Web API, construct drafts" in skill
    assert "POST /fmsg/token" not in skill
    assert "POST /fmsg" not in skill
