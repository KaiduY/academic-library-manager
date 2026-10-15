from __future__ import annotations

import json
import pytest

from academic_library.cli import main
from academic_library.database import connect
from academic_library.scanner import scan_library


def test_cli_help_and_initialization_workflow(env, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_end_to_end_scan_import_search_stats_verify(env, capsys):
    library_file = env["library"] / "Quantum Field Theory book.pdf"
    library_file.write_bytes(b"existing")
    assert main(["--config", str(env["config_path"]), "scan", "--full-hash"]) == 0
    new_file = env["source"] / "Introduction to Quantum Field Theory.pdf"
    new_file.write_bytes(b"new incoming")
    assert main(["--config", str(env["config_path"]), "import", "--source", str(env["source"])]) == 0
    dry_report = capsys.readouterr().out
    assert '"status": "dry-run"' in dry_report
    assert main(["--config", str(env["config_path"]), "import", "--source", str(env["source"]), "--apply"]) == 0
    assert main(["--config", str(env["config_path"]), "search", "Quantum Field Theory", "--format", "json"]) == 0
    assert "Quantum Field Theory" in capsys.readouterr().out
    assert main(["--config", str(env["config_path"]), "stats", "--format", "json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["overview"]["documents_present"] >= 2
    assert main(["--config", str(env["config_path"]), "verify", "--format", "json"]) in (0, 2)


def test_cli_taxonomy_validate_and_review(env, capsys):
    assert main(["--config", str(env["config_path"]), "taxonomy", "validate"]) == 0
    assert '"valid": true' in capsys.readouterr().out
    scan_library(env["config"], env["taxonomy"])
    assert main(["--config", str(env["config_path"]), "taxonomy", "review"]) == 0
    assert 'unclassified_documents' in capsys.readouterr().out


def test_cli_backup_and_export_refuse_overwrite(env, capsys):
    assert main(["--config", str(env["config_path"]), "backup", "--output", str(env["tmp"] / "backup.db")]) == 0
    assert main(["--config", str(env["config_path"]), "backup", "--output", str(env["tmp"] / "backup.db")]) == 2
    capsys.readouterr()
    out = env["tmp"] / "catalog.json"
    assert main(["--config", str(env["config_path"]), "export", "--output", str(out)]) == 0
    assert json.loads(out.read_text()) == []
    assert main(["--config", str(env["config_path"]), "export", "--output", str(out)]) == 2


def test_cli_search_supports_combined_filters(env, capsys):
    p = env["library"] / "Quantum Field Theory.pdf"; p.write_bytes(b"qft")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    assert main(["--config", str(env["config_path"]), "search", "Quantum", "--document-type", "textbook", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(row["filename"] == p.name for row in rows)


def test_doctor_checks_environment(env, capsys):
    assert main(["--config", str(env["config_path"]), "doctor"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out


def test_cli_can_create_initial_configuration_and_database(tmp_path, capsys):
    library = tmp_path / "existing library"
    library.mkdir()
    cfg = tmp_path / "settings" / "config.toml"
    assert main(["--config", str(cfg), "init", "--library-root", str(library)]) == 0
    out = capsys.readouterr().out
    assert "Database initialized" in out
    assert cfg.is_file()
    assert (cfg.parent / "taxonomy.toml").is_file()
    assert main(["--config", str(cfg), "taxonomy", "validate"]) == 0
    capsys.readouterr()


def test_cli_related_subject_tagging_is_explicit_and_searchable(env, capsys):
    p = env["library"] / "Algebraic Topology textbook.pdf"
    p.write_bytes(b"interdisciplinary textbook")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    assert main(["--config", str(env["config_path"]), "tag", "--path", p.name, "--subject", "math.geometry"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "dry-run"
    assert main(["--config", str(env["config_path"]), "tag", "--path", p.name, "--subject", "math.geometry", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "related_subject_added"
    assert main(["--config", str(env["config_path"]), "search", "Algebraic Topology", "--subject", "math.geometry", "--format", "json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(row["filename"] == p.name for row in rows)
    # The canonical primary subject remains unchanged; the alternate is stored separately.
    conn = connect(env["db"])
    try:
        doc = conn.execute("SELECT subject_id FROM documents WHERE relative_path=?", (p.name,)).fetchone()
        related = conn.execute("SELECT subject_id,role FROM document_subjects WHERE document_id=(SELECT id FROM documents WHERE relative_path=?)", (p.name,)).fetchall()
        assert doc["subject_id"] == "math.topology"
        assert {(r["subject_id"], r["role"]) for r in related} == {("math.topology", "primary"), ("math.geometry", "related")}
    finally:
        conn.close()


def test_taxonomy_promote_is_dry_run_until_approved_and_preserves_backup(env, capsys):
    import shutil
    candidate = env["tmp"] / "taxonomy-candidate.toml"
    text = env["config"].taxonomy_path.read_text(encoding="utf-8")
    candidate.write_text(text.replace('version = "1.0.0"', 'version = "1.0.1"'), encoding="utf-8")
    before = env["config"].taxonomy_path.read_bytes()
    assert main(["--config", str(env["config_path"]), "taxonomy", "promote", "--candidate", str(candidate)]) == 0
    assert env["config"].taxonomy_path.read_bytes() == before
    capsys.readouterr()
    assert main(["--config", str(env["config_path"]), "taxonomy", "promote", "--candidate", str(candidate), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "Promoted taxonomy 1.0.0 -> 1.0.1" in out
    backups = list(env["config"].taxonomy_path.parent.glob("taxonomy.toml.backup-*"))
    assert backups and backups[0].read_bytes() == before
    conn = connect(env["db"])
    try:
        row = conn.execute("SELECT old_version,new_version,old_config_backup_path FROM taxonomy_changes").fetchone()
        assert tuple(row[:2]) == ("1.0.0", "1.0.1")
    finally:
        conn.close()


def test_taxonomy_promotion_never_moves_existing_files(env, capsys):
    p = env["library"] / "Algebraic Topology.pdf"; p.write_bytes(b"keep path")
    scan_library(env["config"], env["taxonomy"], full_hash=True)
    candidate = env["tmp"] / "taxonomy-candidate.toml"
    text = env["config"].taxonomy_path.read_text(encoding="utf-8")
    candidate.write_text(text.replace('version = "1.0.0"', 'version = "1.0.2"'), encoding="utf-8")
    assert main(["--config", str(env["config_path"]), "taxonomy", "promote", "--candidate", str(candidate), "--apply"]) == 0
    capsys.readouterr()
    assert p.exists() and p.read_bytes() == b"keep path"
