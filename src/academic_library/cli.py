"""Command-line interface for Academic Library Manager."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import DEFAULT_CONFIG, AppConfig, create_config, load_config
from .database import add_operation, add_related_subject, connect, initialize_database, sync_taxonomy, transaction, utc_now
from .duplicates import duplicate_report
from .importer import recover_imports, run_import
from .logging_utils import LockUnavailable, append_audit, database_lock
from .scanner import scan_library
from .statistics import collect_stats, export_stats
from .taxonomy import Taxonomy, TaxonomyError
from .verification import backup_database, verify_library


class UserError(RuntimeError):
    pass


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _safe_audit(config: AppConfig, event: dict[str, Any]) -> None:
    try:
        append_audit(config.log_directory, event)
    except OSError as exc:
        # SQLite's operation_log remains the primary in-application audit record; expose JSONL failure clearly.
        print(f"warning: could not append JSONL audit log: {exc}", file=sys.stderr)


def _load_context(config_path: str | None) -> tuple[AppConfig, Taxonomy]:
    config = load_config(config_path)
    taxonomy = Taxonomy.load(config.taxonomy_path)
    return config, taxonomy


def _taxonomy_diff(current: Taxonomy, candidate: Taxonomy, conn: sqlite3.Connection) -> dict[str, Any]:
    old_ids, new_ids = set(current.subjects), set(candidate.subjects)
    changed = []
    for subject_id in sorted(old_ids & new_ids):
        old, new = current.subjects[subject_id], candidate.subjects[subject_id]
        if (old.name, old.parent_id, old.path, old.deprecated, old.keywords) != (new.name, new.parent_id, new.path, new.deprecated, new.keywords):
            impacted = conn.execute("SELECT COUNT(*) FROM documents WHERE subject_id=?", (subject_id,)).fetchone()[0]
            changed.append({"id": subject_id, "old": {"name": old.name, "parent_id": old.parent_id, "path": old.path,
                                                        "deprecated": old.deprecated, "keywords": list(old.keywords)},
                            "new": {"name": new.name, "parent_id": new.parent_id, "path": new.path,
                                    "deprecated": new.deprecated, "keywords": list(new.keywords)},
                            "indexed_documents_affected": impacted})
    rules_changed = current.type_keywords != candidate.type_keywords
    document_types_changed = current.document_types != candidate.document_types
    return {"old_version": current.version, "new_version": candidate.version,
            "added_subjects": [{"id": x.id, "name": x.name, "path": x.path} for x in candidate.subjects.values() if x.id not in old_ids],
            "removed_subjects": [{"id": x.id, "name": x.name, "path": x.path} for x in current.subjects.values() if x.id not in new_ids],
            "changed_subjects": changed, "document_type_rules_changed": rules_changed,
            "document_types_changed": document_types_changed,
            "review_count_if_rules_change": conn.execute("SELECT COUNT(*) FROM documents WHERE state='present'").fetchone()[0] if rules_changed or document_types_changed else 0,
            "warning": "This report proposes no file moves or edits. Existing document paths are left untouched."}


def _taxonomy_review(conn: sqlite3.Connection, taxonomy: Taxonomy) -> dict[str, Any]:
    unclassified = [dict(r) for r in conn.execute("SELECT relative_path,filename,title,sha256 FROM documents WHERE state='present' AND (subject_id IS NULL OR subject_id=?) ORDER BY relative_path", (taxonomy.review_subject,))]
    deprecated = [dict(r) for r in conn.execute("SELECT relative_path,subject_id FROM documents WHERE state='present' AND subject_id IN (SELECT id FROM subjects WHERE deprecated=1) ORDER BY relative_path")]
    low_confidence = [dict(r) for r in conn.execute("SELECT relative_path,subject_id,classification_confidence FROM documents WHERE state='present' AND classification_confidence < 0.5 ORDER BY classification_confidence,relative_path")]
    # Token counts are only candidate topics, not claims about contents.
    import re
    tokens: dict[str, int] = {}
    for item in unclassified:
        words = re.findall(r"[a-zA-Z]{4,}", item["filename"].casefold())
        for word in set(words):
            if word not in {"introduction", "lecture", "notes", "solution", "manual", "edition", "volume", "book", "textbook", "copy", "scan", "complete"}:
                tokens[word] = tokens.get(word, 0) + 1
    repeated_tokens = [{"token": token, "unclassified_files": count, "suggestion": "Review whether a subject keyword or category is needed"}
                       for token, count in sorted(tokens.items(), key=lambda pair: (-pair[1], pair[0])) if count >= 3][:50]
    path_counts = conn.execute("SELECT subject_id,COUNT(*) AS n FROM documents WHERE state='present' GROUP BY subject_id ORDER BY n DESC").fetchall()
    import itertools
    redundant = []
    active_subjects = [subject for subject in taxonomy.subjects.values() if not subject.deprecated]
    for left, right in itertools.combinations(active_subjects, 2):
        # Do not compare a parent with its direct or indirect child; their shared language is expected.
        def ancestor_of(a_id, b_id):
            current = taxonomy.subjects.get(b_id)
            while current and current.parent_id:
                if current.parent_id == a_id:
                    return True
                current = taxonomy.subjects.get(current.parent_id)
            return False
        if ancestor_of(left.id, right.id) or ancestor_of(right.id, left.id):
            continue
        a, b = set(left.keywords), set(right.keywords)
        common = sorted(a & b)
        denominator = min(len(a), len(b))
        if denominator >= 2 and len(common) / denominator >= 0.65:
            redundant.append({"subject_ids": [left.id, right.id], "names": [left.name, right.name],
                              "shared_keywords": common, "recommendation": "Review whether these categories need clearer boundaries"})
    return {"taxonomy_version": taxonomy.version, "unclassified_documents": unclassified,
            "deprecated_assignments": deprecated, "low_confidence_assignments": low_confidence,
            "repeated_filename_tokens": repeated_tokens, "possible_redundant_categories": redundant,
            "category_usage": [{"subject_id": r["subject_id"], "count": r["n"]} for r in path_counts],
            "note": "Recommendations are based on paths, filenames, and configured keywords only. No documents or taxonomy files were changed."}


def _search(config: AppConfig, args: argparse.Namespace) -> int:
    conn = connect(config.database_path)
    try:
        sql = """SELECT d.*,s.name AS subject_name,
          GROUP_CONCAT(DISTINCT a.display_name) AS authors,
          GROUP_CONCAT(DISTINCT i.kind || ':' || i.value) AS identifiers
          FROM documents d LEFT JOIN subjects s ON s.id=d.subject_id
          LEFT JOIN document_authors da ON da.document_id=d.id LEFT JOIN authors a ON a.id=da.author_id
          LEFT JOIN identifiers i ON i.document_id=d.id WHERE 1=1"""
        params: list[Any] = []
        if args.query:
            like = f"%{args.query}%"
            sql += " AND (d.title LIKE ? OR d.filename LIKE ? OR d.relative_path LIKE ? OR s.name LIKE ? OR EXISTS (SELECT 1 FROM document_authors da2 JOIN authors a2 ON a2.id=da2.author_id WHERE da2.document_id=d.id AND a2.display_name LIKE ?) OR EXISTS (SELECT 1 FROM identifiers i2 WHERE i2.document_id=d.id AND i2.value LIKE ?))"
            params.extend([like] * 6)
        filters = [("subject_id", args.subject), ("document_type", args.document_type), ("publication_year", args.year), ("sha256", args.checksum)]
        for field, value in filters:
            if value is not None:
                if field == "subject_id":
                    sql += " AND (d.subject_id=? OR EXISTS (SELECT 1 FROM document_subjects ds WHERE ds.document_id=d.id AND ds.subject_id=?))"
                    params.extend([value, value])
                else:
                    sql += f" AND d.{field}=?"
                    params.append(value)
        if args.author:
            sql += " AND EXISTS (SELECT 1 FROM document_authors da3 JOIN authors a3 ON a3.id=da3.author_id WHERE da3.document_id=d.id AND a3.display_name LIKE ?)"
            params.append(f"%{args.author}%")
        if args.identifier:
            sql += " AND EXISTS (SELECT 1 FROM identifiers i3 WHERE i3.document_id=d.id AND (i3.value LIKE ? OR i3.normalized_value LIKE ?))"
            params.extend([f"%{args.identifier}%", f"%{args.identifier.casefold()}%"])
        if args.path:
            sql += " AND d.relative_path LIKE ?"
            params.append(f"%{args.path}%")
        if args.import_batch:
            sql += " AND d.import_batch_id=?"
            params.append(args.import_batch)
        if args.include_missing:
            pass
        else:
            sql += " AND d.state='present'"
        sql += " GROUP BY d.id ORDER BY d.title COLLATE NOCASE,d.relative_path LIMIT ?"
        params.append(args.limit)
        rows = [dict(row) for row in conn.execute(sql, params)]
        if args.format == "json":
            _print_json(rows)
        else:
            if not rows:
                print("No matching documents.")
            for row in rows:
                print(f"{row['relative_path']}\n  title: {row['title'] or '(unknown)'} | subject: {row['subject_name'] or row['subject_id'] or '(unassigned)'} | type: {row['document_type'] or '(unknown)'}")
                if row.get("authors"):
                    print(f"  authors: {row['authors']}")
                if row.get("publication_year"):
                    print(f"  year: {row['publication_year']}")
                if row.get("identifiers"):
                    print(f"  identifiers: {row['identifiers']}")
                print(f"  sha256: {row['sha256'] or '(not calculated)'}")
        return 0
    finally:
        conn.close()


def _export_catalog(config: AppConfig, fmt: str, output: Path) -> None:
    conn = connect(config.database_path)
    try:
        rows = [dict(row) for row in conn.execute("""SELECT d.*,s.name AS subject_name,
            (SELECT GROUP_CONCAT(a.display_name, '; ') FROM document_authors da JOIN authors a ON a.id=da.author_id WHERE da.document_id=d.id) AS authors,
            (SELECT GROUP_CONCAT(i.kind || ':' || i.value, '; ') FROM identifiers i WHERE i.document_id=d.id) AS identifiers
            FROM documents d LEFT JOIN subjects s ON s.id=d.subject_id ORDER BY d.relative_path""")]
    finally:
        conn.close()
    output = output.expanduser().absolute()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing export: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    else:
        with output.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys() if rows else ["relative_path"])
            writer.writeheader()
            writer.writerows(rows)
    print(f"Exported {len(rows)} catalog records to {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="academic-library", description="Non-destructive academic library catalog and maintenance tool")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help=f"TOML config path (default: {DEFAULT_CONFIG})")
    parser.add_argument("--version", action="version", version=f"academic-library {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create configuration if needed and initialize the SQLite catalog")
    p.add_argument("--library-root", help="Existing Academic_Library directory (required when creating a config)")
    p.add_argument("--database", help="Database path (optional; only used when creating a config)")
    p.add_argument("--taxonomy", help="Taxonomy TOML path (optional; only used when creating a config)")
    p.add_argument("--staging-directory", help="Default staging directory")
    p.add_argument("--log-directory", help="Persistent log directory")

    p = sub.add_parser("scan", help="Index/update the catalog without modifying library files")
    p.add_argument("--full-hash", action="store_true", help="Recompute SHA-256 for every indexed file")

    p = sub.add_parser("import", help="Preview or add new files from a read-only source directory")
    p.add_argument("--source", required=True, help="Source directory; never modified by this tool")
    p.add_argument("--apply", action="store_true", help="Perform copy-only import; never overwrites or deletes")
    p.add_argument("--report", type=Path, help="Write a JSON report to a new file (will not overwrite)")

    p = sub.add_parser("duplicates", help="Report exact duplicate groups and possible variants")
    p.add_argument("--format", choices=["text", "json"], default="text")

    p = sub.add_parser("stats", help="Generate collection statistics")
    p.add_argument("--format", choices=["text", "json", "csv"], default="text")
    p.add_argument("--output", type=Path, help="Write report to a new file; refuses overwrite")

    p = sub.add_parser("verify", help="Compare filesystem, catalog, hashes, taxonomy and import journal")
    p.add_argument("--full-hash", action="store_true", help="Rehash all indexed files; more expensive but stronger")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--output", type=Path, help="Write JSON report to a new file")

    p = sub.add_parser("search", help="Search catalog metadata with optional combined filters")
    p.add_argument("query", nargs="?", help="Text in title, filename, path, subject, author, or identifier")
    p.add_argument("--subject")
    p.add_argument("--document-type")
    p.add_argument("--year", type=int)
    p.add_argument("--author")
    p.add_argument("--identifier")
    p.add_argument("--checksum")
    p.add_argument("--path")
    p.add_argument("--import-batch")
    p.add_argument("--include-missing", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--format", choices=["text", "json"], default="text")

    p = sub.add_parser("doctor", help="Check configuration and database environment")
    p = sub.add_parser("status", help="Summarize latest scan, import batches and review backlog")

    p = sub.add_parser("tag", help="Add a related subject assignment for an interdisciplinary document")
    p.add_argument("--path", required=True, help="Document path relative to Academic_Library")
    p.add_argument("--subject", required=True, help="Subject identifier from taxonomy.toml")
    p.add_argument("--apply", action="store_true", help="Persist this related subject assignment; default is preview")

    p = sub.add_parser("backup", help="Create a consistent non-overwriting SQLite backup")
    p.add_argument("--output", type=Path, help="Backup destination; defaults to a timestamped file under backup directory")

    p = sub.add_parser("export", help="Export catalog metadata as JSON or CSV")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("recover", help="Inspect or reconcile import journal after interruption")
    p.add_argument("--apply", action="store_true", help="Record verified already-published files; does not change files")

    p = sub.add_parser("taxonomy", help="Validate and review the editable taxonomy")
    tax_sub = p.add_subparsers(dest="taxonomy_command", required=True)
    q = tax_sub.add_parser("validate", help="Validate taxonomy structure and configuration")
    q.add_argument("--candidate", type=Path, help="Validate a proposed taxonomy instead of the active one")
    q = tax_sub.add_parser("review", help="Suggest taxonomy improvements without changing taxonomy or files")
    q.add_argument("--output", type=Path, help="Write JSON recommendations to a new file")
    q = tax_sub.add_parser("compare", help="Compare active taxonomy with a candidate file")
    q.add_argument("--candidate", type=Path, required=True)
    q.add_argument("--output", type=Path, help="Write diff report to a new file")
    q = tax_sub.add_parser("promote", help="Promote a reviewed taxonomy, preserving a timestamped backup")
    q.add_argument("--candidate", type=Path, required=True)
    q.add_argument("--apply", action="store_true", help="Actually promote candidate; default is report-only")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            cfg_path = Path(args.config).expanduser().absolute()
            if not cfg_path.exists():
                if not args.library_root:
                    raise UserError("--library-root is required when creating the initial configuration")
                create_config(cfg_path, args.library_root, args.database, args.taxonomy,
                              args.staging_directory, args.log_directory)
                print(f"Created configuration: {cfg_path}")
            elif any([args.library_root, args.database, args.taxonomy, args.staging_directory, args.log_directory]):
                raise UserError("A config already exists; edit it explicitly rather than passing path overrides to init")
            config = load_config(cfg_path)
            taxonomy = Taxonomy.load(config.taxonomy_path)
            initialize_database(config.database_path)
            conn = connect(config.database_path)
            try:
                sync_taxonomy(conn, taxonomy)
                add_operation(conn, command="init", mode="apply", operation="initialize_database", result="ok",
                              details={"library_root": str(config.library_root), "taxonomy_version": taxonomy.version})
                conn.commit()
            finally:
                conn.close()
            config.log_directory.mkdir(parents=True, exist_ok=True)
            _safe_audit(config, {"command": "init", "status": "ok", "library_root": str(config.library_root),
                          "database_path": str(config.database_path), "taxonomy_version": taxonomy.version})
            print(f"Database initialized: {config.database_path}\nLibrary root: {config.library_root}\nTaxonomy: {config.taxonomy_path}")
            print("Next: academic-library --config PATH scan --full-hash")
            return 0

        config = load_config(args.config)
        if args.command == "doctor":
            checks = []
            checks.append({"check": "library_root_exists", "ok": config.library_root.is_dir(), "path": str(config.library_root)})
            if config.library_root.exists():
                managed_paths = [config.database_path, config.log_directory, config.staging_directory]
                inside = []
                for managed_path in managed_paths:
                    try:
                        managed_path.resolve(strict=False).relative_to(config.library_root.resolve(strict=True))
                        inside.append(str(managed_path))
                    except ValueError:
                        pass
                checks.append({"check": "managed_state_outside_library", "ok": not inside, "paths_inside_library": inside,
                               "recommendation": "Keep database, logs, and staging outside Academic_Library to prevent indexing operational files."})
            checks.append({"check": "taxonomy_exists", "ok": config.taxonomy_path.is_file(), "path": str(config.taxonomy_path)})
            checks.append({"check": "database_exists", "ok": config.database_path.is_file(), "path": str(config.database_path)})
            checks.append({"check": "database_parent_writable", "ok": config.database_path.parent.exists() and os.access(config.database_path.parent, os.W_OK), "path": str(config.database_path.parent)})
            taxonomy = Taxonomy.load(config.taxonomy_path)
            checks.append({"check": "taxonomy_valid", "ok": not taxonomy.validate(), "version": taxonomy.version, "errors": taxonomy.validate()})
            if config.database_path.exists():
                conn = connect(config.database_path)
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    checks.append({"check": "database_integrity", "ok": integrity == "ok", "result": integrity})
                finally:
                    conn.close()
            _print_json({"status": "ok" if all(x["ok"] for x in checks) else "issues_found", "checks": checks})
            return 0 if all(x["ok"] for x in checks) else 2

        if args.command == "taxonomy":
            if args.taxonomy_command == "validate":
                candidate_path = args.candidate or config.taxonomy_path
                candidate = Taxonomy.load(candidate_path)
                errors = candidate.validate()
                _print_json({"valid": not errors, "path": str(candidate_path), "version": candidate.version, "errors": errors})
                return 0 if not errors else 2
            taxonomy = Taxonomy.load(config.taxonomy_path)
            if args.taxonomy_command == "review":
                conn = connect(config.database_path)
                try:
                    report = _taxonomy_review(conn, taxonomy)
                finally:
                    conn.close()
                if args.output:
                    output = args.output.expanduser().absolute()
                    if output.exists():
                        raise FileExistsError(f"Refusing to overwrite: {output}")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                _print_json(report)
                return 0
            if args.taxonomy_command in {"compare", "promote"}:
                candidate = Taxonomy.load(args.candidate)
                conn = connect(config.database_path)
                try:
                    diff = _taxonomy_diff(taxonomy, candidate, conn)
                finally:
                    conn.close()
                if args.taxonomy_command == "compare":
                    if args.output:
                        output = args.output.expanduser().absolute()
                        if output.exists():
                            raise FileExistsError(f"Refusing to overwrite: {output}")
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_text(json.dumps(diff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    _print_json(diff)
                    return 0
                if not args.apply:
                    diff["mode"] = "dry-run"
                    diff["next_step"] = "Review this diff, then rerun with --apply to promote; no files have been changed."
                    _print_json(diff)
                    return 0
                with database_lock(config.database_path):
                    current_path = config.taxonomy_path
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup_path = current_path.with_name(current_path.name + f".backup-{stamp}")
                    if backup_path.exists():
                        raise FileExistsError(f"Taxonomy backup already exists: {backup_path}")
                    shutil.copy2(current_path, backup_path)
                    with tempfile.NamedTemporaryFile("wb", dir=current_path.parent, prefix=".taxonomy-update-", delete=False) as tmp:
                        temp_path = Path(tmp.name)
                        with args.candidate.open("rb") as origin:
                            shutil.copyfileobj(origin, tmp)
                        tmp.flush()
                        os.fsync(tmp.fileno())
                    try:
                        # --apply is explicit, and old config was copied to a new, preserved backup path.
                        os.replace(temp_path, current_path)
                        promoted = Taxonomy.load(current_path)
                        conn = connect(config.database_path)
                        try:
                            with transaction(conn):
                                sync_taxonomy(conn, promoted)
                                conn.execute("INSERT INTO taxonomy_changes(changed_at,old_version,new_version,old_config_backup_path,new_config_path,change_summary_json) VALUES(?,?,?,?,?,?)",
                                             (utc_now(), taxonomy.version, promoted.version, str(backup_path), str(current_path), json.dumps(diff, ensure_ascii=False)))
                                add_operation(conn, command="taxonomy promote", mode="apply", operation="promote_taxonomy", result="ok",
                                              details={"old_version": taxonomy.version, "new_version": promoted.version,
                                                       "backup_path": str(backup_path), "diff": diff})
                        finally:
                            conn.close()
                        _safe_audit(config, {"command": "taxonomy promote", "mode": "apply", "status": "ok",
                                      "old_version": taxonomy.version, "new_version": promoted.version,
                                      "backup_path": str(backup_path)})
                    except Exception:
                        # Restore the prior active config from its separately preserved backup if promotion failed.
                        if temp_path.exists():
                            temp_path.unlink()
                        if backup_path.exists():
                            shutil.copy2(backup_path, current_path)
                        _safe_audit(config, {"command": "taxonomy promote", "mode": "apply", "status": "failed",
                                      "backup_path": str(backup_path)})
                        raise
                print(f"Promoted taxonomy {taxonomy.version} -> {candidate.version}; previous config preserved at {backup_path}")
                print("Existing documents were not moved or renamed. Run taxonomy review and verify to inspect assignments.")
                return 0

        taxonomy = Taxonomy.load(config.taxonomy_path)
        if args.command == "tag":
            relative = Path(args.path)
            if relative.is_absolute() or any(part in {"..", "."} for part in relative.parts):
                raise UserError("--path must be a safe relative path within Academic_Library")
            subject = taxonomy.subjects.get(args.subject)
            if not subject or subject.deprecated:
                raise UserError(f"--subject must name an active taxonomy subject; got {args.subject!r}")
            conn = connect(config.database_path)
            try:
                if not args.apply:
                    row = conn.execute("SELECT relative_path,subject_id,sha256 FROM documents WHERE relative_path=? AND state='present'", (relative.as_posix(),)).fetchone()
                    if not row:
                        raise UserError(f"No present indexed document at {relative.as_posix()!r}; run scan first")
                    _print_json({"mode": "dry-run", "relative_path": row["relative_path"], "primary_subject_id": row["subject_id"],
                                 "related_subject_id": args.subject, "related_subject_name": subject.name,
                                 "would_change": row["subject_id"] != args.subject})
                    return 0
            finally:
                conn.close()
            with database_lock(config.database_path):
                conn = connect(config.database_path)
                try:
                    sync_taxonomy(conn, taxonomy)
                    report = add_related_subject(conn, relative.as_posix(), args.subject)
                finally:
                    conn.close()
            _safe_audit(config, {"command": "tag", "mode": "apply", **report})
            _print_json(report)
            return 0
        if args.command == "scan":
            report = scan_library(config, taxonomy, full_hash=args.full_hash)
            _print_json(report)
            return 0 if report["status"] == "completed" else 2
        if args.command == "import":
            report = run_import(config, taxonomy, args.source, apply=args.apply, report_path=args.report)
            _print_json(report)
            return 0 if report["status"] in {"dry-run", "completed"} else 2
        if args.command == "duplicates":
            conn = connect(config.database_path)
            try:
                report = duplicate_report(conn)
            finally:
                conn.close()
            if args.format == "json":
                _print_json(report)
            else:
                print(f"Exact duplicate groups: {len(report['exact_duplicate_groups'])}")
                for group in report["exact_duplicate_groups"]:
                    print(f"  SHA-256 {group['sha256']} ({group['count']} files)")
                    for path in group["paths"]:
                        print(f"    {path}")
                print(f"Possible duplicate groups: {len(report['possible_duplicate_groups'])}")
                print(f"Potential version conflicts: {len(report['potential_version_conflicts'])}")
                for group in report["potential_version_conflicts"]:
                    print(f"  {group['normalized_stem']}")
                    for item in group["files"]:
                        print(f"    {item['relative_path']} [{item['sha256'] or 'no hash'}]")
                print(f"Identifier conflicts: {len(report['identifier_conflicts'])}")
            return 0
        if args.command == "stats":
            conn = connect(config.database_path)
            try:
                report = collect_stats(conn)
            finally:
                conn.close()
            _safe_audit(config, {"command": "stats", "format": args.format, "status": "ok",
                          "document_count": report["overview"]["documents_total_known"]})
            text = export_stats(report, args.format, args.output)
            if not args.output:
                print(text, end="")
            else:
                print(f"Wrote statistics report to {args.output}")
            return 0
        if args.command == "verify":
            report = verify_library(config, taxonomy, full_hash=args.full_hash)
            _safe_audit(config, {"command": "verify", "full_hash": args.full_hash, "status": report["status"]})
            if args.output:
                output = args.output.expanduser().absolute()
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
            if args.format == "json":
                _print_json(report)
            else:
                print(f"Verification status: {report['status']}")
                for key, value in report.items():
                    if key == "status":
                        continue
                    if value:
                        print(f"{key}: {json.dumps(value, ensure_ascii=False, default=str)}")
            return 0 if report["status"] == "ok" else 2
        if args.command == "search":
            if args.limit < 1 or args.limit > 10000:
                raise UserError("--limit must be between 1 and 10000")
            return _search(config, args)
        if args.command == "backup":
            if args.output:
                output = args.output
            else:
                backup_dir = config.database_path.parent / "backups"
                output = backup_dir / f"catalog-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.sqlite3"
            path = backup_database(config.database_path, output)
            _safe_audit(config, {"command": "backup", "status": "ok", "destination": str(path)})
            print(f"Database backup verified: {path}")
            return 0
        if args.command == "export":
            _export_catalog(config, args.format, args.output)
            return 0
        if args.command == "recover":
            report = recover_imports(config, taxonomy, apply=args.apply)
            _print_json(report)
            return 0 if not report["not_recoverable"] else 2
        if args.command == "status":
            conn = connect(config.database_path)
            try:
                last = conn.execute("SELECT id,started_at,finished_at,status,files_seen,files_changed,missing_marked FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
                batches = conn.execute("SELECT status,COUNT(*) n FROM import_batches GROUP BY status").fetchall()
                review = conn.execute("SELECT COUNT(*) FROM import_items WHERE status='review_queued'").fetchone()[0]
                unresolved = conn.execute("SELECT COUNT(*) FROM import_items WHERE status IN ('failed','unresolved','copying','pending')").fetchone()[0]
                missing = conn.execute("SELECT COUNT(*) FROM documents WHERE state='missing'").fetchone()[0]
            finally:
                conn.close()
            _print_json({"config": str(config.config_path), "library_root": str(config.library_root), "database": str(config.database_path),
                         "latest_scan": dict(last) if last else None, "import_batches_by_status": {r["status"]: r["n"] for r in batches},
                         "review_queued_items": review, "failed_or_pending_import_items": unresolved, "missing_records": missing})
            return 0
        parser.error("Unknown command")
    except (FileNotFoundError, NotADirectoryError, FileExistsError, PermissionError, ValueError, TaxonomyError, UserError, LockUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        if os.environ.get("ACADEMIC_LIBRARY_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
