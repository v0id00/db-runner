"""Tests for db_runner.py"""
import asyncio
import json
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

# Stub heavy dependencies before importing db_runner
for mod in ["aiomysql", "rich", "rich.box", "rich.align", "rich.console",
            "rich.panel", "rich.progress", "rich.rule", "rich.table"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Stub rich submodules used directly
import rich
rich.box = MagicMock()
sys.modules["rich.box"] = rich.box

import importlib
import db_runner  # noqa: E402  (must come after stubs)


class TestLoadVault(unittest.TestCase):
    def test_plaintext_vault(self):
        content = b"prod-1=secret\nprod-2=other\n"
        with patch("builtins.open", mock_open(read_data=content)):
            vault = db_runner.load_vault("fake.vault")
        self.assertEqual(vault["prod-1"], "secret")
        self.assertEqual(vault["prod-2"], "other")

    def test_vault_file_not_found(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit):
                db_runner.load_vault("missing.vault")

    def test_encrypted_vault_missing_cryptography(self):
        db_runner.HAS_CRYPTOGRAPHY = False
        content = b"gAAAAsomefaketoken"
        with patch("builtins.open", mock_open(read_data=content)):
            with self.assertRaises(SystemExit):
                db_runner.load_vault("enc.vault")
        db_runner.HAS_CRYPTOGRAPHY = False  # restore


class TestLoadConnections(unittest.TestCase):
    def _make_conns(self, data):
        raw = json.dumps(data).encode()
        with patch("builtins.open", mock_open(read_data=raw.decode())):
            with patch("db_runner.find_connections_file", return_value="connections.json"):
                return db_runner.load_connections()

    def test_defaults_applied_mysql(self):
        conns = self._make_conns([{"host": "h", "user": "u", "password": "p"}])
        self.assertEqual(conns[0]["port"], 3306)
        self.assertEqual(conns[0]["db_type"], "mysql")

    def test_defaults_applied_postgresql(self):
        conns = self._make_conns([{"host": "h", "user": "u", "password": "p", "db_type": "postgresql"}])
        self.assertEqual(conns[0]["port"], 5432)

    def test_missing_fields_exits(self):
        with patch("db_runner.find_connections_file", return_value="connections.json"):
            with patch("builtins.open", mock_open(read_data=json.dumps([{"host": "h"}]))):
                with self.assertRaises(SystemExit):
                    db_runner.load_connections()

    def test_empty_list_exits(self):
        with patch("db_runner.find_connections_file", return_value="connections.json"):
            with patch("builtins.open", mock_open(read_data="[]")):
                with self.assertRaises(SystemExit):
                    db_runner.load_connections()


class TestCheckDestructive(unittest.TestCase):
    def test_no_destructive(self):
        # Should not raise or exit
        db_runner.check_destructive("SELECT 1", force=True)

    def test_destructive_force(self):
        # Should not exit with force=True
        db_runner.check_destructive("DROP TABLE foo", force=True)

    def test_destructive_confirmed(self):
        with patch("builtins.input", return_value="YES"):
            db_runner.check_destructive("DELETE FROM foo", force=False)

    def test_destructive_cancelled(self):
        with patch("builtins.input", return_value="no"):
            with self.assertRaises(SystemExit):
                db_runner.check_destructive("TRUNCATE TABLE foo", force=False)


class TestFetchAllDatabases(unittest.TestCase):
    def test_fetch_all_databases_success(self):
        conn = {"name": "s1", "host": "h", "port": 3306, "user": "u", "password": "p", "db_type": "mysql"}

        async def fake_fetch(c):
            return c["name"], ["app_db", "shop_db"], None

        with patch("db_runner.fetch_databases_for", side_effect=fake_fetch):
            result = asyncio.run(db_runner.fetch_all_databases([conn]))
        self.assertIn("s1", result)
        self.assertEqual(sorted(result["s1"]), ["app_db", "shop_db"])

    def test_fetch_all_databases_error(self):
        conn = {"name": "s1", "host": "h", "port": 3306, "user": "u", "password": "p", "db_type": "mysql"}

        async def fake_fetch(c):
            return c["name"], [], "connection refused"

        with patch("db_runner.fetch_databases_for", side_effect=fake_fetch):
            result = asyncio.run(db_runner.fetch_all_databases([conn]))
        self.assertNotIn("s1", result)


class TestExecuteOnDbDispatch(unittest.TestCase):
    def _make_conn(self, db_type="mysql"):
        return {
            "name": "s1", "host": "h", "port": 3306,
            "user": "u", "password": "p", "db_type": db_type,
        }

    def test_dispatch_mysql(self):
        conn = self._make_conn("mysql")
        sem = asyncio.Semaphore(1)
        results = []
        called = []

        async def fake_mysql(*args, **kwargs):
            called.append("mysql")

        with patch("db_runner.execute_on_db_mysql", side_effect=fake_mysql):
            asyncio.run(db_runner.execute_on_db(conn, "db", "SELECT 1", sem, results))
        self.assertEqual(called, ["mysql"])

    def test_dispatch_postgresql(self):
        conn = self._make_conn("postgresql")
        sem = asyncio.Semaphore(1)
        results = []
        called = []

        async def fake_pg(*args, **kwargs):
            called.append("pg")

        with patch("db_runner.execute_on_db_pg", side_effect=fake_pg):
            asyncio.run(db_runner.execute_on_db(conn, "db", "SELECT 1", sem, results))
        self.assertEqual(called, ["pg"])


class TestExecuteOnDbMysqlDryRun(unittest.TestCase):
    def test_dry_run(self):
        conn = {"name": "s1", "host": "h", "port": 3306, "user": "u", "password": "p"}
        sem = asyncio.Semaphore(1)
        results = []
        asyncio.run(db_runner.execute_on_db_mysql(
            conn, "mydb", "SELECT 1", sem, results, dry_run=True
        ))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "DRY")


class TestSystemDBsFilter(unittest.TestCase):
    def test_mysql_system_dbs_excluded(self):
        for db in ["information_schema", "mysql", "performance_schema", "sys", "innodb"]:
            self.assertIn(db, db_runner.SYSTEM_DBS)

    def test_pg_system_dbs_excluded(self):
        for db in ["postgres", "template0", "template1"]:
            self.assertIn(db, db_runner.SYSTEM_DBS_PG)


class TestStartStopSshTunnels(unittest.TestCase):
    def test_no_ssh_conns(self):
        conns = [{"name": "s1", "host": "h", "port": 3306, "user": "u", "password": "p"}]
        tunnels = db_runner.start_ssh_tunnels(conns)
        self.assertEqual(tunnels, [])

    def test_ssh_without_sshtunnel_exits(self):
        db_runner.HAS_SSHTUNNEL = False
        conns = [{"name": "s1", "host": "h", "port": 3306, "user": "u", "password": "p",
                  "ssh_tunnel": {"host": "jump.example.com"}}]
        with self.assertRaises(SystemExit):
            db_runner.start_ssh_tunnels(conns)
        db_runner.HAS_SSHTUNNEL = False  # restore

    def test_stop_ssh_tunnels_handles_errors(self):
        t = MagicMock()
        t.stop.side_effect = Exception("boom")
        db_runner.stop_ssh_tunnels([(t, "s1")])  # should not raise


class TestParseServerDbLine(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(db_runner.parse_server_db_line("server1.mydb"), ("server1", "mydb"))

    def test_dotted_server(self):
        self.assertEqual(db_runner.parse_server_db_line("db.server.com.mydb"), ("db.server.com", "mydb"))

    def test_comment(self):
        self.assertIsNone(db_runner.parse_server_db_line("# comment"))

    def test_empty(self):
        self.assertIsNone(db_runner.parse_server_db_line(""))


if __name__ == "__main__":
    unittest.main()
