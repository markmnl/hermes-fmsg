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
