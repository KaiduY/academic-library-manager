from __future__ import annotations

import sqlite3

import pytest

from academic_library.database import connect, initialize_database, sync_taxonomy


def test_initialize_schema_and_foreign_keys(env):
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"documents", "subjects", "scan_runs", "import_batches", "import_items", "operation_log", "identifiers", "authors"} <= tables
    finally:
        conn.close()


def test_reinitialization_is_idempotent(env):
    initialize_database(env["db"])
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    finally:
        conn.close()


def test_taxonomy_sync_upserts_without_deleting_old_subjects(env):
    conn = connect(env["db"])
    try:
        sync_taxonomy(conn, env["taxonomy"])
        count = conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
        sync_taxonomy(conn, env["taxonomy"])
        assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == count
    finally:
        conn.close()


def test_foreign_key_rejects_unknown_document_subject(env):
    conn = connect(env["db"])
    try:
        sync_taxonomy(conn, env["taxonomy"])
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute("INSERT INTO documents(relative_path,filename,extension,size_bytes,mtime_ns,file_type,first_seen_at,last_indexed_at,subject_id) VALUES('x','x.pdf','.pdf',0,0,'application/pdf','now','now','no-such-subject')")
    finally:
        conn.close()
