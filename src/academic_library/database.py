"""SQLite schema, migrations, catalog upserts, and append-only audit helpers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def initialize_database(db_path: Path | str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.commit()
        current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema version {current} is newer than this application supports ({SCHEMA_VERSION})")
        migrations = {1: _MIGRATION_1}
        for version in range(current + 1, SCHEMA_VERSION + 1):
            script = migrations.get(version)
            if script is None:
                raise RuntimeError(f"No migration is defined for schema version {version}")
            # Schema statements and their version marker commit together, so an interrupted migration can be retried.
            try:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n" + script +
                    f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{utc_now()}');\nCOMMIT;"
                )
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


_MIGRATION_1 = r"""
CREATE TABLE IF NOT EXISTS subjects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT REFERENCES subjects(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    relative_path TEXT NOT NULL,
    deprecated INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),
    taxonomy_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','completed','incomplete','failed')),
    full_hash INTEGER NOT NULL CHECK(full_hash IN (0,1)),
    files_seen INTEGER NOT NULL DEFAULT 0,
    files_indexed INTEGER NOT NULL DEFAULT 0,
    files_changed INTEGER NOT NULL DEFAULT 0,
    missing_marked INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source_root TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('dry-run','apply')),
    status TEXT NOT NULL CHECK(status IN ('planned','running','completed','partial','failed','dry-run')),
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    file_type TEXT NOT NULL,
    title TEXT,
    title_source TEXT,
    title_confidence REAL CHECK(title_confidence IS NULL OR (title_confidence >= 0 AND title_confidence <= 1)),
    publication_year INTEGER CHECK(publication_year IS NULL OR (publication_year >= 1400 AND publication_year <= 2200)),
    publication_year_source TEXT,
    publication_year_confidence REAL CHECK(publication_year_confidence IS NULL OR (publication_year_confidence >= 0 AND publication_year_confidence <= 1)),
    edition TEXT,
    edition_source TEXT,
    edition_confidence REAL CHECK(edition_confidence IS NULL OR (edition_confidence >= 0 AND edition_confidence <= 1)),
    subject_id TEXT REFERENCES subjects(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    document_type TEXT,
    classification_source TEXT,
    classification_confidence REAL CHECK(classification_confidence IS NULL OR (classification_confidence >= 0 AND classification_confidence <= 1)),
    metadata_status TEXT NOT NULL DEFAULT 'filename_only',
    first_seen_at TEXT NOT NULL,
    last_indexed_at TEXT NOT NULL,
    last_seen_scan_id TEXT REFERENCES scan_runs(id),
    state TEXT NOT NULL DEFAULT 'present' CHECK(state IN ('present','missing')),
    import_batch_id TEXT REFERENCES import_batches(id),
    imported_from_path TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY,
    normalized_name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_authors (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE RESTRICT,
    role TEXT NOT NULL DEFAULT 'author',
    source TEXT NOT NULL,
    confidence REAL CHECK(confidence >= 0 AND confidence <= 1),
    PRIMARY KEY(document_id, author_id, role)
);
CREATE TABLE IF NOT EXISTS identifiers (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL CHECK(confidence >= 0 AND confidence <= 1),
    UNIQUE(document_id, kind, normalized_value)
);
CREATE TABLE IF NOT EXISTS document_subjects (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    subject_id TEXT NOT NULL REFERENCES subjects(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK(role IN ('primary','related')),
    source TEXT NOT NULL,
    confidence REAL CHECK(confidence >= 0 AND confidence <= 1),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(document_id, subject_id, role)
);
CREATE INDEX IF NOT EXISTS idx_document_subjects_subject ON document_subjects(subject_id,role);
CREATE TABLE IF NOT EXISTS document_events (
    id INTEGER PRIMARY KEY,
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    event_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_path TEXT,
    new_path TEXT,
    old_sha256 TEXT,
    new_sha256 TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS import_items (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id) ON DELETE RESTRICT,
    source_path TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    source_sha256 TEXT,
    proposed_relative_path TEXT,
    destination_relative_path TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    match_path TEXT,
    match_reason TEXT,
    error TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(batch_id, source_path)
);
CREATE TABLE IF NOT EXISTS taxonomy_changes (
    id INTEGER PRIMARY KEY,
    changed_at TEXT NOT NULL,
    old_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    old_config_backup_path TEXT NOT NULL,
    new_config_path TEXT NOT NULL,
    change_summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operation_log (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    command TEXT NOT NULL,
    mode TEXT NOT NULL,
    batch_id TEXT,
    source_path TEXT,
    destination_path TEXT,
    operation TEXT NOT NULL,
    checksum TEXT,
    result TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_state ON documents(state);
CREATE INDEX IF NOT EXISTS idx_documents_subject ON documents(subject_id);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(document_type);
CREATE INDEX IF NOT EXISTS idx_documents_year ON documents(publication_year);
CREATE INDEX IF NOT EXISTS idx_identifiers_lookup ON identifiers(kind, normalized_value);
CREATE INDEX IF NOT EXISTS idx_import_items_status ON import_items(status);
CREATE INDEX IF NOT EXISTS idx_operation_log_time ON operation_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_document_events_time ON document_events(event_at);
"""


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _sync_taxonomy_rows(conn: sqlite3.Connection, taxonomy: Any) -> None:
    now = utc_now()
    # Parents first: sort by path depth so FK references are available even with immediate checks.
    for subject in sorted(taxonomy.subjects.values(), key=lambda x: (x.path.count("/"), x.id)):
        conn.execute(
            """INSERT INTO subjects(id,name,parent_id,relative_path,deprecated,taxonomy_version,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,parent_id=excluded.parent_id,
               relative_path=excluded.relative_path,deprecated=excluded.deprecated,
               taxonomy_version=excluded.taxonomy_version,updated_at=excluded.updated_at""",
            (subject.id, subject.name, subject.parent_id, subject.path, int(subject.deprecated), taxonomy.version, now),
        )
    active_ids = tuple(taxonomy.subjects)
    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        conn.execute(
            f"UPDATE subjects SET deprecated=1,taxonomy_version=?,updated_at=? WHERE id NOT IN ({placeholders})",
            (taxonomy.version, now, *active_ids),
        )


def sync_taxonomy(conn: sqlite3.Connection, taxonomy: Any) -> None:
    """Upsert a taxonomy; reuse an existing transaction when called from a larger atomic operation."""
    if conn.in_transaction:
        _sync_taxonomy_rows(conn, taxonomy)
    else:
        with transaction(conn):
            _sync_taxonomy_rows(conn, taxonomy)


def add_operation(
    conn: sqlite3.Connection,
    *, command: str,
    mode: str,
    operation: str,
    result: str,
    batch_id: str | None = None,
    source_path: str | None = None,
    destination_path: str | None = None,
    checksum: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO operation_log(timestamp,command,mode,batch_id,source_path,destination_path,
           operation,checksum,result,details_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (utc_now(), command, mode, batch_id, source_path, destination_path, operation, checksum, result,
         json.dumps(details or {}, ensure_ascii=False, sort_keys=True)),
    )


def upsert_document(
    conn: sqlite3.Connection,
    *, relative_path: str,
    filename: str,
    extension: str,
    size_bytes: int,
    mtime_ns: int,
    sha256: str | None,
    file_type: str,
    title: str | None,
    title_source: str | None,
    title_confidence: float | None,
    publication_year: int | None,
    publication_year_source: str | None,
    publication_year_confidence: float | None,
    edition: str | None,
    edition_source: str | None,
    edition_confidence: float | None,
    subject_id: str | None,
    document_type: str | None,
    classification_source: str | None,
    classification_confidence: float | None,
    metadata_status: str,
    scan_id: str | None = None,
    import_batch_id: str | None = None,
    imported_from_path: str | None = None,
) -> tuple[int, bool, bool]:
    """Insert/update a path. Return document id, was_new, was_changed."""
    now = utc_now()
    old = conn.execute("SELECT * FROM documents WHERE relative_path=?", (relative_path,)).fetchone()
    fields = {
        "size_bytes": size_bytes, "mtime_ns": mtime_ns, "sha256": sha256, "file_type": file_type,
        "title": title, "title_source": title_source, "title_confidence": title_confidence,
        "publication_year": publication_year, "publication_year_source": publication_year_source,
        "publication_year_confidence": publication_year_confidence, "edition": edition,
        "edition_source": edition_source, "edition_confidence": edition_confidence, "subject_id": subject_id,
        "document_type": document_type, "classification_source": classification_source,
        "classification_confidence": classification_confidence, "metadata_status": metadata_status,
        "filename": filename, "extension": extension,
    }
    new = old is None
    changed = old is not None and any(old[k] != v for k, v in fields.items())
    if old is None:
        cur = conn.execute(
            """INSERT INTO documents(relative_path,filename,extension,size_bytes,mtime_ns,sha256,file_type,
               title,title_source,title_confidence,publication_year,publication_year_source,publication_year_confidence,
               edition,edition_source,edition_confidence,subject_id,document_type,classification_source,
               classification_confidence,metadata_status,first_seen_at,last_indexed_at,last_seen_scan_id,state,
               import_batch_id,imported_from_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'present',?,?)""",
            (relative_path, filename, extension, size_bytes, mtime_ns, sha256, file_type, title, title_source,
             title_confidence, publication_year, publication_year_source, publication_year_confidence, edition,
             edition_source, edition_confidence, subject_id, document_type, classification_source,
             classification_confidence, metadata_status, now, now, scan_id, import_batch_id, imported_from_path),
        )
        doc_id = int(cur.lastrowid)
        conn.execute("INSERT INTO document_events(document_id,event_at,event_type,new_path,new_sha256,details_json) VALUES(?,?,?,?,?,?)",
                     (doc_id, now, "indexed", relative_path, sha256, "{}"))
    else:
        doc_id = int(old["id"])
        conn.execute(
            """UPDATE documents SET filename=?,extension=?,size_bytes=?,mtime_ns=?,sha256=?,file_type=?,title=?,
               title_source=?,title_confidence=?,publication_year=?,publication_year_source=?,publication_year_confidence=?,
               edition=?,edition_source=?,edition_confidence=?,subject_id=?,document_type=?,
               classification_source=?,classification_confidence=?,metadata_status=?,last_indexed_at=?,
               last_seen_scan_id=COALESCE(?,last_seen_scan_id),state='present',last_error=NULL,
               import_batch_id=COALESCE(?,import_batch_id),imported_from_path=COALESCE(?,imported_from_path)
               WHERE id=?""",
            (filename, extension, size_bytes, mtime_ns, sha256, file_type, title, title_source, title_confidence,
             publication_year, publication_year_source, publication_year_confidence, edition, edition_source,
             edition_confidence, subject_id, document_type, classification_source,
             classification_confidence, metadata_status, now, scan_id, import_batch_id, imported_from_path, doc_id),
        )
        if changed:
            conn.execute(
                "INSERT INTO document_events(document_id,event_at,event_type,old_path,new_path,old_sha256,new_sha256,details_json) VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, now, "indexed_change", relative_path, relative_path, old["sha256"], sha256,
                 json.dumps({"old_size": old["size_bytes"], "new_size": size_bytes}, sort_keys=True)),
            )
    if subject_id is not None:
        # Keep the normalized current primary assignment synchronized with the convenience column.
        conn.execute("DELETE FROM document_subjects WHERE document_id=? AND role='primary' AND subject_id<>?", (doc_id, subject_id))
        conn.execute("DELETE FROM document_subjects WHERE document_id=? AND role='related' AND subject_id=?", (doc_id, subject_id))
        conn.execute(
            """INSERT INTO document_subjects(document_id,subject_id,role,source,confidence,assigned_at)
               VALUES(?,?,'primary',?,?,?) ON CONFLICT(document_id,subject_id,role) DO UPDATE SET
               source=excluded.source,confidence=excluded.confidence,assigned_at=excluded.assigned_at""",
            (doc_id, subject_id, classification_source or "unknown", classification_confidence, now),
        )
    return doc_id, new, changed


def store_identifiers(conn: sqlite3.Connection, document_id: int, identifiers: list[dict[str, Any]]) -> None:
    # Preserve already extracted identifiers; insert new observations without destructive refreshes.
    for ident in identifiers:
        conn.execute(
            """INSERT OR IGNORE INTO identifiers(document_id,kind,value,normalized_value,source,confidence)
               VALUES(?,?,?,?,?,?)""",
            (document_id, ident["kind"], ident["value"], ident["normalized_value"], ident["source"], ident["confidence"]),
        )


def add_related_subject(conn: sqlite3.Connection, relative_path: str, subject_id: str, source: str = "manual") -> dict[str, Any]:
    row = conn.execute("SELECT id,subject_id,sha256 FROM documents WHERE relative_path=? AND state='present'", (relative_path,)).fetchone()
    if not row:
        raise ValueError(f"No present indexed document at path: {relative_path}")
    subject = conn.execute("SELECT id,name,deprecated FROM subjects WHERE id=?", (subject_id,)).fetchone()
    if not subject:
        raise ValueError(f"Unknown subject id: {subject_id}")
    if subject["deprecated"]:
        raise ValueError(f"Subject {subject_id!r} is deprecated and cannot be newly assigned")
    if row["subject_id"] == subject_id:
        return {"relative_path": relative_path, "subject_id": subject_id, "status": "already_primary"}
    now = utc_now()
    conn.execute(
        """INSERT INTO document_subjects(document_id,subject_id,role,source,confidence,assigned_at)
           VALUES(?,?,'related',?,1.0,?) ON CONFLICT(document_id,subject_id,role) DO UPDATE SET
           source=excluded.source,confidence=excluded.confidence,assigned_at=excluded.assigned_at""",
        (row["id"], subject_id, source, now),
    )
    add_operation(conn, command="tag", mode="apply", source_path=relative_path, checksum=row["sha256"],
                  operation="add_related_subject", result="ok", details={"subject_id": subject_id, "subject_name": subject["name"]})
    conn.commit()
    return {"relative_path": relative_path, "subject_id": subject_id, "subject_name": subject["name"], "status": "related_subject_added"}
