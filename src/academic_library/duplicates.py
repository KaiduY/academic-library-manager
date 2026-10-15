"""Exact and heuristic duplicate detection. Heuristics only recommend review; they never discard files."""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .filesystem import sha256_file


def normalize_stem(value: str) -> str:
    stem = Path(value).stem.casefold()
    stem = re.sub(r"\b(?:copy|final|revised|scan|scanned)\b", " ", stem)
    stem = re.sub(r"\b(?:19|20|21)\d{2}\b", " ", stem)
    stem = re.sub(r"\b\d+(?:st|nd|rd|th)?\s*ed(?:ition)?\b", " ", stem)
    return " ".join(re.findall(r"[a-z0-9]+", stem))


def normalized_similarity(a: str, b: str) -> float:
    na, nb = normalize_stem(a), normalize_stem(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def identifiers_for_path(conn: sqlite3.Connection, document_id: int) -> set[tuple[str, str]]:
    rows = conn.execute("SELECT kind,normalized_value FROM identifiers WHERE document_id=?", (document_id,)).fetchall()
    return {(row["kind"], row["normalized_value"]) for row in rows}


def find_exact_duplicate(conn: sqlite3.Connection, library_root: Path, checksum: str) -> str | None:
    rows = conn.execute(
        "SELECT id,relative_path,size_bytes,mtime_ns,sha256 FROM documents WHERE sha256=? AND state='present' ORDER BY relative_path",
        (checksum,),
    ).fetchall()
    for row in rows:
        path = library_root / Path(row["relative_path"])
        try:
            if not path.is_file() or path.is_symlink():
                continue
            st = path.stat(follow_symlinks=False)
            # Rehash only paths whose cached hash matches the candidate. This protects duplicate decisions
            # even if someone changed bytes while preserving size and mtime.
            actual = sha256_file(path)
            # Correct a stale cache fact, but only the catalog metadata, never the file.
            conn.execute("UPDATE documents SET size_bytes=?,mtime_ns=?,sha256=?,state='present',last_indexed_at=? WHERE id=?",
                         (st.st_size, st.st_mtime_ns, actual, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"), row["id"]))
            conn.commit()
            if actual == checksum:
                return row["relative_path"]
        except OSError:
            continue
    return None


def possible_matches(conn: sqlite3.Connection, candidate_path: str, candidate_identifiers: list[dict[str, object]],
                     *, min_similarity: float = 0.88) -> list[dict[str, Any]]:
    normalized = normalize_stem(candidate_path)
    identifiers = {(str(x["kind"]), str(x["normalized_value"])) for x in candidate_identifiers}
    rows = conn.execute(
        "SELECT id,relative_path,filename,title,sha256,subject_id,document_type,state FROM documents WHERE state='present'"
    ).fetchall()
    matches: list[dict[str, Any]] = []
    for row in rows:
        other_norm = normalize_stem(row["filename"])
        score = SequenceMatcher(None, normalized, other_norm).ratio() if normalized and other_norm else 0.0
        shared_id: list[tuple[str, str]] = []
        if identifiers:
            for item in conn.execute("SELECT kind,normalized_value FROM identifiers WHERE document_id=?", (row["id"],)):
                key = (item["kind"], item["normalized_value"])
                if key in identifiers:
                    shared_id.append(key)
        exact_stem = bool(normalized and normalized == other_norm)
        if shared_id or exact_stem or (min(len(normalized), len(other_norm)) >= 12 and score >= min_similarity):
            reason = "shared bibliographic identifier" if shared_id else ("same normalized filename" if exact_stem else "similar normalized filename")
            matches.append({"relative_path": row["relative_path"], "sha256": row["sha256"], "similarity": round(score, 3),
                            "reason": reason, "same_stem": exact_stem, "shared_identifiers": shared_id,
                            "subject_id": row["subject_id"], "document_type": row["document_type"]})
    matches.sort(key=lambda x: (-bool(x["shared_identifiers"]), -x["similarity"], x["relative_path"]))
    return matches


def duplicate_report(conn: sqlite3.Connection) -> dict[str, Any]:
    exact_groups = []
    for row in conn.execute(
        "SELECT sha256,COUNT(*) AS n,GROUP_CONCAT(relative_path,char(10)) AS paths FROM documents WHERE state='present' AND sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1 ORDER BY n DESC"
    ):
        exact_groups.append({"sha256": row["sha256"], "count": row["n"], "paths": row["paths"].split("\n")})
    by_stem: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute("SELECT relative_path,filename,sha256 FROM documents WHERE state='present' ORDER BY relative_path"):
        stem = normalize_stem(row["filename"])
        if stem:
            by_stem.setdefault(stem, []).append({"relative_path": row["relative_path"], "sha256": row["sha256"]})
    potential_groups = []
    version_conflicts = []
    for stem, members in by_stem.items():
        unique_hashes = {m["sha256"] for m in members}
        if len(members) > 1 and len(unique_hashes) == 1:
            # Exact groups are reported independently; name this as same-name duplicate grouping too.
            continue
        if len(members) > 1:
            item = {"normalized_stem": stem, "files": members}
            potential_groups.append(item)
            if len(unique_hashes) > 1:
                version_conflicts.append(item)
    identifier_conflicts = []
    rows = conn.execute(
        "SELECT kind,normalized_value,COUNT(DISTINCT document_id) AS n,GROUP_CONCAT(document_id) AS ids FROM identifiers GROUP BY kind,normalized_value HAVING COUNT(DISTINCT document_id)>1"
    ).fetchall()
    for row in rows:
        identifier_conflicts.append({"kind": row["kind"], "value": row["normalized_value"], "document_ids": row["ids"].split(",")})
    return {"exact_duplicate_groups": exact_groups, "possible_duplicate_groups": potential_groups,
            "potential_version_conflicts": version_conflicts, "identifier_conflicts": identifier_conflicts}
