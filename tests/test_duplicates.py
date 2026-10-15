from __future__ import annotations

from academic_library.database import connect
from academic_library.duplicates import duplicate_report, normalize_stem, normalized_similarity
from academic_library.scanner import scan_library


def test_normalized_stem_and_similarity():
    assert normalize_stem("Quantum Groups 2nd edition.pdf") == normalize_stem("Quantum Groups.pdf")
    assert normalized_similarity("Quantum Groups.pdf", "Quantum Groups 2nd edition.pdf") == 1.0


def test_exact_duplicate_groups_reported(env):
    (env["library"] / "a.pdf").write_bytes(b"same")
    (env["library"] / "b.pdf").write_bytes(b"same")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    conn = connect(env["db"])
    try:
        report = duplicate_report(conn)
        assert len(report["exact_duplicate_groups"]) == 1
        assert report["exact_duplicate_groups"][0]["count"] == 2
    finally:
        conn.close()


def test_different_contents_same_stem_reported_as_version_conflict(env):
    (env["library"] / "Quantum Groups.pdf").write_bytes(b"v1")
    (env["library"] / "Quantum Groups 2nd edition.pdf").write_bytes(b"v2")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    conn = connect(env["db"])
    try:
        report = duplicate_report(conn)
        assert report["potential_version_conflicts"]
    finally:
        conn.close()
