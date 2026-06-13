"""Built-in provider adapters.

Importing this package registers the bundled adapters. Third-party adapters can
register via ``importlib.metadata`` entry points (group ``ope.providers``).
"""

from __future__ import annotations

from app.providers import spotify, ytmusic  # noqa: F401  (import for side effects)
