"""Non-destructive consistency and integrity checks."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import AppConfig
from .database import connect
from .filesystem import inventory_tree, sha256_file
from .scanner import _excluded_paths
from .taxonomy import Taxonomy


def _verify_library_unlocked(config: AppConfig, taxonomy: Taxonomy, *, full_hash: bool = False) -> dict[str, Any]:
    conn = connect(config.database_path)
    errors: list[str] = []
    warnings: list[str] = []
    missing: list[str] = []
    changed_fingerprint: list[str] = []
    hash_mismatches: list[str] = []
    orphan_files: list[str] = []
    reappeared: list[str] = []
    temp_artifacts: list[str] = []
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            errors.extend(f"SQLite integrity_check: {line}" for line in integrity)
        fk_errors = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        if fk_errors:
            errors.append(f"Foreign-key violations: {fk_errors}")
        taxonomy_errors = taxonomy.validate()
        errors.extend(f"Taxonomy: {x}" for x in taxonomy_errors)
        inventory = inventory_tree(config.library_root, ignore_directories=config.ignore_directories,
                                   excluded_paths=_excluded_paths(config))
        warnings.extend(inventory.warnings)
        errors.extend(inventory.errors)
        actual_paths = {item.relative_path: item for item in inventory.files}
        for warning in inventory.warnings:
            if "temporary file" in warning:
                temp_artifacts.append(warning)
        records = conn.execute("SELECT * FROM documents").fetchall()
        indexed_present: set[str] = set()
        for record in records:
            rel = record["relative_path"]
            disk_path = config.library_root / Path(rel)
            if record["state"] == "missing":
                if rel in actual_paths:
                    reappeared.append(rel)
                continue
            indexed_present.add(rel)
            if rel not in actual_paths:
                missing.append(rel)
                continue
            item = actual_paths[rel]
            if item.size_bytes != record["size_bytes"] or item.mtime_ns != record["mtime_ns"]:
                changed_fingerprint.append(rel)
            if full_hash or item.size_bytes != record["size_bytes"] or item.mtime_ns != record["mtime_ns"] or not record["sha256"]:
                try:
                    actual_hash = sha256_file(disk_path)
                    if record["sha256"] and actual_hash != record["sha256"]:
                        hash_mismatches.append(rel)
                except OSError as exc:
                    errors.append(f"Could not hash {rel}: {exc}")
        for rel in sorted(set(actual_paths) - indexed_present):
            # A historical missing record is a reappeared file, not an unindexed orphan.
            known = conn.execute("SELECT 1 FROM documents WHERE relative_path=?", (rel,)).fetchone()
            if not known:
                orphan_files.append(rel)
        pending_imports = [dict(row) for row in conn.execute(
            "SELECT batch_id,source_relative_path,destination_relative_path,status,error FROM import_items WHERE status IN ('copying','pending','failed','unresolved') ORDER BY id")]
        invalid_subjects = [dict(row) for row in conn.execute(
            "SELECT relative_path,subject_id FROM documents WHERE subject_id IS NOT NULL AND subject_id NOT IN (SELECT id FROM subjects)")]
        # Identify old taxonomy/category assignments without rewriting them.
        deprecated_assignments = [dict(row) for row in conn.execute(
            "SELECT d.relative_path,d.subject_id,s.name FROM documents d JOIN subjects s ON s.id=d.subject_id WHERE s.deprecated=1")]
        unexpected_locations = []
        for row in conn.execute("SELECT d.relative_path,d.subject_id,s.relative_path AS subject_path FROM documents d JOIN subjects s ON s.id=d.subject_id WHERE d.state='present'"):
            if row["relative_path"].startswith("90_Review_Queue/"):
                continue
            parent = str(Path(row["relative_path"]).parent).replace("\\", "/")
            expected = row["subject_path"]
            if expected and not (row["relative_path"] == expected or row["relative_path"].startswith(expected.rstrip("/") + "/")):
                unexpected_locations.append({"relative_path": row["relative_path"], "subject_id": row["subject_id"],
                                             "expected_subject_path": expected,
                                             "note": "Suggestion only; files are never moved by verify"})
        report = {"database_integrity": integrity, "foreign_key_violations": fk_errors,
                  "taxonomy_errors": taxonomy_errors, "scan_complete": inventory.complete,
                  "inventory_count": len(inventory.files), "indexed_present_count": len(indexed_present),
                  "missing_indexed_files": missing, "changed_size_or_mtime": changed_fingerprint,
                  "checksum_mismatches": hash_mismatches, "unindexed_files": orphan_files,
                  "reappeared_files_marked_missing": reappeared, "temporary_artifacts": temp_artifacts,
                  "symlink_and_traversal_warnings": warnings, "filesystem_errors": errors,
                  "pending_or_failed_import_items": pending_imports, "invalid_subject_assignments": invalid_subjects,
                  "deprecated_subject_assignments": deprecated_assignments,
                  "unexpected_locations": unexpected_locations,
                  "exact_duplicate_groups": conn.execute(
                      "SELECT COUNT(*) FROM (SELECT sha256 FROM documents WHERE state='present' AND sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1)").fetchone()[0],
                  "full_hash_requested": full_hash}
        report["status"] = "issues_found" if any([errors, missing, changed_fingerprint, hash_mismatches, orphan_files, reappeared, pending_imports, invalid_subjects, deprecated_assignments, unexpected_locations, temp_artifacts]) else "ok"
        return report
    finally:
        conn.close()


def backup_database(database_path: Path | str, destination: Path | str) -> Path:
    source = Path(database_path).absolute()
    target = Path(destination).expanduser().absolute()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    if target.exists():
        raise FileExistsError(f"Backup destination already exists; refusing to overwrite: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    except Exception:
        dst.close()
        src.close()
        # Do not automatically delete failed backup output; leave it for inspection.
        raise
    else:
        dst.close()
        src.close()
    return target


def verify_library(config: AppConfig, taxonomy: Taxonomy, *, full_hash: bool = False) -> dict[str, Any]:
    """Run verification alongside no concurrent mutating CLI operation."""
    from .logging_utils import database_lock
    with database_lock(config.database_path):
        return _verify_library_unlocked(config, taxonomy, full_hash=full_hash)
