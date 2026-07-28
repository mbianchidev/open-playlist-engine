"""Guard against divergent Alembic migration history (multiple heads).

The merge of issue #24 into main's migration history added
``0009_public_source_imports`` after ``0008_local_playlist_imports``; this
test ensures the migration graph still has exactly one head so ``alembic
upgrade head`` remains unambiguous.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_migrations_have_a_single_head() -> None:
    script = _script_directory()
    heads = script.get_heads()

    assert heads == ["0009_public_source_imports"]


def test_public_source_imports_migration_chains_after_local_playlist_imports() -> None:
    script = _script_directory()
    revision = script.get_revision("0009_public_source_imports")

    assert revision is not None
    assert revision.down_revision == "0008_local_playlist_imports"
