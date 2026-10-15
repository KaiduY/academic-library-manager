"""Configuration discovery and safe bootstrap."""
from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(os.environ.get("ACADEMIC_LIBRARY_CONFIG", "~/.config/academic-library/config.toml")).expanduser()


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    library_root: Path
    database_path: Path
    taxonomy_path: Path
    staging_directory: Path
    log_directory: Path
    ignore_directories: tuple[str, ...] = (".git", "__pycache__", ".venv", "node_modules")
    pdfinfo_command: str = ""


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().absolute()


def load_config(path: Path | str | None = None) -> AppConfig:
    cfg_path = Path(path or DEFAULT_CONFIG).expanduser().absolute()
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Configuration not found: {cfg_path}. Run 'academic-library init --library-root PATH' first."
        )
    with cfg_path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)
    paths = raw.get("paths", {})
    scan = raw.get("scan", {})
    metadata = raw.get("metadata", {})
    required = ("library_root", "database_path", "taxonomy_path", "staging_directory", "log_directory")
    missing = [key for key in required if not paths.get(key)]
    if missing:
        raise ValueError(f"Configuration {cfg_path} is missing paths: {', '.join(missing)}")
    return AppConfig(
        config_path=cfg_path,
        library_root=_expand(paths["library_root"]),
        database_path=_expand(paths["database_path"]),
        taxonomy_path=_expand(paths["taxonomy_path"]),
        staging_directory=_expand(paths["staging_directory"]),
        log_directory=_expand(paths["log_directory"]),
        ignore_directories=tuple(scan.get("ignore_directories", [".git", "__pycache__", ".venv", "node_modules"])),
        pdfinfo_command=str(metadata.get("pdfinfo_command", "")).strip(),
    )


def create_config(
    config_path: Path | str,
    library_root: Path | str,
    database_path: Path | str | None = None,
    taxonomy_path: Path | str | None = None,
    staging_directory: Path | str | None = None,
    log_directory: Path | str | None = None,
    overwrite: bool = False,
) -> Path:
    cfg = Path(config_path).expanduser().absolute()
    if cfg.exists() and not overwrite:
        raise FileExistsError(f"Configuration already exists: {cfg}; it was not overwritten")
    root = Path(library_root).expanduser().absolute()
    if not root.is_dir():
        raise NotADirectoryError(f"Library root must already exist and be a directory: {root}")
    data_home = Path("~/.local/share/academic-library").expanduser()
    state_home = Path("~/.local/state/academic-library").expanduser()
    db = Path(database_path).expanduser().absolute() if database_path else data_home / "catalog.sqlite3"
    taxonomy = Path(taxonomy_path).expanduser().absolute() if taxonomy_path else cfg.parent / "taxonomy.toml"
    staging = Path(staging_directory).expanduser().absolute() if staging_directory else data_home / "staging"
    logs = Path(log_directory).expanduser().absolute() if log_directory else state_home / "logs"
    taxonomy.parent.mkdir(parents=True, exist_ok=True)
    if not taxonomy.exists():
        bundled = Path(__file__).parent / "data" / "taxonomy.toml"
        if bundled.is_file():
            shutil.copyfile(bundled, taxonomy)
        else:
            raise FileNotFoundError(f"Bundled default taxonomy is missing: {bundled}")
    cfg.parent.mkdir(parents=True, exist_ok=True)

    def quote(value: Path) -> str:
        # TOML basic-string quoting compatible with paths containing backslashes or quotes.
        import json
        return json.dumps(str(value), ensure_ascii=False)

    content = (
        "# Academic Library Manager configuration. Paths are independent of the source machine.\n"
        "[paths]\n"
        f"library_root = {quote(root)}\n"
        f"database_path = {quote(db)}\n"
        f"taxonomy_path = {quote(taxonomy)}\n"
        f"staging_directory = {quote(staging)}\n"
        f"log_directory = {quote(logs)}\n\n"
        "[scan]\n"
        'ignore_directories = [".git", "__pycache__", ".venv", "node_modules"]\n\n'
        "[metadata]\n"
        'pdfinfo_command = ""\n'
    )
    with cfg.open("x" if not overwrite else "w", encoding="utf-8") as handle:
        handle.write(content)
    return cfg
