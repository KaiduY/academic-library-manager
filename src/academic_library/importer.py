"""Additive, no-clobber imports with checksums, provenance, and crash-recovery records."""
from __future__ import annotations

import errno
import json
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .config import AppConfig
from .database import add_operation, connect, sync_taxonomy, transaction, upsert_document, store_identifiers, utc_now
from .duplicates import find_exact_duplicate, possible_matches
from .filesystem import FileCandidate, inspect_file, inventory_tree, sha256_file
from .logging_utils import append_audit, database_lock
from .taxonomy import Taxonomy


class ImportErrorSafe(RuntimeError):
    pass


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_source(source: Path, library_root: Path) -> Path:
    original_source = source.expanduser().absolute()
    if original_source.is_symlink():
        raise ValueError("Import source root must not be a symlink")
    src = original_source.resolve(strict=True)
    lib = library_root.expanduser().resolve(strict=True)
    if not src.is_dir():
        raise NotADirectoryError(f"Import source is not a directory: {src}")
    if src == lib or _inside(src, lib) or _inside(lib, src):
        raise ValueError("Import source and library root may not be equal or nested; choose a separate source directory")
    if src.is_symlink():
        raise ValueError("Import source root must not be a symlink")
    return src


def _ensure_safe_parent(library_root: Path, parent: Path) -> None:
    root = library_root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ImportErrorSafe(f"Library root must be an existing, non-symlink directory: {root}")
    try:
        parts = parent.absolute().relative_to(root).parts
    except ValueError as exc:
        raise ImportErrorSafe(f"Destination parent is outside the library root: {parent}") from exc
    probe = root
    for part in parts:
        child = probe / part
        if child.is_symlink():
            raise ImportErrorSafe(f"Refusing destination with symlinked parent: {child}")
        try:
            child.mkdir()
        except FileExistsError:
            if not child.is_dir() or child.is_symlink():
                raise ImportErrorSafe(f"Destination parent component is not a safe directory: {child}")
        if child.is_symlink() or not child.is_dir():
            raise ImportErrorSafe(f"Destination parent component is not a safe directory: {child}")
        probe = child


def atomic_copy_no_clobber(source: Path, destination: Path, expected_sha256: str, library_root: Path | None = None) -> str:
    """Copy to a unique sibling temporary, verify, then publish with atomic hard-link no-clobber."""
    if library_root is not None:
        _ensure_safe_parent(library_root, destination.parent)
    else:
        # Used directly only by callers that have already selected a safe parent.
        destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.parent / f".academic-library-tmp-{uuid.uuid4().hex}"
    try:
        source_before = sha256_file(source)
        if source_before != expected_sha256:
            raise ImportErrorSafe(f"Source changed since planning: {source}")
        source_stat = source.stat(follow_symlinks=False)
        digest = __import__("hashlib").sha256()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temp, flags, 0o600)
        source_fd = None
        try:
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            # Wrap descriptors only after both opens succeed, so a failed source open
            # cannot leak the temporary destination descriptor.
            with os.fdopen(fd, "wb") as target, os.fdopen(source_fd, "rb") as origin:
                fd = source_fd = None  # ownership transferred to the file objects
                while True:
                    block = origin.read(1024 * 1024)
                    if not block:
                        break
                    target.write(block)
                    digest.update(block)
                target.flush()
                os.fsync(target.fileno())
            # Make the verified copy readable by its owning account even if the
            # source had restrictive owner bits; retain source group/other bits.
            source_mode = stat.S_IMODE(source_stat.st_mode)
            safe_mode = (source_mode & 0o077) | stat.S_IRUSR | stat.S_IWUSR
            os.chmod(temp, safe_mode)
            os.utime(temp, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns), follow_symlinks=False)
        except Exception:
            # Close descriptors that were not transferred to file objects.
            for pending_fd in (fd, source_fd):
                if isinstance(pending_fd, int):
                    try:
                        os.close(pending_fd)
                    except OSError:
                        pass
            # The temp file is owned by this operation, so cleaning it is safe.
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        temp_hash = digest.hexdigest()
        source_after = sha256_file(source)
        if temp_hash != expected_sha256 or source_after != expected_sha256:
            raise ImportErrorSafe(f"Copy integrity check failed or source changed during copy: {source}")
        # os.link fails rather than replacing when destination already exists; temp and destination share a filesystem.
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV}:
                raise ImportErrorSafe(
                    f"Filesystem does not support safe no-clobber hard-link publication at {destination}; "
                    "file was not published. Use a filesystem supporting hard links or review manually."
                ) from exc
            raise
        # Sync the directory entry where supported, before recording import success.
        try:
            dir_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        actual = sha256_file(destination)
        if actual != expected_sha256:
            # Do not delete or overwrite a published file on verification failure. Report it for review.
            raise ImportErrorSafe(f"Published destination hash mismatch: {destination}")
        return actual
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # A leftover temporary is reported by verify; never mask the primary exception.
            pass


def _relative_safe(relative: str) -> Path:
    p = PurePosixPath(relative)
    if p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise ValueError(f"Unsafe relative path: {relative}")
    return Path(*p.parts)


def _safe_destination(config: AppConfig, relative_path: str) -> Path:
    rel = _relative_safe(relative_path)
    root = config.library_root.absolute()
    dest = root / rel
    if not _inside(dest.absolute(), root):
        raise ValueError(f"Destination escapes library root: {relative_path}")
    if root.is_symlink():
        raise ValueError(f"Library root is a symlink: {root}")
    # Reject pre-existing symlink components so a taxonomy path cannot redirect writes outside the archive.
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Destination path contains a symlink component: {current}")
    return dest


def _validate_import_outputs(config: AppConfig, source_root: Path, report_path: Path | None) -> None:
    managed = [config.database_path, Path(str(config.database_path) + ".lock"),
               config.log_directory, config.staging_directory]
    for path in managed:
        resolved = path.absolute().resolve(strict=False)
        if _inside(resolved, source_root):
            raise ValueError(f"Application state path {path} is inside the read-only import source; move it outside")
    if report_path is not None:
        out = report_path.expanduser().absolute().resolve(strict=False)
        if _inside(out, source_root):
            raise ValueError("--report must not write inside the read-only import source")
        if _inside(out, config.library_root.resolve(strict=True)):
            raise ValueError("--report must not write inside Academic_Library")
        if out.exists():
            raise FileExistsError(f"Refusing to overwrite existing import report: {out}")


def _review_destination(classification_subject: str, kind: str, candidate: FileCandidate) -> str:
    source_label = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate.path.parent.name).strip("._-") or "source"
    digest = candidate.sha256 or "nohash"
    if kind == "unclassified":
        return f"90_Review_Queue/Unclassified/{digest[:12]}/{candidate.filename}"
    return f"90_Review_Queue/{kind}/{source_label}__{digest[:12]}/{candidate.filename}"


def _candidate_status(existing: list[dict[str, Any]], prior: list[dict[str, Any]], same_target_exists: bool) -> tuple[str, str, str | None, str | None]:
    if existing:
        match = existing[0]
        strong = bool(match.get("shared_identifiers")) or bool(match.get("same_stem"))
        kind = "Version_Conflicts" if strong else "Possible_Duplicates"
        return "review", kind, match["relative_path"], match["reason"]
    if prior:
        match = prior[0]
        if match.get("sha256"):
            return "skip_duplicate", "", match.get("destination_relative_path") or match.get("source_relative_path"), "same checksum in this import batch"
        return "review", "Possible_Duplicates", match.get("destination_relative_path"), "similar candidate earlier in this batch"
    if same_target_exists:
        return "review", "Version_Conflicts", None, "destination already exists with different content"
    return "import", "", None, None


def run_import(config: AppConfig, taxonomy: Taxonomy, source: Path | str, *, apply: bool = False,
               report_path: Path | None = None) -> dict[str, Any]:
    source_root = validate_source(Path(source), config.library_root)
    _validate_import_outputs(config, source_root, report_path)
    inventory = inventory_tree(source_root, ignore_directories=config.ignore_directories)
    conn = connect(config.database_path)
    batch_id = uuid.uuid4().hex
    mode = "apply" if apply else "dry-run"
    started = utc_now()
    conn.execute("INSERT INTO import_batches(id,started_at,source_root,mode,status) VALUES(?,?,?,?,?)",
                  (batch_id, started, str(source_root), mode, "running" if apply else "planned"))
    conn.commit()
    summary: dict[str, Any] = {"batch_id": batch_id, "source_root": str(source_root), "mode": mode,
                               "status": "planned" if not apply else "running", "items": [],
                               "errors": list(inventory.errors), "warnings": list(inventory.warnings)}
    try:
        with database_lock(config.database_path):
            sync_taxonomy(conn, taxonomy)
            # Build complete checksummed source inventory first; any unreadable source makes apply partial and explicit.
            candidates: list[FileCandidate] = []
            for item in inventory.files:
                try:
                    candidates.append(inspect_file(item.path, item.relative_path, config.pdfinfo_command, compute_hash=True))
                except OSError as exc:
                    summary["errors"].append(f"Could not read/hash source file {item.path}: {exc}")
            prior: list[dict[str, Any]] = []
            for candidate in candidates:
                proposed_rel, classification = taxonomy.destination_for(candidate.relative_path)
                existing_exact = find_exact_duplicate(conn, config.library_root, candidate.sha256 or "")
                # Also compare against pending sources in deterministic order for within-batch duplicates.
                prior_exact = [p for p in prior if p.get("sha256") == candidate.sha256]
                if existing_exact:
                    action, review_kind, match_path, reason = "skip_duplicate", "", existing_exact, "exact SHA-256 match in existing library"
                    destination_rel = None
                elif prior_exact:
                    action, review_kind, match_path, reason = "skip_duplicate", "", prior_exact[0].get("destination_relative_path"), "exact SHA-256 match earlier in this batch"
                    destination_rel = None
                else:
                    matches = possible_matches(conn, candidate.filename, candidate.identifiers)
                    # Include different names from this source batch using the same heuristic.
                    for prior_item in prior:
                        from .duplicates import normalized_similarity
                        sim = normalized_similarity(candidate.filename, str(prior_item["filename"]))
                        exact_stem = normalize_for_match(candidate.filename) == normalize_for_match(str(prior_item["filename"]))
                        if exact_stem or sim >= 0.88:
                            matches.append({"relative_path": prior_item.get("destination_relative_path") or prior_item["source_relative_path"],
                                            "sha256": prior_item.get("sha256"), "similarity": round(sim, 3),
                                            "reason": "same normalized filename in this batch" if exact_stem else "similar filename in this batch",
                                            "same_stem": exact_stem, "shared_identifiers": []})
                    target = _safe_destination(config, proposed_rel)
                    destination_exists = target.exists() or target.is_symlink()
                    target_same_hash = False
                    if destination_exists and target.is_file() and not target.is_symlink():
                        try:
                            target_same_hash = sha256_file(target) == candidate.sha256
                        except OSError as exc:
                            summary["errors"].append(f"Could not verify occupied destination {target}: {exc}")
                    if target_same_hash:
                        action, review_kind, match_path, reason = "skip_duplicate", "", proposed_rel, "destination already contains identical content"
                        destination_rel = None
                    else:
                        action, review_kind, match_path, reason = _candidate_status(matches, [], destination_exists)
                        if action == "import" and classification.subject_id == taxonomy.review_subject:
                            action, review_kind = "review", "Unclassified"
                            reason = classification.reason
                        destination_rel = _review_destination(classification.subject_id, review_kind or "Possible_Duplicates", candidate) if action == "review" else proposed_rel
                        # If deterministic review destination exists, never overwrite it. Only allow identical copies to skip.
                        if destination_rel:
                            review_target = _safe_destination(config, destination_rel)
                            if review_target.exists() or review_target.is_symlink():
                                if review_target.is_file() and not review_target.is_symlink() and sha256_file(review_target) == candidate.sha256:
                                    action, destination_rel, match_path, reason = "skip_duplicate", None, destination_rel, "review queue already contains identical content"
                                else:
                                    summary["errors"].append(f"Review destination already occupied by different content; left in source: {review_target}")
                                    action, destination_rel = "unresolved", None
                item = {
                    "source_path": str(candidate.path), "source_relative_path": candidate.relative_path,
                    "source_sha256": candidate.sha256, "size_bytes": candidate.size_bytes,
                    "proposed_relative_path": proposed_rel, "destination_relative_path": destination_rel,
                    "action": action, "review_kind": review_kind, "match_path": match_path,
                    "match_reason": reason, "subject_id": classification.subject_id,
                    "document_type": classification.document_type, "classification_confidence": classification.confidence,
                    "classification_reason": classification.reason, "title": candidate.title,
                    "title_source": candidate.title_source, "title_confidence": candidate.title_confidence,
                    "publication_year": candidate.publication_year,
                    "publication_year_source": candidate.publication_year_source,
                    "publication_year_confidence": candidate.publication_year_confidence,
                    "edition": candidate.edition, "edition_source": candidate.edition_source,
                    "edition_confidence": candidate.edition_confidence, "identifiers": candidate.identifiers,
                }
                summary["items"].append(item)
                prior.append({**item, "filename": candidate.filename})
            # Record the full plan first; operations are recoverable if a later copy fails or the process stops.
            with transaction(conn):
                for item in summary["items"]:
                    status = "planned" if not apply else ("pending" if item["action"] in {"import", "review"} else item["action"])
                    conn.execute(
                        """INSERT INTO import_items(batch_id,source_path,source_relative_path,source_sha256,
                           proposed_relative_path,destination_relative_path,action,status,match_path,match_reason,
                           error,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (batch_id, item["source_path"], item["source_relative_path"], item["source_sha256"],
                         item["proposed_relative_path"], item["destination_relative_path"], item["action"], status,
                         item["match_path"], item["match_reason"], None,
                         json.dumps({k: v for k, v in item.items() if k not in {"source_path", "source_relative_path"}}, ensure_ascii=False, sort_keys=True)),
                    )
                    add_operation(conn, command="import", mode=mode, batch_id=batch_id, source_path=item["source_relative_path"],
                                  destination_path=item["destination_relative_path"], checksum=item["source_sha256"],
                                  operation=item["action"], result="planned" if not apply else "pending",
                                  details={"match_path": item["match_path"], "reason": item["match_reason"]})
                conn.execute("UPDATE import_batches SET summary_json=? WHERE id=?",
                             (json.dumps({"planned_count": len(summary["items"]), "errors": summary["errors"]}, ensure_ascii=False), batch_id))
            if apply:
                counts = {"imported": 0, "review_queued": 0, "skipped_duplicate": 0, "unresolved": 0, "failed": 0}
                for item in summary["items"]:
                    action = item["action"]
                    if action == "skip_duplicate":
                        item["status"] = "skipped_duplicate"
                        counts["skipped_duplicate"] += 1
                        _update_item_status(conn, batch_id, item, "skipped_duplicate")
                        continue
                    if action == "unresolved" or not item["destination_relative_path"]:
                        item["status"] = "unresolved"
                        counts["unresolved"] += 1
                        _update_item_status(conn, batch_id, item, "unresolved", "Destination conflict; source preserved")
                        continue
                    destination = _safe_destination(config, item["destination_relative_path"])
                    _update_item_status(conn, batch_id, item, "copying")
                    try:
                        published_hash = atomic_copy_no_clobber(Path(item["source_path"]), destination, item["source_sha256"], config.library_root)
                        candidate = inspect_file(destination, item["destination_relative_path"], config.pdfinfo_command, compute_hash=False)
                        candidate.sha256 = published_hash
                        with transaction(conn):
                            doc_id, _, _ = upsert_document(
                                conn, relative_path=item["destination_relative_path"], filename=candidate.filename,
                                extension=candidate.extension, size_bytes=candidate.size_bytes, mtime_ns=destination.stat().st_mtime_ns,
                                sha256=published_hash, file_type=candidate.file_type, title=item["title"],
                                title_source=item["title_source"], title_confidence=item["title_confidence"],
                                publication_year=item["publication_year"],
                                publication_year_source=item["publication_year_source"],
                                publication_year_confidence=item["publication_year_confidence"],
                                edition=item["edition"], edition_source=item["edition_source"],
                                edition_confidence=item["edition_confidence"],
                                subject_id=item["subject_id"], document_type=item["document_type"],
                                classification_source="taxonomy_keyword_heuristic", classification_confidence=item["classification_confidence"],
                                metadata_status=candidate.metadata_status, import_batch_id=batch_id, imported_from_path=item["source_path"],
                            )
                            store_identifiers(conn, doc_id, item["identifiers"])
                            status = "review_queued" if action == "review" else "imported"
                            conn.execute("UPDATE import_items SET status=?,error=NULL WHERE batch_id=? AND source_path=?",
                                         (status, batch_id, item["source_path"]))
                            conn.execute("INSERT INTO document_events(document_id,event_at,event_type,new_path,new_sha256,details_json) VALUES(?,?,?,?,?,?)",
                                         (doc_id, utc_now(), status, item["destination_relative_path"], published_hash,
                                          json.dumps({"batch_id": batch_id, "source_path": item["source_path"]}, ensure_ascii=False)))
                            add_operation(conn, command="import", mode="apply", batch_id=batch_id,
                                          source_path=item["source_relative_path"], destination_path=item["destination_relative_path"],
                                          checksum=published_hash, operation="copy_verified", result=status,
                                          details={"source_path": item["source_path"], "classification_reason": item["classification_reason"]})
                        item["status"] = status
                        counts[status] += 1
                    except FileExistsError:
                        item["status"] = "unresolved"
                        item["error"] = "Destination appeared concurrently; no overwrite attempted"
                        counts["unresolved"] += 1
                        _update_item_status(conn, batch_id, item, "unresolved", item["error"])
                    except Exception as exc:
                        item["status"] = "failed"
                        item["error"] = str(exc)
                        counts["failed"] += 1
                        _update_item_status(conn, batch_id, item, "failed", str(exc))
                        summary["errors"].append(f"Import failed for {item['source_relative_path']}: {exc}")
                summary["counts"] = counts
                summary["status"] = "completed" if not summary["errors"] and not counts["failed"] and not counts["unresolved"] else "partial"
            else:
                counts: dict[str, int] = {}
                for item in summary["items"]:
                    key = {"import": "would_import", "review": "would_queue_for_review", "skip_duplicate": "would_skip_duplicate", "unresolved": "unresolved"}.get(item["action"], item["action"])
                    counts[key] = counts.get(key, 0) + 1
                summary["counts"] = counts
                summary["status"] = "dry-run"
            conn.execute("UPDATE import_batches SET finished_at=?,status=?,summary_json=? WHERE id=?",
                         (utc_now(), summary["status"], json.dumps({"counts": summary.get("counts", {}), "errors": summary["errors"]}, ensure_ascii=False), batch_id))
            conn.commit()
            try:
                append_audit(config.log_directory, {"command": "import", "mode": mode, "batch_id": batch_id,
                              "source_root": str(source_root), "status": summary["status"], "counts": summary.get("counts", {}),
                              "errors": summary["errors"]})
            except OSError as log_exc:
                # Import state is already recorded in SQLite; make the external audit-log problem visible without
                # incorrectly rewriting a completed batch as failed.
                summary["warnings"].append(f"Could not append JSONL audit log: {log_exc}; SQLite operation log remains available")
        if report_path:
            report_path = report_path.expanduser().absolute()
            try:
                report_path.parent.mkdir(parents=True, exist_ok=True)
                with report_path.open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n")
            except OSError as report_exc:
                summary["warnings"].append(f"Import completed but report could not be written: {report_exc}")
        return summary
    except Exception as exc:
        conn.execute("UPDATE import_batches SET finished_at=?,status='failed',summary_json=? WHERE id=?",
                     (utc_now(), json.dumps({"error": str(exc)}, ensure_ascii=False), batch_id))
        conn.commit()
        add_operation(conn, command="import", mode=mode, batch_id=batch_id, operation="import_exception", result="failed", details={"error": str(exc)})
        conn.commit()
        raise
    finally:
        conn.close()


def normalize_for_match(filename: str) -> str:
    from .duplicates import normalize_stem
    return normalize_stem(filename)


def _update_item_status(conn, batch_id: str, item: dict[str, Any], status: str, error: str | None = None) -> None:
    conn.execute("UPDATE import_items SET status=?,error=? WHERE batch_id=? AND source_path=?",
                 (status, error, batch_id, item["source_path"]))
    conn.commit()
    add_operation(conn, command="import", mode="apply", batch_id=batch_id, source_path=item["source_relative_path"],
                  destination_path=item.get("destination_relative_path"), checksum=item.get("source_sha256"),
                  operation="update_item_status", result=status, details={"error": error})
    conn.commit()


def _recover_imports_unlocked(config: AppConfig, taxonomy: Taxonomy, *, apply: bool = False) -> dict[str, Any]:
    """Recover DB state when a verified final file was published before a process stopped."""
    conn = connect(config.database_path)
    report = {"pending": [], "recoverable": [], "recovered": [], "not_recoverable": []}
    try:
        sync_taxonomy(conn, taxonomy)
        rows = conn.execute("SELECT * FROM import_items WHERE status IN ('copying','failed') ORDER BY id").fetchall()
        for row in rows:
            item = {"batch_id": row["batch_id"], "source_path": row["source_path"],
                    "source_relative_path": row["source_relative_path"], "source_sha256": row["source_sha256"],
                    "destination_relative_path": row["destination_relative_path"]}
            report["pending"].append(item)
            if not row["destination_relative_path"] or not row["source_sha256"]:
                report["not_recoverable"].append({**item, "reason": "missing destination/checksum in journal"})
                continue
            destination = _safe_destination(config, row["destination_relative_path"])
            try:
                if not destination.is_file() or destination.is_symlink() or sha256_file(destination) != row["source_sha256"]:
                    report["not_recoverable"].append({**item, "reason": "destination absent or checksum differs"})
                    continue
            except OSError as exc:
                report["not_recoverable"].append({**item, "reason": str(exc)})
                continue
            report["recoverable"].append(item)
            if apply:
                candidate = inspect_file(destination, row["destination_relative_path"], config.pdfinfo_command, compute_hash=True)
                classification = taxonomy.classify(row["destination_relative_path"])
                with transaction(conn):
                    doc_id, _, _ = upsert_document(
                        conn, relative_path=candidate.relative_path, filename=candidate.filename, extension=candidate.extension,
                        size_bytes=candidate.size_bytes, mtime_ns=candidate.mtime_ns, sha256=candidate.sha256,
                        file_type=candidate.file_type, title=candidate.title, title_source=candidate.title_source,
                        title_confidence=candidate.title_confidence, publication_year=candidate.publication_year,
                        publication_year_source=candidate.publication_year_source,
                        publication_year_confidence=candidate.publication_year_confidence,
                        edition=candidate.edition, edition_source=candidate.edition_source,
                        edition_confidence=candidate.edition_confidence,
                        subject_id=classification.subject_id, document_type=classification.document_type,
                        classification_source=classification.source, classification_confidence=classification.confidence,
                        metadata_status=candidate.metadata_status, import_batch_id=row["batch_id"], imported_from_path=row["source_path"])
                    conn.execute("UPDATE import_items SET status='imported_recovered',error=NULL WHERE id=?", (row["id"],))
                    conn.execute("INSERT INTO document_events(document_id,event_at,event_type,new_path,new_sha256,details_json) VALUES(?,?,?,?,?,?)",
                                 (doc_id, utc_now(), "import_recovered", candidate.relative_path, candidate.sha256,
                                  json.dumps({"batch_id": row["batch_id"]})))
                    add_operation(conn, command="recover", mode="apply", batch_id=row["batch_id"], source_path=row["source_relative_path"],
                                  destination_path=candidate.relative_path, checksum=candidate.sha256, operation="recover_import_record",
                                  result="imported_recovered", details={"source_path": row["source_path"]})
                report["recovered"].append(item)
        if apply:
            batch_ids = {item["batch_id"] for item in report["recovered"]}
            for batch_id in batch_ids:
                unfinished = conn.execute("SELECT COUNT(*) FROM import_items WHERE batch_id=? AND status IN ('copying','pending','failed','unresolved')", (batch_id,)).fetchone()[0]
                if unfinished == 0:
                    conn.execute("UPDATE import_batches SET status='completed',finished_at=? WHERE id=? AND status IN ('running','partial')", (utc_now(), batch_id))
            conn.commit()
        try:
            append_audit(config.log_directory, {"command": "recover", "mode": "apply" if apply else "dry-run", **report})
        except OSError as log_exc:
            report.setdefault("warnings", []).append(f"Could not append JSONL audit log: {log_exc}; SQLite operation log remains available")
        return report
    finally:
        conn.close()


def recover_imports(config: AppConfig, taxonomy: Taxonomy, *, apply: bool = False) -> dict[str, Any]:
    """Serialize journal recovery with scans/imports; recovery only updates metadata, never files."""
    with database_lock(config.database_path):
        return _recover_imports_unlocked(config, taxonomy, apply=apply)
