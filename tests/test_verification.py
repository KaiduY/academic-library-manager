from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest

from academic_library.database import connect, utc_now
from academic_library.importer import recover_imports
from academic_library.scanner import scan_library
from academic_library.verification import backup_database, verify_library


def test_verify_finds_unindexed_orphan_file(env):
    report = scan_library(env["config"], env["taxonomy"], full_hash=True)
    (env["library"] / "not-yet-indexed.pdf").write_bytes(b"orphan")
    verify = verify_library(env["config"], env["taxonomy"], full_hash=True)
    assert "not-yet-indexed.pdf" in verify["unindexed_files"]
    assert verify["status"] == "issues_found"


def test_verify_detects_checksum_mismatch(env):
    p = env["library"] / "hash.pdf"; p.write_bytes(b"aaa")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    before = p.stat()
    p.write_bytes(b"bbb")
    os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
    report = verify_library(env["config"], env["taxonomy"], full_hash=True)
    assert "hash.pdf" in report["checksum_mismatches"]


def test_verify_reports_missing_historical_row_and_reappearance(env):
    p = env["library"] / "again.pdf"; p.write_bytes(b"x")
    scan_library(env["config"], env["taxonomy"])
    p.unlink()
    scan_library(env["config"], env["taxonomy"])
    p.write_bytes(b"x")
    report = verify_library(env["config"], env["taxonomy"])
    assert "again.pdf" in report["reappeared_files_marked_missing"]
    assert not p.is_symlink()


def test_database_backup_integrity_and_no_overwrite(env):
    target = backup_database(env["db"], env["tmp"] / "backup.sqlite3")
    assert target.is_file()
    backup = sqlite3.connect(target)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        backup.close()
    with pytest.raises(FileExistsError):
        backup_database(env["db"], target)


def test_recover_already_published_file_without_modifying_it(env):
    import uuid
    batch = uuid.uuid4().hex
    source = env["source"] / "source.pdf"; source.write_bytes(b"recovery content")
    dest_rel = "02_Physics/Quantum_Field_Theory/recovered.pdf"
    dest = env["library"] / dest_rel; dest.parent.mkdir(parents=True); dest.write_bytes(source.read_bytes())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    conn = connect(env["db"])
    try:
        with conn:
            conn.execute("INSERT INTO import_batches(id,started_at,source_root,mode,status) VALUES(?,?,?,?,?)",
                         (batch, utc_now(), str(env["source"]), "apply", "running"))
            conn.execute("INSERT INTO import_items(batch_id,source_path,source_relative_path,source_sha256,proposed_relative_path,destination_relative_path,action,status) VALUES(?,?,?,?,?,?,?,?)",
                         (batch, str(source), source.name, digest, dest_rel, dest_rel, "import", "copying"))
    finally:
        conn.close()
    report = recover_imports(env["config"], env["taxonomy"], apply=False)
    assert report["recoverable"]
    before = dest.read_bytes()
    applied = recover_imports(env["config"], env["taxonomy"], apply=True)
    assert applied["recovered"]
    assert dest.read_bytes() == before
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT state FROM documents WHERE relative_path=?", (dest_rel,)).fetchone()[0] == "present"
        assert conn.execute("SELECT status FROM import_items WHERE batch_id=?", (batch,)).fetchone()[0] == "imported_recovered"
    finally:
        conn.close()


def test_recovery_refuses_bad_destination_checksum(env):
    import uuid
    batch = uuid.uuid4().hex
    dest_rel = "recovery/bad.pdf"
    dest = env["library"] / dest_rel; dest.parent.mkdir(); dest.write_bytes(b"not expected")
    conn = connect(env["db"])
    try:
        with conn:
            conn.execute("INSERT INTO import_batches(id,started_at,source_root,mode,status) VALUES(?,?,?,?,?)",
                         (batch, utc_now(), str(env["source"]), "apply", "running"))
            conn.execute("INSERT INTO import_items(batch_id,source_path,source_relative_path,source_sha256,proposed_relative_path,destination_relative_path,action,status) VALUES(?,?,?,?,?,?,?,?)",
                         (batch, str(env["source"] / "gone.pdf"), "gone.pdf", hashlib.sha256(b"expected").hexdigest(), dest_rel, dest_rel, "import", "copying"))
    finally:
        conn.close()
    report = recover_imports(env["config"], env["taxonomy"], apply=True)
    assert report["not_recoverable"]
    assert dest.read_bytes() == b"not expected"


def test_verify_does_not_restore_or_delete_documents(env):
    p = env["library"] / "valuable.pdf"; p.write_bytes(b"valuable")
    scan_library(env["config"], env["taxonomy"])
    p.unlink()
    report = verify_library(env["config"], env["taxonomy"])
    assert "valuable.pdf" in report["missing_indexed_files"]
    assert not p.exists()
