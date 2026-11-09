"""Archived graph tools must require caller-owned database credentials."""

import contextlib
import io
import os
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("draw_hypergraph", "refine_hypergraph", "visualize_v6_graph")


class ConnectionRequested(Exception):
    """Stop before any database access or graph rendering."""


class ArchiveNeo4jCredentialsTest(unittest.TestCase):
    def run_script(self, name, env, driver, arguments=()):
        neo4j = types.ModuleType("neo4j")
        neo4j.GraphDatabase = types.SimpleNamespace(driver=driver)
        matplotlib = types.ModuleType("matplotlib")
        matplotlib.__path__ = []
        matplotlib.use = Mock()
        dependencies = {
            "neo4j": neo4j,
            "matplotlib": matplotlib,
            "matplotlib.pyplot": types.ModuleType("matplotlib.pyplot"),
            "matplotlib.patches": types.ModuleType("matplotlib.patches"),
            "networkx": types.ModuleType("networkx"),
        }
        path = ROOT / "archive" / "evals" / f"{name}.py"
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, dependencies):
            with patch.object(sys, "argv", [str(path), *arguments]):
                if name == "visualize_v6_graph" and not arguments:
                    module = runpy.run_path(str(path))
                    module["graph_driver"]()
                else:
                    runpy.run_path(str(path), run_name="__main__")

    def test_missing_or_empty_password_never_attempts_connection(self):
        for name in SCRIPTS:
            for env in ({}, {"MIRIX_NEO4J_PASSWORD": ""}, {"MIRIX_NEO4J_PASSWORD": "   "}):
                with self.subTest(script=name, environment=env):
                    driver = Mock()
                    with self.assertRaisesRegex(ValueError, "Set MIRIX_NEO4J_PASSWORD"):
                        self.run_script(name, env, driver)
                    driver.assert_not_called()

    def test_caller_credentials_and_connection_details_are_used(self):
        env = {
            "MIRIX_NEO4J_URI": "neo4j+s://example.invalid:7687",
            "MIRIX_NEO4J_USER": "test-user",
            "MIRIX_NEO4J_PASSWORD": "test-only-supplied-value",
        }
        for name in SCRIPTS:
            with self.subTest(script=name):
                driver = Mock(side_effect=ConnectionRequested)
                with self.assertRaises(ConnectionRequested):
                    self.run_script(name, env, driver)
                driver.assert_called_once_with(
                    env["MIRIX_NEO4J_URI"],
                    auth=(env["MIRIX_NEO4J_USER"], env["MIRIX_NEO4J_PASSWORD"]),
                )

    def test_help_does_not_require_credentials_or_connect(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                driver = Mock()
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_context:
                        self.run_script(name, {}, driver, ("--help",))
                self.assertEqual(exit_context.exception.code, 0)
                driver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
