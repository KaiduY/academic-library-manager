# Operations runbook

## First production deployment

1. Back up the actual `Academic_Library` directory independently.
2. Install the package in a controlled virtual environment. Run as the normal server account with library read/write permissions; do not default to root.
3. `academic-library init --library-root /actual/path/Academic_Library` using a new config path. Inspect every configured path. Keep SQLite, logs, and staging out of the library root.
4. `academic-library doctor` should pass configuration and SQLite checks. Any state-inside-library warning should be corrected before a full scan.
5. `academic-library backup --output /safe/path/catalog-before-scan.sqlite3` to preserve the initialized catalog state. Back up the documents themselves with a filesystem snapshot or regular backup.
6. `academic-library scan --full-hash`. It is read-only with respect to document files. Review `status`, warnings, and errors; `incomplete` is not a successful reconciliation.
7. `academic-library backup --output /safe/path/catalog-after-scan.sqlite3`.
8. Run `verify --full-hash`, `duplicates`, `stats`, and `taxonomy review`. Treat exact hash groups and heuristic conflicts separately.

## Routine weekly/monthly maintenance

```bash
academic-library scan
academic-library verify
academic-library duplicates --format json
academic-library stats --format json --output "$HOME/library-stats-$(date -u +%Y%m%d).json"
academic-library backup --output "$HOME/catalog-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
```

The commands above write reports/backups to new paths only. For a consistent scheduled routine, use cron or a `systemd` timer owned by the same user that owns the catalog; don't run two jobs simultaneously. Because a new output name is required, include a timestamp in report and backup destinations.

Example cron line (edit the paths/user environment for your system):

```cron
17 3 * * 0 /home/library-user/.venvs/academic-library/bin/academic-library --config /home/library-user/.config/academic-library/config.toml scan >> /home/library-user/academic-library-cron.log 2>&1
```

An ordinary scan uses cached checksums for files with unchanged size and `mtime_ns`. Schedule `scan --full-hash` periodically (for example monthly) if the archive is important, after disk incidents, or when external tools might preserve timestamps.

## New documents from another machine

- Copy incoming material to a separate server-side incoming folder using a transfer command that does not remove source files.
- Keep the original incoming folder until the report shows each candidate was imported, skipped as exact duplicate, or placed in review.
- Run `scan` before the import to refresh the catalog, then run `import --source ...` without `--apply` and review the JSON report.
- Execute the same command with `--apply`. Verify imported checksums and review the log and `90_Review_Queue` outcomes.
- Do not clean up the Mac, incoming directory, or existing library simply because an import command returned success. Independently verify the new archive and source preservation first.

## Troubleshooting

- `doctor` fails: check path spelling, permissions, taxonomy TOML syntax and database integrity.
- `scan` incomplete: resolve access/disconnection errors and rerun; do not use an incomplete scan's absence list to make manual deletions.
- `Destination ... symlink`: remove the symlink only after inspecting it manually, then rerun the dry run. The program refuses to follow it.
- `Filesystem does not support safe no-clobber hard-link publication`: use a supported local filesystem or explicitly stage the material onto a supported destination filesystem; no unsafe fallback is attempted.
- `copying`/`failed` import item: run `verify --full-hash`, then `recover` (preview) and `recover --apply` only if the reported source checksum is already present at the final destination.
- `unindexed_files`: review them; they may be manual additions or partial-operation artifacts. `scan` can index normal files once their placement is understood.
- `checksum_mismatches`: do not overwrite or delete anything. Check external changes and backups, then re-run a full scan to update the catalog only if the on-disk version is the one you intend to retain.

## Database backup/restore

Use the built-in `backup` command, which uses SQLite's backup API and checks the output integrity. It refuses to replace an existing destination. For restore, stop the application, copy a known-good backup to a new database file, confirm `PRAGMA integrity_check`, and adjust the configured `database_path`. Preserve the previous catalog file until the restored catalog has been verified. Always maintain a separate backup of the documents themselves.
