"""Repository contracts: native commands stay native; Compose is explicit integration only."""

import json
import re
import unittest
from pathlib import Path

import yaml  # Already locked through the project's uvicorn[standard] dependency.

ROOT = Path(__file__).resolve().parents[1]
CONTAINER_COMMAND = re.compile(r"\b(?:docker|wsl(?:\.exe)?)\b|\$\(COMPOSE\)", re.I)
NATIVE_TARGETS = {
    "dev", "check", "migrate", "api", "web", "worker", "test", "test-api", "test-web",
    "lint", "build", "typecheck", "generate-client", "e2e",
}


def load_yaml(name):
    # Preserve GitHub's `on` and scalar strings without YAML 1.1 bool conversion.
    return yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class EnvironmentBoundaryTests(unittest.TestCase):
    def test_native_entrypoints_and_make_recipes_never_call_containers(self):
        for name in ("scripts/dev.py", "scripts/dev.ps1"):
            self.assertIsNone(CONTAINER_COMMAND.search((ROOT / name).read_text()), name)
        package = json.loads((ROOT / "package.json").read_text())
        for name, command in package["scripts"].items():
            self.assertIsNone(CONTAINER_COMMAND.search(command), name)
        targets = set()
        observed = set()
        for line in (ROOT / "Makefile").read_text().splitlines():
            if line and not line[0].isspace() and re.match(r"[\w .-]+:", line):
                targets = set(line.split(":", 1)[0].split())
            if line.startswith("\t") and targets & NATIVE_TARGETS:
                observed.update(targets & NATIVE_TARGETS)
                self.assertIsNone(CONTAINER_COMMAND.search(line), line)
        self.assertEqual(observed, NATIVE_TARGETS)

    def test_compose_requires_profile_and_only_mounts_named_data_volumes(self):
        compose = load_yaml("compose.yaml")
        for name, service in compose["services"].items():
            self.assertEqual(service["profiles"], ["integration"], name)
            self.assertNotIn("--reload", service.get("command", ""), name)
            self.assertNotIn("pnpm run dev", service.get("command", ""), name)
            for mount in service.get("volumes", []):
                self.assertIn(mount.split(":", 1)[0], compose["volumes"], (name, mount))
        self.assertTrue({"postgres-data", "hatchet-config", "hatchet-auth", "local-storage"}
                        <= compose["volumes"].keys())

    def test_compose_dependencies_exist_and_are_acyclic(self):
        services = load_yaml("compose.yaml")["services"]

        def visit(name, path):
            self.assertIn(name, services)
            self.assertNotIn(name, path, f"dependency cycle: {path} -> {name}")
            for dependency in services[name].get("depends_on", {}):
                visit(dependency, (*path, name))

        for name in services:
            visit(name, ())
        self.assertEqual(services["hatchet-worker"]["depends_on"]["api"]["condition"],
                         "service_healthy")

    def test_compose_builds_from_root_and_waits_for_readiness(self):
        services = load_yaml("compose.yaml")["services"]
        for name in ("api", "hatchet-worker", "web"):
            build = services[name]["build"]
            self.assertEqual(build["context"], ".")
            self.assertTrue((ROOT / build["dockerfile"]).is_file())
        for name in ("api", "web"):
            self.assertIn("healthcheck", services[name])
            self.assertTrue(all(port.startswith("127.0.0.1:") for port in services[name]["ports"]))

    def test_images_use_locked_dependencies_and_built_web(self):
        api = (ROOT / "apps/api/Dockerfile").read_text()
        web = (ROOT / "apps/web/Dockerfile").read_text()
        self.assertIn("apps/api/uv.lock", api)
        self.assertIn("uv sync --locked", api)
        self.assertNotIn("pip install", api)
        self.assertNotIn("--reload", api + web)
        self.assertIn("pnpm install --frozen-lockfile", web)
        self.assertIn("RUN pnpm --filter @video-factory/web build", web)
        self.assertIn('CMD ["node", ".output/server/index.mjs"]', web)
        excluded = set((ROOT / ".dockerignore").read_text().splitlines())
        self.assertTrue({".env", ".env.*", "**/.env", "**/.env.*", "**/.venv",
                         "**/.uv-cache", "data"} <= excluded)

    def test_native_ci_covers_language_checks_without_services(self):
        job = load_yaml(".github/workflows/ci.yml")["jobs"]["native-checks"]
        self.assertNotIn("services", job)
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIsNone(CONTAINER_COMMAND.search(commands))
        for gate in ("uv sync", "--locked", "pytest", "ruff check", "unittest discover",
                     "pnpm test", "pnpm lint", "pnpm typecheck", "pnpm build",
                     "export_openapi.py", "api-client check", "git diff --exit-code",
                     "export_ffmpeg_build.py", "contract/validate.py"):
            self.assertIn(gate, commands)

    def test_integration_ci_keeps_required_gates_and_isolates_cleanup(self):
        job = load_yaml(".github/workflows/ci.yml")["jobs"]["integration"]
        env = job["env"]
        self.assertEqual(env["COMPOSE_ENV_FILES"], ".env.compose.example")
        self.assertEqual(env["COMPOSE_PROFILES"], "integration")
        self.assertTrue(env["COMPOSE_PROJECT_NAME"].startswith("video-maker-ci-"))
        self.assertIn("github.run_id", env["COMPOSE_PROJECT_NAME"])
        self.assertIn("github.run_attempt", env["COMPOSE_PROJECT_NAME"])
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        for gate in ("alembic upgrade head", "check_database_head.py", "for run in 1 2 3",
                     "test_competing_submissions_cannot_overdraw",
                     "test_competing_batches_cannot_overdraw_wallet", "export_ffmpeg_build.py",
                     "RUN_HATCHET_INTEGRATION=1", "test_hatchet_integration.py", "test:e2e",
                     "--wait --wait-timeout"):
            self.assertIn(gate, commands)
        cleanup = [step for step in job["steps"] if "down -v" in step.get("run", "")]
        self.assertEqual(len(cleanup), 1)
        self.assertEqual(cleanup[0]["if"], "always()")
        self.assertIn('case "$COMPOSE_PROJECT_NAME"', cleanup[0]["run"])
        self.assertIn("video-maker-ci-*)", cleanup[0]["run"])

    def test_existing_required_status_requires_both_groups_to_succeed(self):
        job = load_yaml(".github/workflows/ci.yml")["jobs"]["test"]
        self.assertEqual(set(job["needs"]), {"native-checks", "integration"})
        self.assertEqual(job["if"], "always()")
        gate = job["steps"][0]
        self.assertIn("needs.native-checks.result", gate["env"]["NATIVE_RESULT"])
        self.assertIn("needs.integration.result", gate["env"]["INTEGRATION_RESULT"])
        self.assertIn('test "$NATIVE_RESULT" = success &&', gate["run"])
        self.assertIn('test "$INTEGRATION_RESULT" = success', gate["run"])


if __name__ == "__main__":
    unittest.main()
