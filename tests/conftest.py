from __future__ import annotations

import pytest

from academic_library.config import create_config, load_config
from academic_library.database import initialize_database
from academic_library.taxonomy import Taxonomy


@pytest.fixture
def env(tmp_path):
    library = tmp_path / "Academic_Library"
    source = tmp_path / "new material — Ω"
    library.mkdir()
    source.mkdir()
    config_path = tmp_path / "config" / "config.toml"
    database_path = tmp_path / "state" / "catalog.sqlite3"
    taxonomy_path = tmp_path / "config" / "taxonomy.toml"
    log_path = tmp_path / "state" / "logs"
    staging_path = tmp_path / "state" / "staging"
    create_config(config_path, library, database_path=database_path, taxonomy_path=taxonomy_path,
                  log_directory=log_path, staging_directory=staging_path)
    config = load_config(config_path)
    taxonomy = Taxonomy.load(config.taxonomy_path)
    initialize_database(config.database_path)
    return {"tmp": tmp_path, "library": library, "source": source, "config_path": config_path,
            "config": config, "taxonomy": taxonomy, "db": database_path, "logs": log_path}
