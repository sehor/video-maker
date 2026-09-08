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
                imported = {node.module} | {
                    f"{node.module}.{alias.name}" for alias in node.names
                }
                assert not imported & http_modules, (path.name, node.lineno, imported)
            elif isinstance(node, ast.Import):
                assert not any(alias.name in http_modules for alias in node.names), (
                    path.name
                )


def test_public_routes_delegate_transactions_to_application_services():
    for name in ("projects", "assets", "billing", "generations"):
        tree = ast.parse((APP / f"{name}_api.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert not (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {"commit", "rollback", "flush", "execute", "add"}
                ), (name, node.lineno)
                assert not (
                    isinstance(node.func, ast.Name)
                    and node.func.id
                    in {"GenerationAttempt", "OutboxEvent", "LedgerPosting"}
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


def test_execution_collaborators_cannot_depend_on_sibling_base_methods():
    modules = {
        "provider_execution_context": "AttemptContextService",
        "provider_submission": "ProviderSubmissionService",
        "provider_polling": "ProviderPollingService",
        "provider_callbacks": "ProviderCallbackService",
        "provider_completion": "ProviderCompletionService",
        "provider_settlement": "ProviderSettlementService",
        "provider_execution": "GenerationExecutionService",
    }
    for module, name in modules.items():
        tree = ast.parse((APP / f"{module}.py").read_text(encoding="utf-8"))
        cls = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == name
        )
        assert not cls.bases, name
        methods = {
            node.name
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        constructor = next(
            (node for node in cls.body if getattr(node, "name", None) == "__init__"),
            None,
        )
        provided = (
            {
                node.attr
                for node in ast.walk(constructor)
                if isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            }
            if constructor is not None
            else set()
        )
        accessed = {
            node.attr
            for node in ast.walk(cls)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        assert not accessed - methods - provided, (name, accessed - methods - provided)


def test_async_control_paths_never_open_a_session_directly():
    for filename in (
        "provider_execution.py",
        "outbox.py",
        "provider_cancel_outbox.py",
        "artifact_lifecycle.py",
        "project_cleanup.py",
        "control_plane.py",
    ):
        tree = ast.parse((APP / filename).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                name = ast.unparse(call.func)
                assert name not in {
                    "SessionLocal",
                    "self._session_factory",
                    "self.session_factory",
                    "self.sessions",
                }, (filename, node.name, call.lineno)
