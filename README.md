# Academic Library Manager

A local Python 3 command-line catalog and maintenance tool for a long-lived academic file archive. It uses SQLite for metadata and TOML for configuration and taxonomy. The runtime has **no mandatory third-party dependencies**; `pytest` and `ruff` are optional development tools.

The application is deliberately conservative. It does not rename, move, edit, replace, or delete existing academic documents. Routine import is additive, defaults to dry run, and never overwrites a destination.

## Features

- SQLite catalog with schema versioning, filesystem facts, metadata provenance/confidence, identifiers, normalized authors, primary and related subject assignments, scan history, import batches, file events, and an append-only operation log.
- Incremental scanning (reuse a cached hash only when size and nanosecond mtime still match) and `--full-hash` verification.
- Safe source-directory imports, exact SHA-256 duplicate detection, conservative filename/identifier-based match suggestions, no-clobber publication, checksum verification, provenance, and interrupted-import recovery.
- Editable subject taxonomy with validation, drift recommendations, reviewable comparison, and an explicitly approved promotion workflow that keeps a timestamped backup.
- Search, duplicate reports, collection statistics, JSON/CSV exports, integrity verification, database backup, diagnostics, and recovery.

## Requirements

- Python **3.11 or later** (the standard library TOML parser is used).
- A filesystem on which the application can read source files and create library files.
- Safe import publication uses a temporary file in the destination directory and an atomic hard-link operation that fails if the final destination already exists. If a destination filesystem does not support hard links, the import is refused rather than falling back to an overwrite-prone operation.
- Optional: `pdfinfo` (for example, Debian package `poppler-utils`) to read embedded PDF title and author fields. Without it, imports still work; filenames are only low-confidence metadata.
- Optional development tools: `pytest` and `ruff`.

## Installation

From the project directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test,lint]'
```

For a runtime-only install, use `python -m pip install -e .`. The command is then available as `academic-library`. You can also run it without installation with `PYTHONPATH=src python -m academic_library ...`.

On Debian, the optional PDF metadata tool can be installed with:

```bash
sudo apt install poppler-utils
```

Then set `metadata.pdfinfo_command = "/usr/bin/pdfinfo"` in the config. PDF metadata is untrusted embedded metadata, not proof of bibliographic truth. The program does not parse the body of PDFs or fetch metadata over the internet.

## Initial setup for an existing archive

Run this on the machine that hosts and can read/write `Academic_Library` (for your setup, probably the Debian server). The root directory must already exist. **Do not point the application at your real collection until you have an independent filesystem backup.**

```bash
academic-library --config ~/.config/academic-library/config.toml init \
  --library-root "/path/to/Academic_Library"
```

Replace the example root with the actual path. `init` creates a config and a copy of the default `taxonomy.toml` beside it, initializes the SQLite database outside the library by default, and does not scan or change archive documents. Existing config files are never overwritten by `init`; edit the TOML explicitly to change paths.

Review `~/.config/academic-library/config.toml`. It contains:

```toml
[paths]
library_root = "/path/to/Academic_Library"
database_path = "/home/your-user/.local/share/academic-library/catalog.sqlite3"
taxonomy_path = "/home/your-user/.config/academic-library/taxonomy.toml"
staging_directory = "/home/your-user/.local/share/academic-library/staging"
log_directory = "/home/your-user/.local/state/academic-library/logs"

[scan]
ignore_directories = [".git", "__pycache__", ".venv", "node_modules"]

[metadata]
pdfinfo_command = ""
```

Keep the database, logs and staging area outside `Academic_Library` where possible. `doctor` warns if operational state is placed inside the library. The directory tree is data; the SQLite database is a catalog, not a backup of the documents.

### Back up before the first production scan

1. Back up `Academic_Library` itself using your normal server backup/snapshot process. The application never performs that filesystem backup for you.
2. Initialize the database, then take a baseline database backup (this captures the empty catalog and is useful for recovery of configuration/setup mistakes):

   ```bash
   academic-library --config ~/.config/academic-library/config.toml backup \
     --output "$HOME/catalog-before-first-scan.sqlite3"
   ```

3. Index the existing collection and compute every SHA-256 checksum:

   ```bash
   academic-library --config ~/.config/academic-library/config.toml scan --full-hash
   ```

4. Review the output, then back up the populated catalog too:

   ```bash
   academic-library --config ~/.config/academic-library/config.toml backup \
     --output "$HOME/catalog-after-first-scan.sqlite3"
   ```

Backup destinations must not already exist. After that, ordinary `scan` is incremental; use `scan --full-hash` periodically or after an unexpected disk/event/copy problem. A normal scan treats equal size plus equal `mtime_ns` as a cache hit, so a deliberately modified file with its timestamp and size preserved can evade that quick path until a full-hash scan or full verification.

## Routine workflow

### Scan and verify

```bash
academic-library --config ~/.config/academic-library/config.toml scan
academic-library --config ~/.config/academic-library/config.toml verify
academic-library --config ~/.config/academic-library/config.toml verify --full-hash --format json
```

Scanning updates the catalog only. It never relocates files. A file that disappears is marked `missing` and retained in the database. If any directory traversal or file-hash error makes the scan incomplete, the application does not mark other records missing during that scan. Symlinks and special files are skipped and reported; symlink-related skips also prevent “missing” reconciliation.

### Import a new folder

`--source` must be a folder visible to the machine running the program, and not the library itself or a directory nested inside/containing the library. Both the source and application state directories are protected against being placed inside the source.

Preview first (default):

```bash
academic-library --config ~/.config/academic-library/config.toml import \
  --source "/path/to/new/material" \
  --report "$HOME/import-preview-2026-10-15.json"
```

Review each planned action, then apply explicitly:

```bash
academic-library --config ~/.config/academic-library/config.toml import \
  --source "/path/to/new/material" --apply \
  --report "$HOME/import-applied-2026-10-15.json"
```

The report path must be new and outside both the source and library. Run `scan` before imports so documents already in the library are represented in the catalog. The importer rechecks a database candidate's actual hash before treating it as an exact duplicate; a full scan is the best way to refresh the full catalog first.

Import decisions:

- **Exact SHA-256 match:** skip the copy and record the matching library path.
- **Same filename or similar title, different checksum:** treat as a possible version or identity conflict and preserve the incoming bytes in `90_Review_Queue/Version_Conflicts/` or `90_Review_Queue/Possible_Duplicates/` for review.
- **Uncertain classification:** preserve the incoming file under `90_Review_Queue/Unclassified/`.
- **New, classified file with an available destination:** copy into the taxonomy's subject path.
- **A destination occupied by different content:** never replace it. Route the incoming candidate to review, or report unresolved if the review destination itself is occupied by different content.

Original source files remain in place. Existing archive files are never overwritten by the importer. Version candidates are not declared to be different editions as a fact based only on their filenames; reports explain that the match is heuristic.

If the source is on another machine, first copy it to a separate incoming directory on the server (for example using `scp` or `rsync` without `--remove-source-files` or `--delete`). Run the import command on the server. This application does not connect to your Mac remotely and does not expect both machines to be directly mounted to one another.

### Duplicates, statistics, search, status

```bash
academic-library --config ~/.config/academic-library/config.toml duplicates
academic-library --config ~/.config/academic-library/config.toml duplicates --format json
academic-library --config ~/.config/academic-library/config.toml stats
academic-library --config ~/.config/academic-library/config.toml stats --format json
academic-library --config ~/.config/academic-library/config.toml stats --format csv --output "$HOME/library-stats.csv"
academic-library --config ~/.config/academic-library/config.toml search "quantum groups"
academic-library --config ~/.config/academic-library/config.toml search --subject math.topology --year 2020 --format json
academic-library --config ~/.config/academic-library/config.toml search --identifier 10.1234/example
academic-library --config ~/.config/academic-library/config.toml status
academic-library --config ~/.config/academic-library/config.toml doctor
```

The search index covers known metadata, filenames, paths, authors extracted from PDF metadata, identifiers found in filenames, and subject names. It does **not** index PDF body text. All metadata fields retain source/confidence where applicable; title or year inferred from a filename is not a verified bibliographic fact.

### Interdisciplinary documents

Every document has one primary subject and canonical filesystem location. Add a related catalog subject without making a second copy:

```bash
academic-library --config ~/.config/academic-library/config.toml tag \
  --path "01_Mathematics/Topology/Example.pdf" --subject math.geometry

academic-library --config ~/.config/academic-library/config.toml tag \
  --path "01_Mathematics/Topology/Example.pdf" --subject math.geometry --apply
```

The first command is a preview; `--apply` records a `related` subject assignment in SQLite. It does not move or rename the document. Search with `--subject` includes both primary and related subjects.

### Taxonomy validation and updates

The active taxonomy is an editable TOML file. See `config/taxonomy.toml` for subject IDs, parent IDs, paths, keywords, document types and review category. Use stable IDs; don't repurpose an old ID for a different field of study.

```bash
academic-library --config ~/.config/academic-library/config.toml taxonomy validate
academic-library --config ~/.config/academic-library/config.toml taxonomy review
academic-library --config ~/.config/academic-library/config.toml taxonomy compare --candidate "$HOME/taxonomy-candidate.toml"
academic-library --config ~/.config/academic-library/config.toml taxonomy promote --candidate "$HOME/taxonomy-candidate.toml"
```

Promotion without `--apply` is only a preview. After reviewing the diff, promote explicitly:

```bash
academic-library --config ~/.config/academic-library/config.toml taxonomy promote \
  --candidate "$HOME/taxonomy-candidate.toml" --apply
```

This preserves the prior config as a timestamped `taxonomy.toml.backup-*`, records the change in SQLite and the JSONL audit log, and updates taxonomy metadata. It never moves or renames existing files. Use `verify` and `taxonomy review` after a taxonomy update to inspect deprecated assignments and unexpected locations. Suggestions are based on configured keywords, file names and paths only, not document contents or an LLM.

### Recovery, export, backup and restore

```bash
academic-library --config ~/.config/academic-library/config.toml verify --full-hash
academic-library --config ~/.config/academic-library/config.toml recover
academic-library --config ~/.config/academic-library/config.toml recover --apply
academic-library --config ~/.config/academic-library/config.toml export --format json --output "$HOME/catalog.json"
academic-library --config ~/.config/academic-library/config.toml backup --output "$HOME/catalog-backup.sqlite3"
```

`recover` defaults to a report-only mode. `recover --apply` updates database state only when an import journal has a destination whose current checksum matches the recorded source checksum. It does not copy, delete, or replace files. Failed or partial items with mismatched bytes remain for manual inspection. Temporary artifacts are reported by `verify`; the program never silently cleans them up.

To restore a catalog backup, stop other application operations and copy the verified backup to a *new* database path first. After checking it with `sqlite3 BACKUP.sqlite3 'PRAGMA integrity_check;'`, update `database_path` in the TOML config to the restored file. Do not overwrite a production database in place without making a separate copy of the existing database and preserving the old file. The archive files need their own filesystem backup and are not restored by this command.

## Tests and development

```bash
python -m pip install -e '.[test,lint]'
pytest -q
ruff check src tests
python -m compileall -q src tests
```

Tests create isolated temporary trees and databases. They do not point to a real collection. The application should be run as a regular user with the required permissions, not as root by default.

## What it will never do automatically

- Delete, rename, move, or edit an imported source file.
- Overwrite an existing library destination.
- Replace an older version with a newer one.
- Delete duplicates, missing-file records, old database events or empty directories.
- Make a permanent taxonomy update without `taxonomy promote --apply`.
- Move an existing document after a taxonomy change.
- Treat fuzzy filename similarity as proof of document identity.

## Verification report

The actual automated and disposable end-to-end checks performed for this release, including the checks that could not be run, are recorded in [`docs/verification-report.md`](docs/verification-report.md).
