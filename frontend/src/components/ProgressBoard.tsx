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
}

// Phase 5 — live progress. Reconnect/replay via Last-Event-ID is handled by the
// browser's EventSource plus the backend's persisted job_item cursor.
export default function ProgressBoard({ jobId, className, onMigrationChanged }: Props) {
  const [job, setJob] = useState<JobView | null>(null);
  const [items, setItems] = useState<JobItemView[]>([]);
  const [reviewInputs, setReviewInputs] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const notifiedDoneForJob = useRef<string | null>(null);
  const targetProviderLabel = job ? providerLabel(job.target_provider) : "target provider";
  const targetPlaylists =
    job?.status === "done" ? getTargetPlaylists(items, job.target_provider) : [];
  const doubtfulItems = items.filter((item) => item.status === "needs_review");
  const approvableItems = doubtfulItems.filter((item) => item.target_uri);

  useEffect(() => {
    getMigrationItems(jobId).then(setItems).catch(() => setItems([]));
    const dispose = subscribeProgress(jobId, (e) => {
      const payload = JSON.parse(e.data) as ProgressEvent;
      if (payload.job) {
        setJob(payload.job);
        if (payload.job.status === "done" && notifiedDoneForJob.current !== jobId) {
          notifiedDoneForJob.current = jobId;
          void onMigrationChanged?.();
        }
      }
      if (payload.items) setItems(payload.items);
    });
    return dispose;
  }, [jobId, onMigrationChanged]);

  return (
    <section className={["card", className].filter(Boolean).join(" ")}>
      <h2>Progress · {jobId}</h2>
      <p className="muted">
        {job ? `${job.status}: ${job.done}/${job.total} done, ${job.failed} failed` : "waiting…"}
      </p>
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
      {error ? <p className="warn">{error}</p> : null}
      {doubtfulItems.length > 0 ? (
        <div className="review-toolbar">
          <button
            className="secondary compact"
            disabled={approvableItems.length === 0}
            onClick={() => reviewMany(approvableItems, "approve")}
          >
            Approve all suggested
          </button>
          <button
            className="secondary compact"
            onClick={() => reviewMany(doubtfulItems, "skip")}
          >
            Deny all doubtful
          </button>
        </div>
      ) : null}
      <div className="progress-list">
        {items.length === 0 ? (
          <p className="muted">
            {job?.status === "failed" ? "No tracks were created." : "Waiting for tracks…"}
          </p>
        ) : (
          items.map((item) => (
            <div key={item.id} className="progress-row">
              <span className={`status status-${item.status}`}>{item.status}</span>
              <span>{formatTrack(item)}</span>
              {item.reason ? <span className="muted">{item.reason}</span> : null}
              {item.status === "needs_review" || item.status === "failed" ? (
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
          ))
        )}
      </div>
    </section>
  );

  async function review(item: JobItemView, action: "approve" | "skip") {
    setError(null);
    try {
      const updated = await reviewMigrationItem(jobId, item.id, {
        action,
        target_uri: action === "approve" ? reviewInputs[item.id] ?? item.target_uri : null,
      });
      setItems((prev) => prev.map((row) => (row.id === updated.id ? updated : row)));
      void onMigrationChanged?.();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
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
      const byId = new Map(updated.map((item) => [item.id, item]));
      setItems((prev) => prev.map((row) => byId.get(row.id) ?? row));
      void onMigrationChanged?.();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
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
