from __future__ import annotations

import os

import pytest

from academic_library.database import connect
from academic_library.scanner import scan_library


def test_scan_indexes_multiple_files(env):
    (env["library"] / "Quantum Field Theory Notes.pdf").write_bytes(b"first")
    (env["library"] / "Math" / "Algebra.pdf").parent.mkdir()
    (env["library"] / "Math" / "Algebra.pdf").write_bytes(b"second")
    report = scan_library(env["config"], env["taxonomy"], full_hash=True)
    assert report["status"] == "completed"
    assert report["files_seen"] == 2
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE state='present'").fetchone()[0] == 2
    finally:
        conn.close()


def test_repeat_scan_is_idempotent(env):
    (env["library"] / "A.pdf").write_bytes(b"same bytes")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    first = connect(env["db"])
    try:
        doc_id = first.execute("SELECT id FROM documents WHERE relative_path='A.pdf'").fetchone()[0]
    finally:
        first.close()
    report = scan_library(env["config"], env["taxonomy"])
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM documents WHERE relative_path='A.pdf'").fetchone()[0] == doc_id
        assert report["files_changed"] == 0
    finally:
        conn.close()


def test_scan_detects_new_file(env):
    scan_library(env["config"], env["taxonomy"])
    (env["library"] / "new.pdf").write_bytes(b"new")
    report = scan_library(env["config"], env["taxonomy"])
    assert report["files_changed"] == 1


def test_scan_detects_modified_file(env):
    p = env["library"] / "same.pdf"
    p.write_bytes(b"old")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    p.write_bytes(b"new contents")
    report = scan_library(env["config"], env["taxonomy"])
    conn = connect(env["db"])
    try:
        row = conn.execute("SELECT sha256,size_bytes FROM documents WHERE relative_path='same.pdf'").fetchone()
        assert row["size_bytes"] == len(b"new contents")
        assert report["files_changed"] == 1
    finally:
        conn.close()


def test_missing_file_marked_but_history_retained(env):
    p = env["library"] / "gone.pdf"
    p.write_bytes(b"keep record")
    scan_library(env["config"], env["taxonomy"])
    p.unlink()
    report = scan_library(env["config"], env["taxonomy"])
    conn = connect(env["db"])
    try:
        row = conn.execute("SELECT state FROM documents WHERE relative_path='gone.pdf'").fetchone()
        assert row["state"] == "missing"
        assert report["missing_marked"] == 1
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE relative_path='gone.pdf'").fetchone()[0] == 1
    finally:
        conn.close()


def test_incomplete_scan_does_not_mark_missing(monkeypatch, env):
    from academic_library import scanner
    (env["library"] / "historical.pdf").write_bytes(b"old")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    (env["library"] / "historical.pdf").unlink()
    (env["library"] / "unreadable.pdf").write_bytes(b"new")
    original = scanner.sha256_file
    def flaky(path, *args, **kwargs):
        if str(path).endswith("unreadable.pdf"):
            raise PermissionError("simulated unreadable file")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(scanner, "sha256_file", flaky)
    report = scan_library(env["config"], env["taxonomy"], full_hash=True)
    conn = connect(env["db"])
    try:
        state = conn.execute("SELECT state FROM documents WHERE relative_path='historical.pdf'").fetchone()[0]
        assert report["status"] == "incomplete"
        assert state == "present"
        assert report["missing_marked"] == 0
    finally:
        conn.close()


def test_full_hash_detects_preserved_mtime_and_size(env):
    p = env["library"] / "stable.pdf"
    p.write_bytes(b"aaa")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    old_stat = p.stat()
    p.write_bytes(b"bbb")
    os.utime(p, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    fast = scan_library(env["config"], env["taxonomy"], full_hash=False)
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT sha256 FROM documents WHERE relative_path='stable.pdf'").fetchone()[0] != __import__('hashlib').sha256(b"bbb").hexdigest()
    finally:
        conn.close()
    full = scan_library(env["config"], env["taxonomy"], full_hash=True)
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT sha256 FROM documents WHERE relative_path='stable.pdf'").fetchone()[0] == __import__('hashlib').sha256(b"bbb").hexdigest()
        assert full["status"] == "completed"
    finally:
        conn.close()


def test_scan_does_not_follow_symlink_files(env):
    target = env["tmp"] / "private.pdf"
    target.write_bytes(b"external")
    link = env["library"] / "linked.pdf"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not supported in this environment")
    report = scan_library(env["config"], env["taxonomy"])
    assert any("symlink" in w for w in report["warnings"])
    conn = connect(env["db"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE relative_path='linked.pdf'").fetchone()[0] == 0
    finally:
        conn.close()
