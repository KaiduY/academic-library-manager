from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from academic_library.database import connect
from academic_library.importer import atomic_copy_no_clobber, run_import
from academic_library.scanner import scan_library


def _snapshot(root: Path):
    result = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            st = path.stat()
            result[path.relative_to(root).as_posix()] = (path.read_bytes(), st.st_size, st.st_mtime_ns)
    return result


def test_import_dry_run_leaves_source_and_library_documents_unchanged(env):
    f = env["source"] / "A Quantum Field Theory textbook.pdf"
    f.write_bytes(b"incoming"); (env["library"] / "existing.pdf").write_bytes(b"existing")
    before_source, before_lib = _snapshot(env["source"]), _snapshot(env["library"])
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=False)
    assert report["status"] == "dry-run"
    assert _snapshot(env["source"]) == before_source
    assert _snapshot(env["library"]) == before_lib


def test_imports_new_document_and_verifies_hash(env):
    f = env["source"] / "Quantum Field Theory textbook.pdf"
    f.write_bytes(b"new scholarly document")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report["counts"]["imported"] == 1
    item = report["items"][0]
    final = env["library"] / item["destination_relative_path"]
    assert final.read_bytes() == f.read_bytes()
    assert hashlib.sha256(final.read_bytes()).hexdigest() == item["source_sha256"]
    assert f.exists()
    conn = connect(env["db"])
    try:
        row = conn.execute("SELECT imported_from_path,import_batch_id,sha256 FROM documents WHERE relative_path=?", (item["destination_relative_path"],)).fetchone()
        assert row["imported_from_path"] == str(f)
        assert row["import_batch_id"] == report["batch_id"]
        assert row["sha256"] == item["source_sha256"]
    finally:
        conn.close()


def test_exact_duplicate_different_filename_skipped(env):
    original = env["library"] / "Quantum Field Theory.pdf"
    original.write_bytes(b"same bytes")
    (env["source"] / "Quantum Field Theory copy.pdf").write_bytes(b"same bytes")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report["counts"]["skipped_duplicate"] == 1
    assert len([x for x in env["library"].rglob("*") if x.is_file() and x.name == "Quantum Field Theory copy.pdf"]) == 0
    assert (env["source"] / "Quantum Field Theory copy.pdf").exists()


def test_same_filename_different_checksum_never_overwrites(env):
    original = env["library"] / "Quantum Field Theory.pdf"
    original.write_bytes(b"older edition")
    incoming = env["source"] / "Quantum Field Theory.pdf"
    incoming.write_bytes(b"different edition")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    old = original.read_bytes()
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report["counts"]["review_queued"] == 1
    assert original.read_bytes() == old
    queued = [p for p in env["library"].rglob("Quantum Field Theory.pdf") if p != original]
    assert queued and queued[0].read_bytes() == incoming.read_bytes()
    assert incoming.read_bytes() == b"different edition"


def test_different_edition_is_a_version_candidate(env):
    (env["library"] / "Quantum Groups.pdf").write_bytes(b"edition one")
    (env["source"] / "Quantum Groups 2nd edition.pdf").write_bytes(b"edition two")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report["counts"]["review_queued"] == 1
    item = report["items"][0]
    assert "Version_Conflicts" in item["destination_relative_path"]
    assert (env["library"] / "Quantum Groups.pdf").read_bytes() == b"edition one"


def test_uncertain_duplicate_preserved_in_review(env):
    (env["library"] / "Lecture Notes Quantum Groups.pdf").write_bytes(b"one")
    (env["source"] / "Lecture Notes Quantum Groups revised.pdf").write_bytes(b"two")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report["counts"]["review_queued"] == 1
    assert "90_Review_Queue" in report["items"][0]["destination_relative_path"]


def test_unicode_and_spaces_preserved(env):
    f = env["source"] / "Québec — Quantum Groups (revised).pdf"
    f.write_bytes(b"unicode")
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report["items"][0]["source_relative_path"] == f.name
    dest = env["library"] / report["items"][0]["destination_relative_path"]
    assert dest.exists() and dest.read_bytes() == b"unicode"


def test_import_refuses_source_nested_under_library(env):
    nested = env["library"] / "incoming"
    nested.mkdir()
    with pytest.raises(ValueError, match="may not be equal or nested"):
        run_import(env["config"], env["taxonomy"], nested, apply=False)


def test_import_refuses_report_in_source(env):
    (env["source"] / "new.pdf").write_bytes(b"x")
    with pytest.raises(ValueError, match="--report must not write inside"):
        run_import(env["config"], env["taxonomy"], env["source"], apply=False, report_path=env["source"] / "report.json")


def test_database_or_log_inside_source_is_refused(env):
    from dataclasses import replace
    bad_config = replace(env["config"], log_directory=env["source"] / "logs")
    with pytest.raises(ValueError, match="inside the read-only import source"):
        run_import(bad_config, env["taxonomy"], env["source"], apply=False)


def test_failed_copy_keeps_source_and_logs_failure(monkeypatch, env):
    f = env["source"] / "Quantum Field Theory book.pdf"
    f.write_bytes(b"important source")
    before = _snapshot(env["source"])
    import academic_library.importer as importer
    def fail(*args, **kwargs):
        raise OSError("simulated disk full")
    monkeypatch.setattr(importer, "atomic_copy_no_clobber", fail)
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report["counts"]["failed"] == 1
    assert _snapshot(env["source"]) == before
    assert not list(env["library"].rglob(f.name))


def test_hardlink_no_clobber_existing_destination(env):
    source = env["source"] / "source.pdf"; source.write_bytes(b"source bytes")
    destination = env["library"] / "existing.pdf"; destination.write_bytes(b"destination bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        atomic_copy_no_clobber(source, destination, digest, env["library"])
    assert destination.read_bytes() == b"destination bytes"
    assert source.read_bytes() == b"source bytes"


def test_simulated_concurrent_destination_creation_is_not_overwritten(monkeypatch, env):
    import academic_library.importer as importer
    source = env["source"] / "incoming.pdf"; source.write_bytes(b"incoming")
    dest = env["library"] / "target.pdf"
    real_link = os.link
    def race_link(src, dst, **kwargs):
        Path(dst).write_bytes(b"racer content")
        return real_link(src, dst, **kwargs)
    monkeypatch.setattr(importer.os, "link", race_link)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        atomic_copy_no_clobber(source, dest, digest, env["library"])
    assert dest.read_bytes() == b"racer content"
    assert source.read_bytes() == b"incoming"


def test_no_clobber_publication_refuses_filesystem_without_hard_links(monkeypatch, env):
    import academic_library.importer as importer
    source = env["source"] / "incoming.pdf"; source.write_bytes(b"incoming")
    dest = env["library"] / "target.pdf"
    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "unsupported")
    monkeypatch.setattr(importer.os, "link", unsupported)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(importer.ImportErrorSafe, match="does not support safe no-clobber"):
        atomic_copy_no_clobber(source, dest, digest, env["library"])
    assert not dest.exists()
    assert source.read_bytes() == b"incoming"


def test_review_queue_target_never_overwritten(env):
    f = env["source"] / "unclassified-name.pdf"
    f.write_bytes(b"one")
    report = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    target = env["library"] / report["items"][0]["destination_relative_path"]
    assert target.read_bytes() == b"one"
    # Exact rerun is deduplicated by the database checksum before queue path resolution.
    report2 = run_import(env["config"], env["taxonomy"], env["source"], apply=True)
    assert report2["counts"]["skipped_duplicate"] == 1
    assert target.read_bytes() == b"one"
