"""Match service — owns the evidence graph, scoring and review policy.

Adapters only *search*; this service decides which candidate (if any) is good
enough, with what confidence, and whether a human must review it. It also reads
and writes the evidence graph (an injected repository), so a bad match from one
context never silently becomes global truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from app.core.adapter import ProviderAdapter, ProviderCredential, TrackCandidate
from app.core.models import Track

_FEAT = re.compile(r"\s*[\(\[]?\s*(feat|ft|with)\.?\s.*$", re.IGNORECASE)
_NOISE = re.compile(r"[^a-z0-9]+")


def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = _FEAT.sub("", s.lower())
    return _NOISE.sub(" ", s).strip()


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class MatchResult:
    candidate: TrackCandidate | None
    confidence: float
    source: str  # "isrc_exact" | "fuzzy" | "graph_cache" | "none"
    needs_review: bool


class EvidenceGraph(Protocol):
    """Persistence seam for the cross-provider identity graph (db-backed)."""

    async def lookup(self, *, isrc: str | None, provider: str) -> TrackCandidate | None: ...

    async def record(
        self,
        *,
        track: Track,
        provider: str,
        candidate: TrackCandidate,
        confidence: float,
        source: str,
    ) -> None: ...


def score(track: Track, cand: TrackCandidate) -> tuple[float, str]:
    """Return (confidence, source) for a candidate against the source track."""
    if track.isrc and cand.isrc and track.isrc == cand.isrc:
        return 1.0, "isrc_exact"

    title = _ratio(_norm(track.title), _norm(cand.title))
    artist = _ratio(_norm(track.artist), _norm(cand.artist))
    album = _ratio(_norm(track.album), _norm(cand.album)) if track.album and cand.album else 0.0

    dur = 0.0
    if track.duration_s and cand.duration_s:
        delta = abs(track.duration_s - cand.duration_s)
        dur = 1.0 if delta <= 3 else max(0.0, 1.0 - delta / 30.0)

    confidence = 0.45 * title + 0.35 * artist + 0.1 * album + 0.1 * dur
    return round(confidence, 4), "fuzzy"


class MatchService:
    def __init__(self, graph: EvidenceGraph | None, review_threshold: float = 0.8) -> None:
        self._graph = graph
        self._threshold = review_threshold

    async def resolve(
        self, track: Track, target: ProviderAdapter, cred: ProviderCredential
    ) -> MatchResult:
        provider = target.info.name

        # 1. Cheap path: a confirmed/cached edge in the evidence graph.
        if self._graph is not None and track.isrc:
            cached = await self._graph.lookup(isrc=track.isrc, provider=provider)
            if cached is not None:
                return MatchResult(cached, 1.0, "graph_cache", needs_review=False)

        # 2. Ask the adapter to search, then score locally.
        candidates = await target.search_tracks(cred, track)
        if not candidates:
            return MatchResult(None, 0.0, "none", needs_review=True)

        best, best_conf, best_src = None, -1.0, "none"
        for cand in candidates:
            conf, src = score(track, cand)
            if conf > best_conf:
                best, best_conf, best_src = cand, conf, src

        needs_review = best_conf < self._threshold
        if self._graph is not None and best is not None and not needs_review:
            await self._graph.record(
                track=track,
                provider=provider,
                candidate=best,
                confidence=best_conf,
                source=best_src,
            )
        return MatchResult(best, best_conf, best_src, needs_review)
