"""Read-only filesystem inventory, hashing, and conservative metadata extraction."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable


@dataclass
class FileCandidate:
    path: Path
    relative_path: str
    filename: str
    extension: str
    size_bytes: int
    mtime_ns: int
    sha256: str | None = None
    file_type: str = "unknown"
    title: str | None = None
    title_source: str | None = None
    title_confidence: float | None = None
    authors: list[str] = field(default_factory=list)
    publication_year: int | None = None
    publication_year_source: str | None = None
    publication_year_confidence: float | None = None
    edition: str | None = None
    edition_source: str | None = None
    edition_confidence: float | None = None
    metadata_status: str = "filename_only"
    identifiers: list[dict[str, object]] = field(default_factory=list)


@dataclass
class InventoryResult:
    files: list[FileCandidate]
    errors: list[str]
    warnings: list[str]
    complete: bool


def sha256_file(path: Path | str, chunk_size: int = 1024 * 1024) -> str:
    """Hash a regular file without following a last-component symlink where supported."""
    file_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(file_path, flags)
    try:
        stat_result = os.fstat(fd)
        if not __import__("stat").S_ISREG(stat_result.st_mode):
            raise OSError(f"Not a regular file: {file_path}")
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for block in iter(lambda: handle.read(chunk_size), b""):
                digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(fd)


def file_type_for(path: Path) -> str:
    ext = path.suffix.casefold()
    if ext == ".pdf":
        return "application/pdf"
    if ext in {".djvu", ".djv"}:
        return "image/vnd.djvu"
    if ext in {".epub"}:
        return "application/epub+zip"
    if ext in {".tex", ".bib", ".md", ".txt", ".rst"}:
        return "text/document-source"
    if ext in {".py", ".ipynb", ".r", ".jl"}:
        return "text/code"
    if ext in {".csv", ".tsv", ".json", ".xlsx", ".xls"}:
        return "data"
    if ext in {".mp4", ".mkv", ".mov", ".avi"}:
        return "video"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
        return "image"
    if ext in {".zip", ".tar", ".gz", ".7z"}:
        return "archive"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "unknown"


def extract_filename_metadata(candidate: FileCandidate) -> None:
    stem = Path(candidate.filename).stem
    clean = re.sub(r"[._]+", " ", stem).strip()
    candidate.title = clean or stem
    candidate.title_source = "filename"
    candidate.title_confidence = 0.35 if clean else 0.1
    years = [int(match) for match in re.findall(r"(?<!\d)(1[4-9]\d{2}|20\d{2}|21\d{2})(?!\d)", stem)]
    if years:
        # Filename year is only a heuristic; do not call it verified bibliographic metadata.
        candidate.publication_year = years[-1]
        candidate.publication_year_source = "filename"
        candidate.publication_year_confidence = 0.45
    edition_match = re.search(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:ed(?:ition)?\.?|edition)\b", stem)
    if edition_match:
        candidate.edition = f"{edition_match.group(1)} edition"
        candidate.edition_source = "filename"
        candidate.edition_confidence = 0.45
    text = stem
    doi_matches = re.findall(r"(?i)\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", text)
    for value in doi_matches:
        normalized = value.rstrip(".,;)").lower()
        candidate.identifiers.append({"kind": "doi", "value": normalized, "normalized_value": normalized,
                                       "source": "filename", "confidence": 0.75})
    arxiv_matches = re.findall(r"(?i)(?:arxiv[:._ -]*)?((?:\d{4}\.\d{4,5})(?:v\d+)?|[a-z-]+/\d{7}(?:v\d+)?)", text)
    for value in arxiv_matches:
        normalized = value.lower()
        candidate.identifiers.append({"kind": "arxiv", "value": normalized, "normalized_value": normalized,
                                       "source": "filename", "confidence": 0.70})
    # Record ISBN-shaped candidates without pretending the digits have been validated against the PDF.
    for value in re.findall(r"(?<!\d)(?:97[89][ -]?\d[ -]?(?:\d[ -]?){9,10}\d|\d(?:[ -]?\d){8,12}[\dXx])(?!\d)", text):
        normalized = re.sub(r"[^0-9Xx]", "", value).upper()
        if len(normalized) in (10, 13):
            candidate.identifiers.append({"kind": "isbn_candidate", "value": normalized, "normalized_value": normalized,
                                           "source": "filename", "confidence": 0.55})
    candidate.metadata_status = "filename_heuristic"


def enrich_with_pdfinfo(candidate: FileCandidate, command: str | None) -> None:
    """Optionally read embedded PDF metadata via configured pdfinfo; no source changes are made."""
    if not command or candidate.extension.casefold() != ".pdf":
        return
    executable = shutil.which(command)
    if not executable:
        return
    try:
        completed = subprocess.run([executable, str(candidate.path)], capture_output=True, text=True,
                                   timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return
    if completed.returncode != 0:
        return
    fields = {}
    for line in completed.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().casefold()] = value.strip()
    title = fields.get("title", "")
    if title and title.casefold() not in {"untitled", "(none)"}:
        candidate.title = title
        candidate.title_source = "embedded_pdf_metadata"
        candidate.title_confidence = 0.75
        candidate.metadata_status = "embedded_metadata"
    author = fields.get("author", "")
    if author and author.casefold() not in {"(none)", "unknown"}:
        candidate.authors = [x.strip() for x in re.split(r"\s*;\s*|\s+and\s+", author) if x.strip()]


def inspect_file(path: Path, relative_path: str, pdfinfo_command: str | None = None,
                 compute_hash: bool = True) -> FileCandidate:
    if path.is_symlink():
        raise OSError(f"Refusing symlink file: {path}")
    stat = path.stat(follow_symlinks=False)
    if not __import__("stat").S_ISREG(stat.st_mode):
        raise OSError(f"Not a regular file: {path}")
    candidate = FileCandidate(
        path=path, relative_path=PurePosixPath(relative_path).as_posix(), filename=path.name,
        extension=path.suffix.casefold(), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
        file_type=file_type_for(path),
    )
    extract_filename_metadata(candidate)
    enrich_with_pdfinfo(candidate, pdfinfo_command)
    if compute_hash:
        candidate.sha256 = sha256_file(path)
    return candidate


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def inventory_tree(root: Path | str, *, ignore_directories: Iterable[str] = (),
                   excluded_paths: Iterable[Path | str] = ()) -> InventoryResult:
    base = Path(root).absolute()
    errors: list[str] = []
    warnings: list[str] = []
    files: list[FileCandidate] = []
    if not base.is_dir():
        return InventoryResult([], [f"Not a readable directory: {base}"], [], False)
    exclusions = [Path(p).absolute() for p in excluded_paths]
    ignored = set(ignore_directories)

    def onerror(exc: OSError) -> None:
        errors.append(f"Directory traversal error: {exc.filename or base}: {exc}")

    for current, dirnames, filenames in os.walk(base, topdown=True, followlinks=False, onerror=onerror):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            child = current_path / dirname
            if dirname in ignored:
                continue
            if child.is_symlink():
                warnings.append(f"Skipped symlink directory: {child}")
                continue
            if any(child == x or _is_relative_to(child, x) for x in exclusions):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            path = current_path / filename
            if path.name.startswith(".academic-library-tmp-") or path.name.endswith(".partial"):
                warnings.append(f"Skipped possible incomplete temporary file: {path}")
                continue
            if any(path == x for x in exclusions):
                continue
            try:
                if path.is_symlink():
                    warnings.append(f"Skipped symlink file: {path}")
                    continue
                stat = path.stat(follow_symlinks=False)
                if not path.is_file():
                    warnings.append(f"Skipped non-regular file: {path}")
                    continue
                rel = path.relative_to(base).as_posix()
                # Scanning is inventory-only; hashes are filled by the scanner based on its cache.
                candidate = inspect_file(path, rel, compute_hash=False)
                candidate.size_bytes = stat.st_size
                candidate.mtime_ns = stat.st_mtime_ns
                files.append(candidate)
            except (OSError, ValueError) as exc:
                errors.append(f"Could not inspect {path}: {exc}")
    files.sort(key=lambda item: item.relative_path.casefold())
    blocking_warnings = any(("symlink" in warning or "non-regular file" in warning) for warning in warnings)
    return InventoryResult(files, errors, warnings, not errors and not blocking_warnings)
