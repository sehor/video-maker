import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("native_dev", ROOT / "scripts/dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


@pytest.fixture(autouse=True)
def clean_database():
    """Command safety tests never connect to or rebuild a database."""
    yield


def environment(password=""):
    return {
        "DATABASE_URL": f"postgresql+psycopg://postgres:{password}@localhost:5432/video-maker",
        "BETTER_AUTH_DATABASE_URL": f"postgresql://postgres:{password}@localhost:5432/video-maker",
    }


def test_env_is_literal_preserves_empty_password_and_does_not_create_storage(tmp_path):
    target = tmp_path / "local environment.env"
    storage = tmp_path / "storage with spaces"
    target.write_text(
        f'POSTGRES_PASSWORD=\nLITERAL=${{HOME}}$(whoami)`test`\nSTORAGE_ROOT="{storage.as_posix()}"\n',
        encoding="utf-8-sig",
    )
    loaded = dev.load_environment(target, {"HOME": "not-expanded", "PRIORITY": "shell"})
    assert loaded["POSTGRES_PASSWORD"] == ""
    assert loaded["LITERAL"] == "${HOME}$(whoami)`test`"
    assert loaded["PRIORITY"] == "shell"
    assert Path(loaded["STORAGE_ROOT"]) == storage
    assert not storage.exists()


def test_shell_overrides_file_and_relative_paths_are_rooted_at_repository(tmp_path):
    target = tmp_path / "test.env"
    target.write_text("POSTGRES_USER=old\nSTORAGE_ROOT=./data/storage\n")
    loaded = dev.load_environment(target, {"POSTGRES_USER": "new"})
    assert loaded["POSTGRES_USER"] == "new"
    assert Path(loaded["STORAGE_ROOT"]) == ROOT / "data/storage"


def test_production_environment_is_rejected(tmp_path):
    target = tmp_path / "test.env"
    target.write_text("ENVIRONMENT=production\n")
    with pytest.raises(dev.PreflightError, match="development"):
        dev.load_environment(target, {})


@pytest.mark.parametrize("password,decoded", [("", ""), ("p%40ss%3A%24%23%25", "p@ss:$#%")])
def test_database_pair_accepts_explicit_empty_and_encoded_passwords(password, decoded):
    env = {**environment(password), "POSTGRES_PASSWORD": decoded}
    assert dev.validate_database_pair(env).password == decoded


@pytest.mark.parametrize("value", [
    "not-a-url-secret", "postgresql://postgres:@localhost/db",
    "postgresql+psycopg://localhost/db", "postgresql+psycopg://postgres@localhost/db",
    "postgresql+psycopg://postgres:secret@postgres/db",
    "postgresql+psycopg://postgres:secret@localhost:not-a-port/db",
    "postgresql+psycopg://postgres:secret@localhost/db?host=another-server",
    "postgresql+psycopg://postgres:secret@localhost/db?dbname=production",
])
def test_bad_urls_are_rejected_without_leaking_credentials(value):
    with pytest.raises(dev.PreflightError) as error:
        dev.validate_database_pair({**environment(), "DATABASE_URL": value})
    assert "secret" not in str(error.value)


def test_disagreeing_clients_and_postgres_fields_fail_before_connection():
    env = environment()
    env["BETTER_AUTH_DATABASE_URL"] = env["BETTER_AUTH_DATABASE_URL"].replace("5432", "5433")
    with pytest.raises(dev.PreflightError, match="same target"):
        dev.validate_database_pair(env)
    with pytest.raises(dev.PreflightError, match="POSTGRES_USER"):
        dev.validate_database_pair({**environment(), "POSTGRES_USER": "different"})


@pytest.mark.parametrize("name", ["video-maker", "video-maker_test_backup", ""])
def test_test_database_must_be_separate_and_end_in_test(name):
    env = environment()
    env["TEST_DATABASE_URL"] = f"postgresql+psycopg://postgres:@localhost/{name}"
    with pytest.raises(dev.PreflightError):
        dev.validate_test_database(env, dev.validate_database_pair(env))


def test_development_database_with_test_suffix_is_still_protected():
    env = {key: value + "_test" for key, value in environment().items()}
    env["TEST_DATABASE_URL"] = env["DATABASE_URL"]
    with pytest.raises(dev.PreflightError, match="separate"):
        dev.validate_test_database(env, dev.validate_database_pair(env))


def test_test_database_accepts_only_explicit_isolated_target():
    env = {**environment(), "TEST_DATABASE_URL": environment()["DATABASE_URL"] + "_test"}
    target = dev.validate_test_database(env, dev.validate_database_pair(env))
    assert target.database.endswith("_test")


def test_migration_guard_refuses_unversioned_data_and_destructive_upgrade():
    with pytest.raises(dev.PreflightError, match="Unversioned"):
        dev.migration_guard((), {"projects"})
    with pytest.raises(dev.PreflightError, match="drops legacy"):
        dev.migration_guard(("0001_stage_one",), {"jobs"})
    dev.migration_guard((), set())
    dev.migration_guard((), {"user", "session", "jwks"})


def test_local_worker_is_rejected_without_launching_process(tmp_path, monkeypatch, capsys):
    target = tmp_path / "local.env"
    target.write_text("\n".join(f"{k}={v}" for k, v in environment().items()))
    monkeypatch.setattr(dev.os, "environ", {})
    calls = []
    monkeypatch.setattr(dev, "run", lambda *args, **kwargs: calls.append(args))
    assert dev.main(["--env-file", str(target), "worker"]) == 1
    assert not calls
    assert "local needs none" in capsys.readouterr().err


def test_failed_preflight_never_launches_migrations(tmp_path, monkeypatch):
    target = tmp_path / "local.env"
    target.write_text("\n".join(f"{k}={v}" for k, v in environment().items()))
    monkeypatch.setattr(dev.os, "environ", {})
    monkeypatch.setattr(dev, "check_tools", lambda env: None)
    monkeypatch.setattr(dev, "auth_check", lambda env, **kwargs: None)
    monkeypatch.setattr(dev, "inspect_database", lambda url: ((), ("head",), {"important_data"}))
    calls = []
    monkeypatch.setattr(dev, "run", lambda *args, **kwargs: calls.append(args))
    assert dev.main(["--env-file", str(target), "migrate"]) == 1
    assert not calls


def test_hatchet_file_paths_are_relative_to_repository_not_process_directory(tmp_path):
    target = tmp_path / "cloud.env"
    target.write_text(
        'HATCHET_CLIENT_TOKEN_FILE="private keys/token.txt"\n'
        'HATCHET_CLIENT_TLS_ROOT_CA_FILE="private keys/root.pem"\n'
    )
    loaded = dev.load_environment(target, {})
    assert Path(loaded["HATCHET_CLIENT_TOKEN_FILE"]) == ROOT / "private keys/token.txt"
    assert Path(loaded["HATCHET_CLIENT_TLS_ROOT_CA_FILE"]) == ROOT / "private keys/root.pem"


def test_cloud_without_credentials_fails_before_database_or_process(tmp_path, monkeypatch, capsys):
    target = tmp_path / "cloud.env"
    target.write_text("WORKFLOW_BACKEND=local\nHATCHET_CLIENT_TOKEN=   \n")
    monkeypatch.setattr(dev.os, "environ", {})

    def unexpected(*args, **kwargs):
        pytest.fail("No credentials must fail before any database or process access")

    monkeypatch.setattr(dev, "validate_database_pair", unexpected)
    monkeypatch.setattr(dev, "run", unexpected)
    assert dev.main(["--env-file", str(target), "test-hatchet"]) == 1
    assert "credentials are missing" in capsys.readouterr().err


@pytest.mark.parametrize("configuration", [
    "HATCHET_CLIENT_TOKEN=private-test-token\nHATCHET_CLIENT_TLS_STRATEGY=none",
    "HATCHET_CLIENT_TOKEN_FILE=missing-cloud-token.txt",
])
def test_cloud_invalid_configuration_fails_instead_of_skipping(
    tmp_path, monkeypatch, capsys, configuration,
):
    target = tmp_path / "cloud.env"
    target.write_text("\n".join(f"{k}={v}" for k, v in environment().items())
                      + "\n" + configuration)
    monkeypatch.setattr(dev.os, "environ", {})

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid Cloud settings must fail before database or process access")

    monkeypatch.setattr(dev, "inspect_database", unexpected)
    monkeypatch.setattr(dev, "run", unexpected)
    assert dev.main(["--env-file", str(target), "test-hatchet"]) == 1
    output = capsys.readouterr()
    assert "SKIP" not in output.out
    assert "private-test-token" not in output.err
