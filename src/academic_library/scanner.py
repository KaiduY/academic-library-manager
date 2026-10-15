"""Read-only library scan that updates the database but never mutates indexed files."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .database import add_operation, connect, sync_taxonomy, transaction, upsert_document, store_identifiers, utc_now
from .filesystem import inventory_tree, sha256_file
from .logging_utils import append_audit, database_lock
from .taxonomy import Taxonomy


def _excluded_paths(config: AppConfig) -> list[Path]:
    excluded = [config.database_path, Path(str(config.database_path) + "-wal"), Path(str(config.database_path) + "-shm"),
                Path(str(config.database_path) + "-journal"), Path(str(config.database_path) + ".lock"),
                config.log_directory, config.staging_directory]
    # Never exclude the library root itself if a database path is set directly beneath it.
    return excluded


def scan_library(config: AppConfig, taxonomy: Taxonomy, *, full_hash: bool = False) -> dict[str, Any]:
    if not config.library_root.is_dir():
        raise NotADirectoryError(f"Library root does not exist: {config.library_root}")
    with database_lock(config.database_path):
        conn = connect(config.database_path)
        scan_id = uuid.uuid4().hex
        started = utc_now()
        conn.execute(
            "INSERT INTO scan_runs(id,started_at,status,full_hash) VALUES(?,?,?,?)",
            (scan_id, started, "running", int(full_hash)),
        )
        conn.commit()
        try:
            sync_taxonomy(conn, taxonomy)
            inventory = inventory_tree(
                config.library_root,
                ignore_directories=config.ignore_directories,
                excluded_paths=_excluded_paths(config),
            )
            errors = list(inventory.errors)
            warnings = list(inventory.warnings)
            indexed = changed = 0
            with transaction(conn):
                for candidate in inventory.files:
                    old = conn.execute("SELECT * FROM documents WHERE relative_path=?", (candidate.relative_path,)).fetchone()
                    if old and not full_hash and old["size_bytes"] == candidate.size_bytes and old["mtime_ns"] == candidate.mtime_ns and old["sha256"]:
                        candidate.sha256 = old["sha256"]
                    else:
                        try:
                            candidate.sha256 = sha256_file(candidate.path)
                        except OSError as exc:
                            errors.append(f"Could not hash {candidate.path}: {exc}")
                            # Preserve prior database record and do not mark this scan as complete.
                            conn.execute("UPDATE documents SET last_error=? WHERE relative_path=?",
                                         (str(exc), candidate.relative_path))
                            continue
                    classification = taxonomy.classify(candidate.relative_path)
                    subject_id, doc_type = classification.subject_id, classification.document_type
                    source, confidence = classification.source, classification.confidence
                    if old and old["classification_source"] == "manual":
                        subject_id, doc_type = old["subject_id"], old["document_type"]
                        source, confidence = "manual", old["classification_confidence"]
                    if old and old["title_source"] in {"manual", "curated"}:
                        title, title_source, title_confidence = old["title"], old["title_source"], old["title_confidence"]
                    else:
                        title, title_source, title_confidence = candidate.title, candidate.title_source, candidate.title_confidence
                    doc_id, is_new, is_changed = upsert_document(
                        conn, relative_path=candidate.relative_path, filename=candidate.filename,
                        extension=candidate.extension, size_bytes=candidate.size_bytes, mtime_ns=candidate.mtime_ns,
                        sha256=candidate.sha256, file_type=candidate.file_type, title=title, title_source=title_source,
                        title_confidence=title_confidence, publication_year=candidate.publication_year,
                        publication_year_source=candidate.publication_year_source,
                        publication_year_confidence=candidate.publication_year_confidence,
                        edition=candidate.edition, edition_source=candidate.edition_source,
                        edition_confidence=candidate.edition_confidence, subject_id=subject_id, document_type=doc_type,
                        classification_source=source, classification_confidence=confidence,
                        metadata_status=candidate.metadata_status, scan_id=scan_id,
                    )
                    store_identifiers(conn, doc_id, candidate.identifiers)
                    if candidate.authors:
                        for author in candidate.authors:
                            norm = " ".join(author.casefold().split())
                            conn.execute("INSERT OR IGNORE INTO authors(normalized_name,display_name) VALUES(?,?)", (norm, author))
                            author_id = conn.execute("SELECT id FROM authors WHERE normalized_name=?", (norm,)).fetchone()[0]
                            conn.execute(
                                "INSERT OR IGNORE INTO document_authors(document_id,author_id,role,source,confidence) VALUES(?,?,?,?,?)",
                                (doc_id, author_id, "author", "embedded_pdf_metadata", 0.75),
                            )
                    indexed += 1
                    changed += int(is_new or is_changed)
                    add_operation(conn, command="scan", mode="full-hash" if full_hash else "incremental",
                                  operation="index_file", result="new" if is_new else ("changed" if is_changed else "unchanged"),
                                  source_path=candidate.relative_path, checksum=candidate.sha256,
                                  details={"size_bytes": candidate.size_bytes, "mtime_ns": candidate.mtime_ns})
                missing_marked = 0
                if inventory.complete and not errors:
                    found = {item.relative_path for item in inventory.files}
                    # If we failed to hash any file, don't mark missing to avoid destructive conclusions.
                    rows = conn.execute("SELECT id,relative_path,state FROM documents WHERE state='present'").fetchall()
                    for row in rows:
                        if row["relative_path"] not in found:
                            conn.execute("UPDATE documents SET state='missing',last_indexed_at=? WHERE id=?", (utc_now(), row["id"]))
                            conn.execute("INSERT INTO document_events(document_id,event_at,event_type,old_path,details_json) VALUES(?,?,?,?,?)",
                                         (row["id"], utc_now(), "marked_missing", row["relative_path"], "{}"))
                            add_operation(conn, command="scan", mode="full-hash" if full_hash else "incremental",
                                          operation="mark_missing", result="requires_investigation", source_path=row["relative_path"])
                            missing_marked += 1
                status = "completed" if not errors and inventory.complete else "incomplete"
                conn.execute(
                    "UPDATE scan_runs SET finished_at=?,status=?,files_seen=?,files_indexed=?,files_changed=?,missing_marked=?,errors_json=? WHERE id=?",
                    (utc_now(), status, len(inventory.files), indexed, changed, missing_marked,
                     json.dumps(errors, ensure_ascii=False), scan_id),
                )
                add_operation(conn, command="scan", mode="full-hash" if full_hash else "incremental",
                              operation="finish_scan", result=status,
                              details={"scan_id": scan_id, "files_seen": len(inventory.files), "indexed": indexed,
                                       "changed": changed, "missing_marked": missing_marked, "errors": errors, "warnings": warnings})
            try:
                append_audit(config.log_directory, {"command": "scan", "mode": "full-hash" if full_hash else "incremental",
                              "scan_id": scan_id, "status": status, "files_seen": len(inventory.files),
                              "indexed": indexed, "changed": changed, "missing_marked": missing_marked,
                              "errors": errors, "warnings": warnings})
            except OSError as log_exc:
                warnings.append(f"Could not append JSONL audit log: {log_exc}; SQLite operation log remains available")
            return {"scan_id": scan_id, "status": status, "files_seen": len(inventory.files),
                    "files_indexed": indexed, "files_changed": changed, "missing_marked": missing_marked,
                    "errors": errors, "warnings": warnings}
        except Exception as exc:
            conn.execute("UPDATE scan_runs SET finished_at=?,status='failed',errors_json=? WHERE id=?",
                         (utc_now(), json.dumps([str(exc)]), scan_id))
            conn.commit()
            try:
                add_operation(conn, command="scan", mode="full-hash" if full_hash else "incremental",
                              operation="scan_exception", result="failed", details={"scan_id": scan_id, "error": str(exc)})
                conn.commit()
            except Exception:
                pass
            raise
        finally:
            conn.close()
