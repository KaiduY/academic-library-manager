"""Current-state and historical statistics, suitable for terminal or JSON/CSV export."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from .duplicates import duplicate_report


def collect_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    present = conn.execute("SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM documents WHERE state='present'").fetchone()
    totals = {"documents_present": present[0], "bytes_present": present[1],
              "documents_missing": conn.execute("SELECT COUNT(*) FROM documents WHERE state='missing'").fetchone()[0],
              "documents_total_known": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
              "documents_unclassified": conn.execute("SELECT COUNT(*) FROM documents WHERE state='present' AND (subject_id IS NULL OR subject_id='review.unclassified')").fetchone()[0],
              "documents_incomplete_metadata": conn.execute("SELECT COUNT(*) FROM documents WHERE state='present' AND (title IS NULL OR title_source IN ('filename','filename_heuristic') OR publication_year IS NULL)").fetchone()[0],
              "documents_hash_missing": conn.execute("SELECT COUNT(*) FROM documents WHERE state='present' AND sha256 IS NULL").fetchone()[0],
              "documents_without_identifiers": conn.execute("SELECT COUNT(*) FROM documents d WHERE d.state='present' AND NOT EXISTS (SELECT 1 FROM identifiers i WHERE i.document_id=d.id)").fetchone()[0]}
    by_extension = [{"extension": row[0] or "(none)", "count": row[1], "bytes": row[2]} for row in conn.execute(
        "SELECT extension,COUNT(*),COALESCE(SUM(size_bytes),0) FROM documents WHERE state='present' GROUP BY extension ORDER BY COUNT(*) DESC,extension")]
    by_type = [{"document_type": row[0] or "unclassified", "count": row[1], "bytes": row[2]} for row in conn.execute(
        "SELECT document_type,COUNT(*),COALESCE(SUM(size_bytes),0) FROM documents WHERE state='present' GROUP BY document_type ORDER BY COUNT(*) DESC")]
    by_subject = [{"subject_id": row[0] or "unclassified", "subject": row[1] or "Unassigned", "count": row[2], "bytes": row[3]}
                  for row in conn.execute("""SELECT d.subject_id,COALESCE(s.name,'Unassigned'),COUNT(*),COALESCE(SUM(d.size_bytes),0)
                  FROM documents d LEFT JOIN subjects s ON s.id=d.subject_id WHERE d.state='present'
                  GROUP BY d.subject_id,s.name ORDER BY COUNT(*) DESC,s.name""")]
    largest = [{"relative_path": row[0], "size_bytes": row[1], "sha256": row[2]} for row in conn.execute(
        "SELECT relative_path,size_bytes,sha256 FROM documents WHERE state='present' ORDER BY size_bytes DESC LIMIT 20")]
    recent_imports = [dict(row) for row in conn.execute(
        "SELECT id,started_at,finished_at,source_root,mode,status,summary_json FROM import_batches ORDER BY started_at DESC LIMIT 20")]
    latest_scan_row = conn.execute("SELECT * FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    latest_scan = dict(latest_scan_row) if latest_scan_row else None
    if latest_scan and latest_scan.get("errors_json"):
        try:
            latest_scan["errors"] = json.loads(latest_scan.pop("errors_json"))
        except json.JSONDecodeError:
            pass
    scans = [dict(row) for row in conn.execute(
        "SELECT started_at,status,files_seen,files_changed,missing_marked FROM scan_runs ORDER BY started_at")]
    ops = conn.execute("SELECT COUNT(*) FROM operation_log WHERE result IN ('failed','incomplete','unresolved')").fetchone()[0]
    import_failed = conn.execute("SELECT COUNT(*) FROM import_items WHERE status IN ('failed','unresolved','copying','pending')").fetchone()[0]
    pending_review = conn.execute("SELECT COUNT(*) FROM import_items WHERE status='review_queued'").fetchone()[0]
    dup = duplicate_report(conn)
    return {"overview": totals, "by_extension": by_extension, "by_document_type": by_type,
            "by_subject": by_subject, "largest_files": largest, "recent_imports": recent_imports,
            "scan_history": scans, "latest_scan": latest_scan, "operation_errors_count": ops,
            "failed_or_pending_import_items": import_failed, "import_items_review_queued": pending_review,
            "duplicate_statistics": {"exact_duplicate_groups": len(dup["exact_duplicate_groups"]),
                                      "possible_duplicate_groups": len(dup["possible_duplicate_groups"]),
                                      "potential_version_conflicts": len(dup["potential_version_conflicts"]),
                                      "identifier_conflicts": len(dup["identifier_conflicts"]),
                                      "documents_with_identifier_conflict": len({str(doc_id) for item in dup["identifier_conflicts"] for doc_id in item["document_ids"]})},
            "duplicate_details": dup}


def export_stats(report: dict[str, Any], fmt: str, output: Path | None = None) -> str:
    if fmt == "json":
        text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    elif fmt == "csv":
        rows = []
        overview = report.get("overview", {})
        rows.extend({"section": "overview", "key": key, "value": value} for key, value in overview.items())
        for section in ("by_extension", "by_document_type", "by_subject", "largest_files", "duplicate_statistics"):
            value = report.get(section, [])
            if isinstance(value, dict):
                rows.extend({"section": section, "key": k, "value": v} for k, v in value.items())
            else:
                rows.extend({"section": section, "key": str(i), "value": json.dumps(row, ensure_ascii=False)} for i, row in enumerate(value))
        import io
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["section", "key", "value"])
        writer.writeheader()
        writer.writerows(rows)
        text = buffer.getvalue()
    else:
        lines = ["Academic library statistics", "============================", ""]
        for key, value in report.get("overview", {}).items():
            lines.append(f"{key.replace('_', ' '):32} {value}")
        lines += ["", "By subject:"]
        for row in report.get("by_subject", []):
            lines.append(f"  {row['subject']:<38} {row['count']:>6} files  {row['bytes']:>12} bytes")
        lines += ["", "By document type:"]
        for row in report.get("by_document_type", []):
            lines.append(f"  {row['document_type']:<26} {row['count']:>6} files  {row['bytes']:>12} bytes")
        lines += ["", "Duplicate / maintenance:"]
        for key, value in report.get("duplicate_statistics", {}).items():
            lines.append(f"  {key.replace('_', ' '):<35} {value}")
        lines.append(f"  {'failed or pending import items':<35} {report.get('failed_or_pending_import_items', 0)}")
        lines.append(f"  {'review-queued imports':<35} {report.get('import_items_review_queued', 0)}")
        if report.get("latest_scan"):
            lines += ["", f"Latest scan: {report['latest_scan'].get('started_at')} ({report['latest_scan'].get('status')})"]
        text = "\n".join(lines) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        # Report exports are new outputs; do not silently overwrite a prior report.
        with output.open("x", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return text
