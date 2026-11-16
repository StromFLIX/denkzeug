"""Upgrade cleanup must be scoped, idempotent and preserve unrelated saved work."""

import json
import sqlite3

import pytest
from nautionette_backend.db import Database
from nautionette_backend.migrations import remove_miniapps


@pytest.mark.parametrize("partial", [False, True])
def test_upgrade_removes_executable_ui_tables_and_access_records(tmp_path, partial):
    path = str(tmp_path / "upgrade.db")
    database = Database(path)
    chat = database.create_chat("Keep this conversation", "default")
    database.set_setting("unrelated", {"keep": True})
    database._conn.close()
    with sqlite3.connect(path) as old:
        old.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE mini_apps (id TEXT PRIMARY KEY, title TEXT);
            CREATE TABLE mini_app_releases (
                app_id TEXT REFERENCES mini_apps(id), revision INTEGER, manifest TEXT
            );
            CREATE TABLE mini_app_calls (app_id TEXT REFERENCES mini_apps(id), response TEXT);
            CREATE TABLE mini_app_events (app_id TEXT REFERENCES mini_apps(id), outcome TEXT);
            CREATE INDEX mini_app_events_app ON mini_app_events(app_id);
            CREATE TRIGGER mini_apps_resource_owner AFTER INSERT ON mini_apps BEGIN
                INSERT INTO access_resources VALUES('app',NEW.id,'owner'); END;
            INSERT INTO mini_apps VALUES('old','Retired interface');
            INSERT INTO mini_app_releases VALUES('old',1,'{"backend":{"source":"old code"}}');
            INSERT INTO mini_app_calls VALUES('old','old response');
            INSERT INTO mini_app_events VALUES('old','ok');
            INSERT INTO access_grants VALUES('app','old','member',0);
            INSERT INTO access_resources VALUES('workflow','keep','owner');
            INSERT INTO access_grants VALUES('workflow','keep','member',0);
            INSERT INTO api_resource_ids VALUES('app','old','retired');
            INSERT INTO api_resource_ids VALUES('workflow','keep','retained');
        """)
        if partial:
            old.execute("DROP TABLE mini_app_calls")
    for _ in range(2):
        upgraded = Database(path)
        try:
            assert not upgraded.query("SELECT name FROM sqlite_master WHERE name LIKE 'mini_app%'")
            assert not upgraded.query("SELECT 1 FROM access_resources WHERE kind='app'")
            assert not upgraded.query("SELECT 1 FROM access_grants WHERE kind='app'")
            assert upgraded.one("SELECT id FROM api_resource_ids WHERE kind='workflow'")["id"] == "retained"
            assert not upgraded.query("SELECT 1 FROM api_resource_ids WHERE kind='app'")
            assert upgraded.one("SELECT resource_id FROM access_grants")["resource_id"] == "keep"
            assert upgraded.get_chat(chat["id"])["title"] == "Keep this conversation"
            assert upgraded.get_setting("unrelated") == {"keep": True}
            assert upgraded.query("PRAGMA foreign_key_check") == []
        finally:
            upgraded._conn.close()


def test_obsolete_api_grants_are_removed_without_revoking_other_scopes():
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE api_keys(id TEXT PRIMARY KEY, grants TEXT)")
        grants = [
            {"scope": "miniapps:write", "resources": ["*"]},
            {"scope": "workflows:read", "resources": ["keep"]},
        ]
        conn.execute("INSERT INTO api_keys VALUES('mixed',?)", (json.dumps(grants),))
        conn.execute("INSERT INTO api_keys VALUES('obsolete',?)", (json.dumps(grants[:1]),))
        for _ in range(2):
            remove_miniapps(conn)
            saved = {row["id"]: json.loads(row["grants"]) for row in conn.execute("SELECT * FROM api_keys")}
            assert saved == {"mixed": grants[1:], "obsolete": []}
