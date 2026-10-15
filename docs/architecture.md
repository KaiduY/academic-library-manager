# Architecture

## Runtime topology

The application is a single-user CLI process. It should run on the server (or another trusted machine) that can access both the library root and the incoming source path. It does not assume remote filesystem access to the Mac. SQLite and logs should live outside the academic document tree.

The dependency graph is intentionally small:

- `cli.py`: arguments, exit codes, orchestration and text/JSON presentation.
- `config.py`: TOML config loading and first-run config creation.
- `taxonomy.py`: editable taxonomy, validation, deterministic low-confidence classification.
- `filesystem.py`: read-only traversal, no-follow file hashing, filename heuristics and optional `pdfinfo` adapter.
- `database.py`: SQLite schema, transaction helpers, catalog upserts, identifiers/authors, and event/audit tables.
- `scanner.py`: stable scan-run lifecycle, incremental hashing, change events, missing-state reconciliation.
- `duplicates.py`: exact hash checks plus potential matches using normalized filename, similarity and available identifiers.
- `importer.py`: import plan, batch journal, verified temporary copy, no-clobber publication and recovery.
- `statistics.py`: state summaries and JSON/CSV/text output.
- `verification.py`: filesystem/catalog/database checks and SQLite backup.
- `logging_utils.py`: advisory process lock and append-only JSONL audit log.

## Database model

`documents` has one row per canonical relative path, with stable ID, filesystem fingerprint, SHA-256, inferred/embedded metadata, primary taxonomy subject and import provenance. `subjects` mirrors active and historically deprecated taxonomy nodes. `document_subjects` is the normalized primary/related classification table; `documents.subject_id` is a convenient primary-subject field. `authors`, `document_authors`, and `identifiers` normalize people and identifiers with their metadata source/confidence. `scan_runs` tracks scan completeness. `import_batches` and `import_items` are an import journal. `document_events` preserves observations such as indexing, changes, missing status and recovered imports. `operation_log` is an append-only in-application audit table. `taxonomy_changes` records explicit promotions and backup paths. `schema_migrations` records the schema version applied.

The catalog is not authoritative over the filesystem. A path record may be stale; verification reports rather than repairs such states. Database transactions cover batches of database changes, not filesystem changes, so every import first writes a durable journal item and then performs a separately verified file operation.

## Import state machine

For each source candidate, the planner records a SHA-256, initial destination proposal, match evidence and action. Dry-run records a plan but does not publish files. Apply records the plan, marks an item `copying`, creates a unique temporary sibling file, verifies its hash and source stability, then atomically creates the final name using `os.link(temp, destination)`. A hard link fails if any object already occupies the destination; normal import code never replaces it. The temporary name and destination are on the same filesystem even when the source lives on another mount.

After publication, the file is re-hashed, and only then is its document row and import status committed. If a process dies between publication and database commit, the journal remains `copying` or `failed`; `recover --apply` may reconcile the database if the destination checksum matches the recorded source checksum. If the target filesystem cannot support no-clobber hard-link publication, the application refuses to publish rather than use `rename`/copy semantics that might overwrite. A temporary file surviving a hard process crash is reported and left for review.

## Hashing model

An incremental scan reuses a stored SHA-256 only when path, size and `mtime_ns` match the previous observation. This avoids repeatedly hashing large PDFs. This is a performance cache, not a cryptographic guarantee that the bytes never changed: an external tool could rewrite a file with the same length and restore its mtime. `scan --full-hash` or `verify --full-hash` forces recomputation. Before using a stored hash to skip an import, candidate library matches are re-hashed. Files with errors leave the scan incomplete, which suppresses missing-file reconciliation.

## Classification model

One canonical primary subject controls the normal destination. Classification is deterministic keyword matching over relative path and filename, with low confidence and an explicit unclassified destination when no configured keyword matches. The application does not inspect PDF body content or silently ask an AI model. Related subject assignments can be added to the catalog with the explicit `tag --apply` command without copying the file again.
