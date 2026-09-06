"""Static safety boundaries, not substitutes for behavioral/service tests."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_COMMAND = re.compile(r"\b(?:docker|wsl(?:\.exe)?)\b|\$\(COMPOSE\)", re.I)


def test_test_entrypoints_never_launch_containers():
    package = json.loads((ROOT / "package.json").read_text())
    for name, command in package["scripts"].items():
        if name.startswith("test"):
            assert not CONTAINER_COMMAND.search(command), name
    for path in ("scripts/test.py", "scripts/dev.py", "scripts/dev.ps1"):
        assert not CONTAINER_COMMAND.search((ROOT / path).read_text()), path


def test_required_ci_runs_native_database_and_e2e_without_container_services():
    ci = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    jobs = ci["jobs"]
    native = jobs["native-checks"]
    assert native["runs-on"].startswith("windows-")
    assert "services" not in native
    commands = "\n".join(step.get("run", "") for step in native["steps"])
    assert not CONTAINER_COMMAND.search(commands)
    assert "pnpm test" in commands and "pnpm test:e2e" in commands
    assert "continue-on-error" not in str(native)
    assert jobs["test"]["needs"] == ["native-checks"]
    assert jobs["test"]["if"] == "always()"


def test_compose_cannot_be_enabled_by_default_or_mount_native_data():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    for service in compose["services"].values():
        assert service["profiles"] == ["integration"]
        for mount in service.get("volumes", []):
            if isinstance(mount, str):
                assert mount.split(":", 1)[0] in compose["volumes"]
            else:
                assert mount["type"] == "volume"
                assert mount["source"] in compose["volumes"]
