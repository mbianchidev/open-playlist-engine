import { useEffect, useRef, useState } from "react";
import {
  getMigrationItems,
  reviewMigrationItem,
  reviewMigrationItems,
  subscribeProgress,
} from "../api/client";
import type { JobItemView, JobView, ProgressEvent } from "../api/types";

interface Props {
  jobId: string;
  className?: string;
  onMigrationChanged?: () => void | Promise<void>;
  onReconnectProvider?: (provider: string) => void | Promise<void>;
}

// Phase 5 — live progress. Reconnect/replay via Last-Event-ID is handled by the
// browser's EventSource plus the backend's persisted job_item cursor.
export default function ProgressBoard({
  jobId,
  className,
  onMigrationChanged,
  onReconnectProvider,
}: Props) {
  const [job, setJob] = useState<JobView | null>(null);
  const [items, setItems] = useState<JobItemView[]>([]);
  const [reviewInputs, setReviewInputs] = useState<Record<string, string>>({});
  const [approveThresholdPct, setApproveThresholdPct] = useState(70);
  const [skipThresholdPct, setSkipThresholdPct] = useState(50);
  const [collapsed, setCollapsed] = useState(false);
  const [reviewCollapsed, setReviewCollapsed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const notifiedDoneForJob = useRef<string | null>(null);
  const locallyReviewedItems = useRef<Map<string, JobItemView>>(new Map());
  const targetProviderLabel = job ? providerLabel(job.target_provider) : "target provider";
  const targetPlaylists =
    job?.status === "done" ? getTargetPlaylists(items, job.target_provider) : [];
  const counts = countStatuses(items);
  const total = job?.total ?? items.length;
  const migratedCount = counts.written ?? 0;
  const reviewCount = counts.needs_review ?? 0;
  const processedCount =
    migratedCount + (counts.skipped ?? 0) + reviewCount + (counts.failed ?? 0);
  const waitingCount = Math.max(total - processedCount, 0);
  const progressPct = total > 0 ? Math.min(100, Math.round((migratedCount / total) * 100)) : 0;
  const playlistLabel = summarizePlaylists(items);
  const uncertainItems = items.filter((item) => item.status === "needs_review");
  const reviewableItems = items.filter(isReviewableItem).sort(compareReviewItems);
  const migrationItems = items.filter((item) => !isReviewableItem(item));
  const thresholdApprovableItems = uncertainItems.filter(
    (item) => item.target_uri && confidenceAtOrAboveThreshold(item.confidence, approveThresholdPct),
  );
  const thresholdSkippableItems = uncertainItems.filter((item) =>
    confidenceAtOrBelowThreshold(item.confidence, skipThresholdPct),
  );
  const reconnectProvider =
    job?.error && shouldPromptReconnect(job.error) ? providerForReconnect(job, job.error) : null;

  useEffect(() => {
    locallyReviewedItems.current.clear();
    setReviewCollapsed(false);
    getMigrationItems(jobId).then(updateItemsFromServer).catch(() => setItems([]));
    const dispose = subscribeProgress(jobId, (e) => {
      const payload = JSON.parse(e.data) as ProgressEvent;
      if (payload.job) {
        setJob(payload.job);
        if (payload.job.status === "done" && notifiedDoneForJob.current !== jobId) {
          notifiedDoneForJob.current = jobId;
          void onMigrationChanged?.();
        }
      }
      if (payload.items) updateItemsFromServer(payload.items);
    });
    return dispose;
  }, [jobId, onMigrationChanged]);

  return (
    <section className={["card", className].filter(Boolean).join(" ")}>
      <button
        className="progress-collapse-toggle"
        type="button"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed((value) => !value)}
      >
        <span>
          <strong>{playlistLabel}</strong>
          <span className="muted">Migration {shortJobId(jobId)}</span>
        </span>
        <span className="muted">{collapsed ? "Show" : "Hide"}</span>
      </button>
      <div
        className="migration-progress-meter"
        role="progressbar"
        aria-label="Migrated songs"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progressPct}
      >
        <span style={{ transform: `scaleX(${progressPct / 100})` }} />
      </div>
      <p className="progress-summary">
        {migratedCount}/{total || "?"} migrated
        {reviewCount ? ` · ${reviewCount} need review` : ""}
        {waitingCount ? ` · ${waitingCount} waiting` : ""}
        {job?.failed ? ` · ${job.failed} failed` : ""}
      </p>
      {!collapsed ? (
        <div className="progress-panel-body">
          {job?.status === "done" ? (
            <div className="notice migration-success">
              <strong>Migration succeeded.</strong>
              {targetPlaylists.length > 0 ? (
                <div className="target-playlists">
                  {targetPlaylists.map((playlist) =>
                    playlist.url ? (
                      <a
                        key={playlist.id}
                        href={playlist.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Open {playlist.label} in {targetProviderLabel}
                      </a>
                    ) : (
                      <span key={playlist.id}>
                        Target playlist: {playlist.label} ({playlist.id})
                      </span>
                    ),
                  )}
                </div>
              ) : null}
            </div>
          ) : null}
          {job?.error ? <p className="warn">{job.error}</p> : null}
          {reconnectProvider ? (
            <div className="auth-reconnect-callout">
              <p>Reconnect {providerLabel(reconnectProvider)}, then start the migration again.</p>
              <button
                className="secondary compact"
                disabled={!onReconnectProvider}
                onClick={() => void onReconnectProvider?.(reconnectProvider)}
              >
                Reconnect {providerLabel(reconnectProvider)}
              </button>
            </div>
          ) : null}
          {error ? <p className="warn">{error}</p> : null}
          {reviewableItems.length > 0 ? (
            <div className="review-bin">
              <button
                className="review-bin-toggle"
                type="button"
                aria-expanded={!reviewCollapsed}
                onClick={() => setReviewCollapsed((value) => !value)}
              >
                <span>
                  <strong>Review uncertain songs</strong>
                  <span className="muted">
                    {uncertainItems.length} uncertain
                    {counts.failed ? ` · ${counts.failed} failed` : ""}
                  </span>
                </span>
                <span className="muted">{reviewCollapsed ? "Show" : "Hide"}</span>
              </button>
              {!reviewCollapsed ? (
                <div className="review-bin-body">
                  {uncertainItems.length > 0 ? (
                    <div className="review-toolbar">
                      <div className="bulk-review-action">
                        <label htmlFor="approveThreshold">Approve above</label>
                        <input
                          id="approveThreshold"
                          type="number"
                          min="0"
                          max="100"
                          step="0.01"
                          value={approveThresholdPct}
                          onChange={(e) =>
                            setApproveThresholdPct(normalizeThreshold(e.target.value, 70))
                          }
                        />
                        <span>%</span>
                        <button
                          className="secondary compact"
                          disabled={thresholdApprovableItems.length === 0}
                          onClick={() => reviewMany(thresholdApprovableItems, "approve")}
                        >
                          Approve above {approveThresholdPct}% ({thresholdApprovableItems.length})
                        </button>
                      </div>
                      <div className="bulk-review-action">
                        <label htmlFor="skipThreshold">Skip below</label>
                        <input
                          id="skipThreshold"
                          type="number"
                          min="0"
                          max="100"
                          step="0.01"
                          value={skipThresholdPct}
                          onChange={(e) =>
                            setSkipThresholdPct(normalizeThreshold(e.target.value, 50))
                          }
                        />
                        <span>%</span>
                        <button
                          className="secondary compact"
                          disabled={thresholdSkippableItems.length === 0}
                          onClick={() => reviewMany(thresholdSkippableItems, "skip")}
                        >
                          Skip below {skipThresholdPct}% ({thresholdSkippableItems.length})
                        </button>
                      </div>
                    </div>
                  ) : null}
                  <div className="progress-list review-list">
                    {reviewableItems.map(renderProgressRow)}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="progress-list">
            {items.length === 0 ? (
              <p className="muted">
                {job?.status === "failed" ? "No tracks were created." : "Waiting for tracks…"}
              </p>
            ) : (
              migrationItems.map(renderProgressRow)
            )}
          </div>
        </div>
      ) : null}
    </section>
  );

  async function review(item: JobItemView, action: "approve" | "skip") {
    setError(null);
    try {
      const updated = await reviewMigrationItem(jobId, item.id, {
        action,
        target_uri: action === "approve" ? reviewInputs[item.id] ?? item.target_uri : null,
      });
      applyReviewedItems([updated]);
      const refreshError = await refreshItemsAfterReview();
      if (refreshError) setError(`Review saved, but refresh failed: ${refreshError}`);
      void onMigrationChanged?.();
    } catch (e: unknown) {
      const refreshError = await refreshItemsAfterReview();
      setError(reviewErrorMessage(e, refreshError));
    }
  }

  async function reviewMany(reviewItems: JobItemView[], action: "approve" | "skip") {
    if (reviewItems.length === 0) return;
    setError(null);
    try {
      const updated = await reviewMigrationItems(jobId, {
        action,
        item_ids: reviewItems.map((item) => item.id),
      });
      applyReviewedItems(updated);
      const refreshError = await refreshItemsAfterReview();
      if (refreshError) setError(`Review saved, but refresh failed: ${refreshError}`);
      void onMigrationChanged?.();
    } catch (e: unknown) {
      const refreshError = await refreshItemsAfterReview();
      setError(reviewErrorMessage(e, refreshError));
    }
  }

  function renderProgressRow(item: JobItemView) {
    return (
      <div key={item.id} className={`progress-row ${isReviewableItem(item) ? "review-row" : ""}`}>
        <span className={`status status-${item.status}`}>{statusLabel(item.status)}</span>
        <span className="progress-track-title">
          {formatTrack(item)}
          {item.status === "needs_review" ? (
            <span className="badge inline confidence-badge">{formatConfidence(item.confidence)}</span>
          ) : null}
        </span>
        {item.reason ? <span className="muted progress-row-reason">{item.reason}</span> : null}
        {isReviewableItem(item) ? (
          <div className="review-controls">
            <input
              value={reviewInputs[item.id] ?? item.target_uri ?? ""}
              onChange={(e) =>
                setReviewInputs((prev) => ({ ...prev, [item.id]: e.target.value }))
              }
              placeholder="ytmusic:video:id or YouTube Music URL"
            />
            <button className="secondary compact" onClick={() => review(item, "approve")}>
              Approve
            </button>
            <button className="secondary compact" onClick={() => review(item, "skip")}>
              Skip
            </button>
          </div>
        ) : null}
      </div>
    );
  }

  function applyReviewedItems(updated: JobItemView[]) {
    const byId = new Map(updated.map((item) => [item.id, item]));
    setItems((prev) => prev.map((row) => byId.get(row.id) ?? row));
    for (const item of updated) {
      if (!isReviewableItem(item)) {
        locallyReviewedItems.current.set(item.id, item);
      }
    }
    setReviewInputs((prev) => {
      const next = { ...prev };
      for (const item of updated) {
        if (!isReviewableItem(item)) delete next[item.id];
      }
      return next;
    });
  }

  function updateItemsFromServer(nextItems: JobItemView[]) {
    setItems(mergeLocallyReviewedItems(nextItems));
  }

  function mergeLocallyReviewedItems(nextItems: JobItemView[]): JobItemView[] {
    return nextItems.map((item) => {
      const reviewed = locallyReviewedItems.current.get(item.id);
      if (!reviewed) return item;
      if (isReviewableItem(item)) return reviewed;
      locallyReviewedItems.current.delete(item.id);
      return item;
    });
  }

  async function refreshItemsAfterReview(): Promise<string | null> {
    try {
      const latest = await getMigrationItems(jobId);
      updateItemsFromServer(latest);
      return null;
    } catch (e: unknown) {
      return e instanceof Error ? e.message : String(e);
    }
  }
}

function formatTrack(item: JobItemView): string {
  const bits = [`${item.title} — ${item.artist}`];
  if (item.album) bits.push(item.album);
  if (item.release_year) bits.push(String(item.release_year));
  if (item.explicit) bits.push("explicit");
  return bits.join(" · ");
}

function normalizeThreshold(rawValue: string, fallback: number): number {
  const value = Number(rawValue);
  if (!Number.isFinite(value)) return fallback;
  const trimmed = rawValue.trim();
  const asPercent = value > 0 && value < 1 && trimmed.startsWith("0.") ? value * 100 : value;
  return Math.min(100, Math.max(0, Math.round(asPercent)));
}

function confidenceAtOrAboveThreshold(confidence: number | null, thresholdPct: number): boolean {
  return confidence !== null && confidence * 100 >= thresholdPct;
}

function confidenceAtOrBelowThreshold(confidence: number | null, thresholdPct: number): boolean {
  return confidence !== null && confidence * 100 <= thresholdPct;
}

function isReviewableItem(item: JobItemView): boolean {
  return item.status === "needs_review" || item.status === "failed";
}

function compareReviewItems(a: JobItemView, b: JobItemView): number {
  const statusDelta = reviewStatusRank(a.status) - reviewStatusRank(b.status);
  if (statusDelta !== 0) return statusDelta;
  const confidenceDelta = (a.confidence ?? Number.POSITIVE_INFINITY) - (
    b.confidence ?? Number.POSITIVE_INFINITY
  );
  if (confidenceDelta !== 0) return confidenceDelta;
  if (a.source_playlist_id !== b.source_playlist_id) {
    return a.source_playlist_id.localeCompare(b.source_playlist_id);
  }
  return a.position - b.position;
}

function reviewStatusRank(status: string): number {
  if (status === "needs_review") return 0;
  if (status === "failed") return 1;
  return 2;
}

function formatConfidence(confidence: number | null): string {
  if (confidence === null) return "No score";
  return `${Math.round(confidence * 100)}% match`;
}

function reviewErrorMessage(error: unknown, refreshError: string | null): string {
  const message = error instanceof Error ? error.message : String(error);
  return refreshError ? `${message}. Refresh failed too: ${refreshError}` : message;
}

function countStatuses(items: JobItemView[]): Record<string, number> {
  return items.reduce<Record<string, number>>((counts, item) => {
    counts[item.status] = (counts[item.status] ?? 0) + 1;
    return counts;
  }, {});
}

function summarizePlaylists(items: JobItemView[]): string {
  const names = [
    ...new Set(
      items
        .map((item) => item.source_playlist_name)
        .filter((name): name is string => Boolean(name)),
    ),
  ];
  if (names.length === 0) return "Preparing migration";
  if (names.length === 1) return names[0] ?? "Migration";
  return `${names.slice(0, 2).join(", ")}${
    names.length > 2 ? ` + ${names.length - 2} more` : ""
  }`;
}

function shortJobId(jobId: string): string {
  return jobId.slice(0, 8);
}

function statusLabel(status: string): string {
  return status.replace("_", " ");
}

function shouldPromptReconnect(message: string): boolean {
  const normalized = message.toLowerCase();
  return normalized.includes("reconnect") || normalized.includes("signed-in session");
}

function providerForReconnect(job: JobView, message: string): string {
  const normalized = message.toLowerCase();
  if (mentionsProvider(normalized, job.source_provider)) return job.source_provider;
  if (mentionsProvider(normalized, job.target_provider)) return job.target_provider;
  return job.target_provider;
}

function mentionsProvider(message: string, provider: string): boolean {
  const normalizedProvider = provider.toLowerCase();
  return (
    message.includes(normalizedProvider) ||
    message.includes(providerLabel(provider).toLowerCase())
  );
}

interface TargetPlaylist {
  id: string;
  label: string;
  url: string | null;
}

function getTargetPlaylists(items: JobItemView[], provider: string): TargetPlaylist[] {
  const byId = new Map<string, TargetPlaylist>();
  for (const item of items) {
    if (!item.target_playlist_id || byId.has(item.target_playlist_id)) {
      continue;
    }
    byId.set(item.target_playlist_id, {
      id: item.target_playlist_id,
      label: item.source_playlist_name ?? item.target_playlist_id,
      url: targetPlaylistUrl(provider, item.target_playlist_id),
    });
  }
  return [...byId.values()];
}

function targetPlaylistUrl(provider: string, playlistId: string): string | null {
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    return `https://music.youtube.com/playlist?list=${encodeURIComponent(playlistId)}`;
  }
  if (provider === "spotify") {
    return `https://open.spotify.com/playlist/${encodeURIComponent(playlistId)}`;
  }
  return null;
}

function providerLabel(provider: string): string {
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    return "YouTube Music";
  }
  if (provider === "spotify") {
    return "Spotify";
  }
  return "target provider";
}
