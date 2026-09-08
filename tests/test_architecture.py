"""Guard dependency and transaction boundaries without importing infrastructure."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "apps" / "api" / "app"


def test_application_and_infrastructure_do_not_import_http_modules():
    http_modules = {"app.api", "app.public_api", "app.http_dependencies"}
    http_modules.update(f"app.{path.stem}" for path in APP.glob("*_api.py"))
    for path in APP.glob("*.py"):
        if path.stem == "main" or f"app.{path.stem}" in http_modules:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in http_modules, (path.name, node.lineno, node.module)
            elif isinstance(node, ast.Import):
                assert not any(alias.name in http_modules for alias in node.names), path.name


def test_public_routes_delegate_transactions_to_application_services():
    for name in ("projects", "assets", "billing", "generations"):
        tree = ast.parse((APP / f"{name}_api.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"commit", "rollback", "flush", "execute", "add"}
                ), (name, node.lineno)
                assert not (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"GenerationAttempt", "OutboxEvent", "LedgerPosting"}
                ), (name, node.lineno)


def test_ledger_and_settlement_never_commit_callers_transaction():
    for name in ("ledger.py", "provider_settlement.py"):
        tree = ast.parse((APP / name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"commit", "rollback"}
            for node in ast.walk(tree)
        ), name
