"""Local playlist-file, public URL, and pasted-text playlist imports.

All three source kinds share one ephemeral, lease-backed
``LocalPlaylistImport`` table and lifecycle (see ``app.imports.service``).
The provider constants below are the synthetic ``source_provider`` values
used on ``MigrationJob``/``LocalPlaylistImport`` rows for imports that do not
come from a connected provider account.
"""

from app.imports.models import ImportFormat, ImportLimits, ImportParseResult

LOCAL_FILE_PROVIDER = "local_file"
PUBLIC_URL_PROVIDER = "public_url"
PASTED_TEXT_PROVIDER = "pasted_text"

# Synthetic providers backed by an import record (LocalPlaylistImport) rather
# than a connected provider account/credential.
IMPORT_RECORD_PROVIDERS = frozenset(
    {LOCAL_FILE_PROVIDER, PUBLIC_URL_PROVIDER, PASTED_TEXT_PROVIDER}
)


def is_import_record_provider(provider: str) -> bool:
    return provider in IMPORT_RECORD_PROVIDERS


__all__ = [
    "IMPORT_RECORD_PROVIDERS",
    "LOCAL_FILE_PROVIDER",
    "PASTED_TEXT_PROVIDER",
    "PUBLIC_URL_PROVIDER",
    "ImportFormat",
    "ImportLimits",
    "ImportParseResult",
    "is_import_record_provider",
]
