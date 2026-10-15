# Safety model and failure semantics

## Filesystem invariants

1. The source tree is opened read-only. No source delete/rename/move/chmod/write is issued by routine commands.
2. Existing library documents are never opened for writing by scan/import/verify/taxonomy operations.
3. Import destination paths must be relative and contain no `.` or `..` components. Symlink roots and destination parent components are rejected.
4. Import publication uses a sibling temporary and no-clobber hard link. Existing destinations cause a skipped duplicate only after a matching hash, or a review/unresolved outcome. No ordinary rename is used to publish a final file.
5. Existing source/destination folders are not reorganized automatically; subject mismatch results are reports only.
6. Incomplete scans never mark files missing. Complete scans may mark an indexed row missing, but never delete that row or recreate the file.
7. A missing or failed verification result triggers a report, not a repair that might overwrite evidence.
8. Output reports and backups use exclusive creation and refuse to overwrite existing reports/backups.

## Import decision priority

- Compute source SHA-256; errors are surfaced.
- Re-hash database records whose cached SHA-256 matches before choosing an exact match.
- If exact content already exists in the library or earlier in the same batch, skip the copy and record why.
- If a normalized filename or available identifier suggests the same work but bytes differ, route it to a hash-keyed review folder. This is a candidate relationship, not a declaration that it is an edition.
- If no likely match exists and the taxonomy path is clear, copy to the subject path.
- If the destination appeared concurrently, no-clobber publication fails; the incoming source remains untouched and the result is unresolved.
- If the deterministic review destination itself already holds different bytes, do not invent a numeric suffix. Leave the source in place and report unresolved.

## SQLite and process concurrency

Mutating scan, import, tag, taxonomy promotion, recovery and verification use an advisory lock file adjacent to the database on macOS/Linux. A second mutating invocation fails promptly rather than sharing an unsafe plan. SQLite `foreign_keys` and `busy_timeout` are enabled. Every scan and import is journaled in the database. The normal operational interface only appends to `operation_log`; status tables are updated as state transitions occur.

The advisory lock coordinates instances of this application, not arbitrary external programs. Someone manually editing/moving files concurrently can still create a discrepancy. No-clobber publication and post-copy hashes defend the most important import race, and the next scan/verify reports external changes.

## Failure interpretation

- **Scan `incomplete`:** one or more directory/file operations failed or a symlink/special item was skipped. Existing records remain; missing reconciliation is suppressed.
- **Import `partial`:** some candidates were copied/skipped, but at least one item failed or could not be safely placed. Consult the JSON report, `import_items`, `verify`, and `recover`.
- **`copying` journal item:** likely interrupted while copying/publishing. Recovery only marks it complete when the destination already exists and matches the recorded checksum.
- **`failed` item with matching destination:** recovery may reconcile the database if a final file was published before an exception. If its hash differs, it is not auto-repaired.
- **Unindexed file:** not automatically considered unwanted; it may be a manual addition or an interrupted import. Scan after reviewing it.
- **Unexpected location:** taxonomy suggestion only. No move occurs.

## Limitations

- Metadata extracted from filenames and PDF metadata can be wrong. `title_source`, `title_confidence`, field source/confidence and `metadata_status` indicate how values were obtained. No internet query is performed.
- Incremental scan hash cache can be stale if a file's bytes change while its size and nanosecond mtime are preserved. Full hashing closes this gap.
- SQLite and the filesystem cannot share a single atomic transaction. Import journaling and `recover` make common interruption states observable, but manual review can still be necessary.
- Atomic hard-link publication requires a destination filesystem that supports hard links. Unsupported filesystems fail closed.
- Application locks do not coordinate with external file managers or scripts. Avoid editing a collection while a large import/scan is underway.
