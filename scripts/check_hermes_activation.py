"""Verify fmsg activation through a real installed Hermes runtime.

Run with the target Hermes checkout on PYTHONPATH.  This intentionally uses a
temporary Hermes home, so it neither reads nor modifies a developer's agent.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile


ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-fmsg-activation-") as tmp:
        home = Path(tmp)
        plugin_dir = home / "plugins" / "fmsg-platform"
        shutil.copytree(
            ROOT,
            plugin_dir,
            ignore=shutil.ignore_patterns(".git", ".pytest_cache", "__pycache__"),
        )

        # Import Hermes only after HERMES_HOME is isolated.  The env loader
        # must run before plugin discovery: this matches gateway startup.
        os.environ["HERMES_HOME"] = str(home)
        (home / "config.yaml").write_text(
            "\n".join(
                [
                    "plugins:",
                    "  enabled:",
                    "    - fmsg-platform",
                    "  disabled: []",
                    "",
                ]
            )
        )
        (home / ".env").write_text(
            "\n".join(
                [
                    "FMSG_API_URL=https://api.example.com",
                    "FMSG_API_KEY=fmsgk_test_id_test_secret",
                    "FMSG_HOME_CHANNEL=@owner@example.com",
                    "FMSG_ALLOWED_USERS=@owner@example.com",
                    "",
                ]
            )
        )

        from hermes_cli.env_loader import load_hermes_dotenv

        load_hermes_dotenv()
        from hermes_cli.plugins import discover_plugins
        from gateway.config import load_gateway_config
        from gateway.platform_registry import platform_registry

        discover_plugins()
        entry = platform_registry.get("fmsg")
        if entry is None:
            raise RuntimeError("fmsg platform was not registered")
        config = load_gateway_config()
        platform = next((item for item in config.platforms if item.value == "fmsg"), None)
        if platform is None or not config.platforms[platform].enabled:
            raise RuntimeError("fmsg platform was not enabled from FMSG_* configuration")
        if not entry.check_fn():
            raise RuntimeError("fmsg dependencies are unavailable")
        if entry.is_connected is not None and not entry.is_connected(config.platforms[platform]):
            raise RuntimeError("fmsg credentials were not recognised")
    print("Hermes fmsg activation OK")


if __name__ == "__main__":
    main()
