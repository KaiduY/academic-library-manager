from __future__ import annotations

from pathlib import Path
import shutil
import tomllib

import pytest

from academic_library.taxonomy import Taxonomy, TaxonomyError


def test_bundled_taxonomy_is_valid(env):
    assert env["taxonomy"].validate() == []
    assert env["taxonomy"].version


def test_classifies_qft_and_operational_material(env):
    tax = env["taxonomy"]
    classification = tax.classify("Quantum Field Theory/Quantum Field Theory Lecture Notes.pdf")
    assert classification.subject_id == "physics.qft"
    assert classification.document_type == "lecture_notes"


def test_unknown_material_goes_to_review(env):
    dest, classification = env["taxonomy"].destination_for("odd-file-ϕ.pdf")
    assert classification.subject_id == env["taxonomy"].review_subject
    assert dest.startswith("90_Review_Queue/Unclassified/")


def test_duplicate_subject_ids_rejected(env, tmp_path):
    text = env["config"].taxonomy_path.read_text(encoding="utf-8")
    bad = tmp_path / "duplicate.toml"
    bad.write_text(text + '\n[[subjects]]\nid = "physics"\nname = "Duplicate"\nparent_id = ""\npath = "99_Dupe"\nkeywords = []\ndeprecated = false\n', encoding="utf-8")
    with pytest.raises(TaxonomyError, match="duplicate subject IDs"):
        Taxonomy.load(bad)


def test_circular_subject_hierarchy_rejected(env, tmp_path):
    bad = tmp_path / "cycle.toml"
    bad.write_text('''version = "x"\nreview_subject = "a"\ndocument_types=["other"]\n[[subjects]]\nid="a"\nname="A"\nparent_id="b"\npath="A"\nkeywords=[]\n[[subjects]]\nid="b"\nname="B"\nparent_id="a"\npath="A/B"\nkeywords=[]\n''', encoding="utf-8")
    with pytest.raises(TaxonomyError):
        Taxonomy.load(bad)


def test_unsafe_relative_destination_rejected(env, tmp_path):
    bad = tmp_path / "unsafe.toml"
    bad.write_text('''version = "x"\nreview_subject = "a"\ndocument_types=["other"]\n[[subjects]]\nid="a"\nname="A"\nparent_id=""\npath="../escape"\nkeywords=[]\n''', encoding="utf-8")
    with pytest.raises(TaxonomyError):
        Taxonomy.load(bad)


def test_taxonomy_review_report_does_not_mutate_files(env):
    from academic_library.cli import _taxonomy_review
    from academic_library.database import connect
    doc = env["library"] / "strange.pdf"
    doc.write_bytes(b"not pdf, preserved")
    before = (doc.read_bytes(), doc.stat().st_mtime_ns)
    from academic_library.scanner import scan_library
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    conn = connect(env["db"])
    try:
        report = _taxonomy_review(conn, env["taxonomy"])
    finally:
        conn.close()
    assert report["unclassified_documents"]
    assert (doc.read_bytes(), doc.stat().st_mtime_ns) == before
