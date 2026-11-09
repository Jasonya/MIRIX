"""Credential checks run before any Neo4j connection or destructive restore.

These unit tests load the small modules directly so they can also run without
the optional server integrations: pytest --noconftest tests/test_neo4j_credentials.py.
"""

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import dotenv
import neo4j
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, relative_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("MIRIX_NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("MIRIX_ENABLE_GRAPH_MEMORY", raising=False)
    # A forgotten guard must fail locally instead of contacting any database.
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", MagicMock(side_effect=AssertionError("unexpected connection")))
    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", MagicMock(side_effect=AssertionError("unexpected connection")))


@pytest.fixture
def settings_module(isolated_env, monkeypatch):
    return _load_module("mirix.settings", "mirix/settings.py", monkeypatch)


@pytest.fixture
def client(settings_module, monkeypatch):
    log_module = ModuleType("mirix.log")
    log_module.get_logger = logging.getLogger
    monkeypatch.setitem(sys.modules, "mirix.log", log_module)
    return _load_module("_neo4j_credentials_client", "mirix/database/neo4j_client.py", monkeypatch)


@pytest.fixture
def snapshot(isolated_env, monkeypatch):
    return _load_module("_neo4j_credentials_snapshot", "evals/memory_snapshot.py", monkeypatch)


def test_settings_have_no_password_default(settings_module):
    assert settings_module.Settings(_env_file=None).neo4j_password is None


def test_settings_read_explicit_password(settings_module, monkeypatch):
    monkeypatch.setenv("MIRIX_NEO4J_PASSWORD", "unit-test-supplied-password")
    assert settings_module.Settings(_env_file=None).neo4j_password == "unit-test-supplied-password"


def test_disabled_graph_needs_no_credentials(client):
    assert asyncio.run(client.init_neo4j_client()) is None
    neo4j.AsyncGraphDatabase.driver.assert_not_called()


@pytest.mark.parametrize("password", [None, "", "   "])
def test_enabled_graph_missing_password_never_connects(client, caplog, password):
    client.settings.enable_graph_memory = True
    client.settings.neo4j_password = password
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(client.init_neo4j_client()) is None
    assert "MIRIX_NEO4J_PASSWORD" in caplog.text
    neo4j.AsyncGraphDatabase.driver.assert_not_called()


def test_enabled_graph_uses_supplied_password(client, monkeypatch):
    client.settings.enable_graph_memory = True
    client.settings.neo4j_password = "unit-test-supplied-password"
    driver = MagicMock()
    driver.verify_connectivity = AsyncMock()
    create = MagicMock(return_value=driver)
    bootstrap = AsyncMock()
    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", create)
    monkeypatch.setattr(client, "_bootstrap_schema", bootstrap)

    assert asyncio.run(client.init_neo4j_client()) is driver
    create.assert_called_once_with(client.settings.neo4j_uri, auth=(client.settings.neo4j_user, "unit-test-supplied-password"))
    driver.verify_connectivity.assert_awaited_once()
    bootstrap.assert_awaited_once()


def test_snapshot_help_needs_no_credentials(snapshot, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["memory_snapshot.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        snapshot.main()
    assert exc.value.code == 0
    assert "Save/load Mirix memory snapshots" in capsys.readouterr().out
    neo4j.GraphDatabase.driver.assert_not_called()


def test_snapshot_pg_only_export_skips_missing_password(snapshot, capsys):
    assert snapshot._neo4j_export() == {"nodes": [], "relationships": []}
    assert "graph export skipped" in capsys.readouterr().out
    neo4j.GraphDatabase.driver.assert_not_called()


def test_graph_snapshot_requires_password_before_saving(snapshot, monkeypatch):
    monkeypatch.setenv("MIRIX_ENABLE_GRAPH_MEMORY", "true")
    pg_run = MagicMock(side_effect=AssertionError("unexpected PostgreSQL operation"))
    monkeypatch.setattr(snapshot.subprocess, "run", pg_run)
    with pytest.raises(ValueError, match="MIRIX_NEO4J_PASSWORD"):
        snapshot.save("missing-password", include_agents=False)
    pg_run.assert_not_called()
    neo4j.GraphDatabase.driver.assert_not_called()


def test_snapshot_restore_checks_password_before_pg_mutation(snapshot, monkeypatch, tmp_path):
    monkeypatch.setattr(snapshot, "SNAP_ROOT", tmp_path)
    target = tmp_path / "with-graph"
    target.mkdir()
    (target / "meta.json").write_text(json.dumps({"pg_tables": ["episodic_memory"]}))
    (target / "neo4j_graph.json").write_text(json.dumps({"nodes": [{"id": 1}], "relationships": []}))
    pg_run = MagicMock(side_effect=AssertionError("unexpected PostgreSQL mutation"))
    monkeypatch.setattr(snapshot.subprocess, "run", pg_run)

    with pytest.raises(ValueError, match="MIRIX_NEO4J_PASSWORD"):
        snapshot.load("with-graph")
    pg_run.assert_not_called()
    neo4j.GraphDatabase.driver.assert_not_called()


def test_snapshot_empty_graph_restore_needs_no_password(snapshot):
    snapshot._neo4j_import({"nodes": [], "relationships": []})
    neo4j.GraphDatabase.driver.assert_not_called()


def test_snapshot_export_uses_supplied_password(snapshot, monkeypatch):
    snapshot.NEO4J["password"] = "unit-test-supplied-password"
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    session.run.side_effect = [[{"id": 1, "labels": ["Node"], "props": {"name": "test"}}], []]
    create = MagicMock(return_value=driver)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", create)

    graph = snapshot._neo4j_export()
    assert len(graph["nodes"]) == 1
    create.assert_called_once_with(snapshot.NEO4J["uri"], auth=(snapshot.NEO4J["user"], "unit-test-supplied-password"))
    driver.close.assert_called_once()


def test_eval_reports_missing_password_without_losing_pg_stats(settings_module, monkeypatch):
    module = _load_module("_neo4j_credentials_eval", "evals/mab/_eval_db.py", monkeypatch)
    monkeypatch.setattr(module, "_pg_scalar", lambda sql: "8" if "length(" in sql else "1")
    result = module.measure_memory_size("test-user")
    assert result["flat_total_chars"] == 16
    assert result["total_tokens"] == 4
    assert "MIRIX_NEO4J_PASSWORD" in result["graph"]["error"]
    neo4j.GraphDatabase.driver.assert_not_called()


def test_eval_uses_supplied_password(settings_module, monkeypatch):
    settings_module.settings.neo4j_password = "unit-test-supplied-password"
    module = _load_module("_neo4j_credentials_eval", "evals/mab/_eval_db.py", monkeypatch)
    monkeypatch.setattr(module, "_pg_scalar", lambda sql: "0")
    driver = MagicMock()
    session = driver.session.return_value.__enter__.return_value
    session.run.return_value.single.side_effect = [
        {"nodes": 1, "node_chars": 12}, {"edges": 1, "edge_chars": 4}
    ]
    create = MagicMock(return_value=driver)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", create)

    result = module.measure_memory_size("test-user")
    assert result["graph_total_chars"] == 16
    create.assert_called_once_with(settings_module.settings.neo4j_uri, auth=(settings_module.settings.neo4j_user, "unit-test-supplied-password"))
    driver.close.assert_called_once()
