from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FRONTEND_COMMAND = re.compile(r"\b(?:npm\s+(?:ci|install|run)|npx)\b")
OTHER_LOCK_FILES = frozenset(
    {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "bun.lock", "bun.lockb"}
)
IGNORED_DIRECTORIES = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".nuxt",
        ".output",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "coverage",
        "dist",
        "graphify-out",
        "node_modules",
        "venv",
    }
)


def fenced_commands(path: Path) -> list[tuple[int, str]]:
    commands: list[tuple[int, str]] = []
    in_fence = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and FORBIDDEN_FRONTEND_COMMAND.search(line):
            commands.append((line_number, line.strip()))
    return commands


def unexpected_lock_files(root: Path) -> list[Path]:
    matches: list[Path] = []
    for current, directories, files in os.walk(root):
        directories[:] = sorted(
            directory for directory in directories if directory not in IGNORED_DIRECTORIES
        )
        matches.extend(
            Path(current, filename) for filename in sorted(OTHER_LOCK_FILES.intersection(files))
        )
    return matches


def main() -> int:
    errors: list[str] = []
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    if package.get("packageManager") != "pnpm@9.12.3":
        errors.append("package.json must pin packageManager to pnpm@9.12.3")

    if not (ROOT / "pnpm-lock.yaml").is_file():
        errors.append("pnpm-lock.yaml is missing")
    for path in unexpected_lock_files(ROOT):
        errors.append(f"unexpected JavaScript lock file: {path.relative_to(ROOT)}")

    workflows = ROOT / ".github" / "workflows"
    command_files = [
        ROOT / "Makefile",
        *sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml"))),
    ]
    for path in command_files:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN_FRONTEND_COMMAND.search(line):
                errors.append(f"{path.relative_to(ROOT)}:{line_number}: use pnpm: {line.strip()}")

    documentation = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in documentation:
        for line_number, line in fenced_commands(path):
            errors.append(f"{path.relative_to(ROOT)}:{line_number}: use pnpm: {line}")

    if errors:
        print("Repository baseline failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Repository baseline OK: pnpm@9.12.3, one JS lock file, no npm/npx commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
