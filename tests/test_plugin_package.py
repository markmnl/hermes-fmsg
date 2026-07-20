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


def test_install_manifest_prompts_for_home_and_allowlist():
    """Hermes only prompts requires_env — keep access control there."""
    manifest = (ROOT / "plugin.yaml").read_text()
    requires, _, optional = manifest.partition("optional_env:")
    assert "  - name: FMSG_HOME_CHANNEL\n" in requires
    assert "  - name: FMSG_ALLOWED_USERS\n" in requires
    assert "  - name: FMSG_ALLOW_ALL_USERS\n" in optional
    assert "  - name: FMSG_HOME_CHANNEL_NAME\n" in optional
    # Home channel itself must not be optional-only.
    assert "  - name: FMSG_HOME_CHANNEL\n" not in optional


def test_after_install_doc_exists():
    text = (ROOT / "after-install.md").read_text()
    assert "FMSG_ALLOWED_USERS" in text
    assert "FMSG_HOME_CHANNEL" in text
    assert "Unauthorized" in text or "unauthorized" in text.lower()


def test_platform_hint_keeps_api_credentials_inside_adapter():
    source = (ROOT / "plugin" / "adapter.py").read_text()

    assert "Do not call the fmsg Web API, read FMSG_API_KEY" in source
    assert "use FMSG_API_URL and FMSG_API_KEY" not in source
