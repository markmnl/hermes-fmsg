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
