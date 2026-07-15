import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Download,
  ExternalLink,
  FileJson,
  FileSpreadsheet,
  Filter,
} from "lucide-react";
import {
  ApiError,
  getMigrationItemPage,
  migrationReportUrl,
} from "../api/client";
import type {
  AccountHistoryView,
  JobItemView,
  MigrationItemFilters,
  MigrationItemPage,
  MigrationStatsView,
} from "../api/types";
import {
  providerLabel,
  providerTrackUrl,
} from "../utils/providers";

const PAGE_SIZE = 50;

interface Props {
  stats: MigrationStatsView;
}

interface FilterDraft {
  sourcePlaylistId: string;
  status: string;
  minConfidence: string;
  maxConfidence: string;
  title: string;
  artist: string;
  reason: string;
}

const EMPTY_FILTERS: FilterDraft = {
  sourcePlaylistId: "",
  status: "",
  minConfidence: "",
  maxConfidence: "",
  title: "",
  artist: "",
  reason: "",
};

export default function MigrationHistoryDetail({ stats }: Props) {
  const [draft, setDraft] = useState<FilterDraft>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<FilterDraft>(EMPTY_FILTERS);
  const [page, setPage] = useState<MigrationItemPage>({
    items: [],
    total: 0,
    limit: PAGE_SIZE,
    offset: 0,
  });
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const apiFilters = useMemo(() => itemFilters(applied), [applied]);
  const pageNumber = Math.floor(offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / PAGE_SIZE));

  useEffect(() => {
    setDraft(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setOffset(0);
    setPage({ items: [], total: 0, limit: PAGE_SIZE, offset: 0 });
    setError(null);
  }, [stats.id]);

  useEffect(() => {
    if (!stats.detail_available) {
      setPage({ items: [], total: 0, limit: PAGE_SIZE, offset: 0 });
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    getMigrationItemPage(stats.id, apiFilters, { limit: PAGE_SIZE, offset })
      .then((nextPage) => {
        if (!cancelled) setPage(nextPage);
      })
      .catch((nextError: unknown) => {
        if (cancelled) return;
        if (nextError instanceof ApiError && nextError.status === 410) {
          setError("Item-level migration detail has expired.");
        } else {
          setError(errorMessage(nextError));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [apiFilters, offset, stats.detail_available, stats.id]);

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setApplied({ ...draft });
    setOffset(0);
  }

  function clearFilters() {
    setDraft(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setOffset(0);
  }

  return (
    <div className="history-detail">
      <div className="history-route-strip">
        <RouteEndpoint
          label="Source"
          provider={stats.source_provider}
          account={stats.source_account}
        />
        <div className="history-route-stamp">
          <span className={`badge status-${stats.outcome ?? stats.status}`}>
            {statusLabel(stats.outcome ?? stats.status)}
          </span>
          <strong>{formatDuration(stats.duration_s)}</strong>
          <span className="muted">Migration {stats.id.slice(0, 8)}</span>
        </div>
        <RouteEndpoint
          label="Target"
          provider={stats.target_provider}
          account={stats.target_account}
        />
      </div>

      <div className="history-timeline" aria-label="Migration lifecycle">
        <TimelineMoment label="Created" value={stats.created_at} />
        <TimelineMoment label="Started" value={stats.started_at} />
        <TimelineMoment label="Finished" value={stats.completed_at} />
        <div className="history-timeline-retention">
          <Clock3 aria-hidden="true" />
          <span>
            <strong>Item detail</strong>
            <span className="muted">{retentionLabel(stats)}</span>
          </span>
        </div>
      </div>

      {stats.warnings.length > 0 ? (
        <div className="history-callout history-warning">
          <AlertTriangle aria-hidden="true" />
          <div>
            <strong>Warnings acknowledged when this migration started</strong>
            <ul>
              {stats.warnings.map((warning) => (
                <li key={`${warning.code}-${warning.message}`}>{warning.message}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
      {stats.error ? (
        <div className="history-callout history-error">
          <AlertTriangle aria-hidden="true" />
          <div>
            <strong>Migration error</strong>
            <p>{stats.error}</p>
          </div>
        </div>
      ) : null}

      <div className="history-report-bar">
        <div>
          <strong>Download migration report</strong>
          <p className="muted">
            Export every ledger row or only tracks that need attention.
          </p>
        </div>
        <div className="history-report-actions">
          <ReportLink stats={stats} format="csv" scope="all" />
          <ReportLink stats={stats} format="csv" scope="problems" />
          <ReportLink stats={stats} format="json" scope="all" />
          <ReportLink stats={stats} format="json" scope="problems" />
        </div>
      </div>

      {!stats.detail_available ? (
        <p className="empty-guidance">
          Item rows and downloads are no longer available. The retained summary above remains
          available for this migration.
        </p>
      ) : (
        <>
          <form className="history-filters" onSubmit={applyFilters}>
            <div className="history-filter-heading">
              <span>
                <Filter aria-hidden="true" />
                <strong>Filter ledger items</strong>
              </span>
              <button className="secondary compact" type="button" onClick={clearFilters}>
                Clear
              </button>
            </div>
            <div className="history-filter-grid">
              <label>
                Playlist
                <select
                  value={draft.sourcePlaylistId}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      sourcePlaylistId: event.target.value,
                    }))
                  }
                >
                  <option value="">All playlists</option>
                  {stats.playlists.map((playlist) => (
                    <option key={playlist.source_playlist_id} value={playlist.source_playlist_id}>
                      {playlist.source_playlist_name ?? playlist.source_playlist_id}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Status
                <select
                  value={draft.status}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, status: event.target.value }))
                  }
                >
                  <option value="">Any status</option>
                  {["pending", "matched", "needs_review", "written", "skipped", "failed"].map(
                    (status) => (
                      <option key={status} value={status}>
                        {statusLabel(status)}
                      </option>
                    ),
                  )}
                </select>
              </label>
              <label>
                Minimum confidence
                <input
                  type="number"
                  min="0"
                  max="100"
                  step="1"
                  inputMode="numeric"
                  placeholder="0%"
                  value={draft.minConfidence}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      minConfidence: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Maximum confidence
                <input
                  type="number"
                  min="0"
                  max="100"
                  step="1"
                  inputMode="numeric"
                  placeholder="100%"
                  value={draft.maxConfidence}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      maxConfidence: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Title contains
                <input
                  value={draft.title}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, title: event.target.value }))
                  }
                />
              </label>
              <label>
                Artist contains
                <input
                  value={draft.artist}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, artist: event.target.value }))
                  }
                />
              </label>
              <label className="history-reason-filter">
                Reason contains
                <input
                  value={draft.reason}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, reason: event.target.value }))
                  }
                />
              </label>
            </div>
            <button type="submit">
              <Filter aria-hidden="true" />
              Apply filters
            </button>
          </form>

          <div className="history-ledger-heading">
            <div>
              <strong>Item ledger</strong>
              <p className="muted">
                {page.total} matching {page.total === 1 ? "track" : "tracks"}
              </p>
            </div>
            <span className="muted">
              Page {pageNumber} of {pageCount}
            </span>
          </div>
          {loading ? <p className="muted">Loading migration items...</p> : null}
          {error ? <p className="warn">{error}</p> : null}
          {!loading && !error && page.items.length === 0 ? (
            <p className="empty-guidance">No migration items match these filters.</p>
          ) : null}
          {page.items.length > 0 ? (
            <div className="history-table-wrap">
              <table className="history-table">
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Track</th>
                    <th>Playlist</th>
                    <th>Confidence</th>
                    <th>Result</th>
                    <th>Target</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((item) => (
                    <HistoryRow
                      key={item.id}
                      item={item}
                      sourceProvider={stats.source_provider}
                      targetProvider={stats.target_provider}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <div className="history-pagination">
            <button
              className="button-link secondary compact"
              disabled={offset === 0 || loading}
              onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
            >
              <ChevronLeft aria-hidden="true" />
              Previous
            </button>
            <button
              className="secondary compact"
              disabled={offset + PAGE_SIZE >= page.total || loading}
              onClick={() => setOffset((current) => current + PAGE_SIZE)}
            >
              Next
              <ChevronRight aria-hidden="true" />
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function RouteEndpoint({
  label,
  provider,
  account,
}: {
  label: string;
  provider: string;
  account: AccountHistoryView | null;
}) {
  return (
    <div className="history-route-endpoint">
      <span className="muted">{label}</span>
      <strong>{providerLabel(provider)}</strong>
      <span>{accountLabel(account)}</span>
      {account ? <code>{shortIdentifier(account.id)}</code> : null}
    </div>
  );
}

function TimelineMoment({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <span className="muted">{label}</span>
      <strong>{formatDateTime(value) ?? "Not recorded"}</strong>
    </div>
  );
}

function ReportLink({
  stats,
  format,
  scope,
}: {
  stats: MigrationStatsView;
  format: "csv" | "json";
  scope: "all" | "problems";
}) {
  const label = `${scope === "all" ? "All items" : "Problem items"} ${format.toUpperCase()}`;
  return (
    <a
      className="secondary compact"
      href={migrationReportUrl(stats.id, format, scope)}
      download
      aria-disabled={!stats.detail_available}
      onClick={(event) => {
        if (!stats.detail_available) event.preventDefault();
      }}
    >
      {format === "csv" ? (
        <FileSpreadsheet aria-hidden="true" />
      ) : (
        <FileJson aria-hidden="true" />
      )}
      {label}
      <Download aria-hidden="true" />
    </a>
  );
}

function HistoryRow({
  item,
  sourceProvider,
  targetProvider,
}: {
  item: JobItemView;
  sourceProvider: string;
  targetProvider: string;
}) {
  const targetUrl = item.target_uri
    ? providerTrackUrl(targetProvider, item.target_uri)
    : null;
  const sourceUri = sourceProviderUri(item, sourceProvider);
  const sourceUrl = sourceUri ? providerTrackUrl(sourceProvider, sourceUri) : null;
  return (
    <tr>
      <td>
        <span className={`status status-${item.status}`}>{statusLabel(item.status)}</span>
      </td>
      <td>
        <strong>{item.title}</strong>
        <span>{item.artist}</span>
        {item.album ? <span className="muted">{item.album}</span> : null}
        {sourceUrl ? (
          <a href={sourceUrl} target="_blank" rel="noreferrer">
            Source
            <ExternalLink aria-hidden="true" />
          </a>
        ) : null}
      </td>
      <td>{item.source_playlist_name ?? item.source_playlist_id}</td>
      <td>{formatConfidence(item.confidence)}</td>
      <td>
        {item.reason ? <span>{item.reason}</span> : <span className="muted">No issue recorded</span>}
        {item.review_action ? (
          <span className="history-review-note">
            {statusLabel(item.review_action)} from{" "}
            {statusLabel(item.review_original_status ?? "review")}
            {item.review_original_reason ? `: ${item.review_original_reason}` : ""}
            {item.reviewed_at ? ` · ${formatDateTime(item.reviewed_at)}` : ""}
          </span>
        ) : null}
      </td>
      <td>
        {item.target_uri ? (
          targetUrl ? (
            <a href={targetUrl} target="_blank" rel="noreferrer">
              Open target
              <ExternalLink aria-hidden="true" />
            </a>
          ) : (
            <code>{item.target_uri}</code>
          )
        ) : (
          <span className="muted">No target</span>
        )}
      </td>
      <td>{formatDateTime(item.updated_at) ?? "Not recorded"}</td>
    </tr>
  );
}

function itemFilters(draft: FilterDraft): MigrationItemFilters {
  return {
    sourcePlaylistId: draft.sourcePlaylistId || null,
    statuses: draft.status ? [draft.status] : [],
    minConfidence: confidenceValue(draft.minConfidence),
    maxConfidence: confidenceValue(draft.maxConfidence),
    title: draft.title.trim() || null,
    artist: draft.artist.trim() || null,
    reason: draft.reason.trim() || null,
  };
}

function confidenceValue(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return Math.min(1, Math.max(0, parsed / 100));
}

function sourceProviderUri(item: JobItemView, provider: string): string | null {
  const providerUris = item.source_metadata.provider_uris;
  if (!providerUris || typeof providerUris !== "object") return null;
  const value = (providerUris as Record<string, unknown>)[provider];
  return typeof value === "string" ? value : null;
}

function accountLabel(account: AccountHistoryView | null): string {
  if (!account) return "Account unavailable";
  if (!account.connected) return "Disconnected account";
  return account.display_name ?? "Connected account";
}

function retentionLabel(stats: MigrationStatsView): string {
  if (stats.retention_days === 0) return "Retained indefinitely";
  if (!stats.detail_available) {
    return stats.detail_purged_at
      ? `Purged ${formatDateTime(stats.detail_purged_at) ?? ""}`.trim()
      : `Expired ${formatDateTime(stats.detail_expires_at) ?? ""}`.trim();
  }
  return `Available until ${formatDateTime(stats.detail_expires_at) ?? "retention cleanup"}`;
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "Duration pending";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function formatConfidence(confidence: number | null): string {
  return confidence === null ? "—" : `${Math.round(confidence * 100)}%`;
}

function formatDateTime(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function shortIdentifier(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
