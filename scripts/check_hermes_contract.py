"""Smoke-test the plugin against an installed Hermes Agent package."""

import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parent.parent


class CaptureContext:
    def __init__(self):
        self.platform = None

    def register_platform(self, **kwargs):
        self.platform = kwargs


def main() -> None:
    # Importing the adapter exercises the real Hermes gateway interfaces.
    if "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []
        namespace.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = namespace
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.fmsg_platform_contract",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not create plugin import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    ctx = CaptureContext()
    module.register(ctx)
    platform = ctx.platform or {}
    required = {
        "name",
        "adapter_factory",
        "check_fn",
        "validate_config",
        "standalone_sender_fn",
        "allowed_users_env",
    }
    missing = sorted(required - platform.keys())
    if missing:
        raise RuntimeError(f"register_platform contract missing: {', '.join(missing)}")
    if platform["name"] != "fmsg":
        raise RuntimeError(f"unexpected platform name: {platform['name']!r}")
    print("Hermes platform contract OK")


if __name__ == "__main__":
    main()
