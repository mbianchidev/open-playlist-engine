"""Provider registry.

Adapters self-register with ``@register``. In a hosted deployment this list
should be restricted to allow-listed/signed plugins (see docs/DESIGN.md trust
boundary); self-host trusts locally installed modules.
"""

from __future__ import annotations

from app.core.adapter import ProviderAdapter, ProviderInfo

_REGISTRY: dict[str, ProviderAdapter] = {}


def register(adapter: ProviderAdapter) -> ProviderAdapter:
    name = adapter.info.name
    if name in _REGISTRY:
        raise ValueError(f"provider '{name}' already registered")
    _REGISTRY[name] = adapter
    return adapter


def get(name: str) -> ProviderAdapter:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown provider '{name}'") from exc


def all_adapters() -> list[ProviderAdapter]:
    return list(_REGISTRY.values())


def all_info() -> list[ProviderInfo]:
    return [a.info for a in _REGISTRY.values()]
