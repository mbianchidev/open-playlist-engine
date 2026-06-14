import { useEffect, useState } from "react";
import { getMigrationItems, reviewMigrationItem, subscribeProgress } from "../api/client";
import type { JobItemView, JobView, ProgressEvent } from "../api/types";

interface Props {
  jobId: string;
}

// Phase 5 — live progress. Reconnect/replay via Last-Event-ID is handled by the
// browser's EventSource plus the backend's persisted job_item cursor.
export default function ProgressBoard({ jobId }: Props) {
  const [job, setJob] = useState<JobView | null>(null);
  const [items, setItems] = useState<JobItemView[]>([]);
  const [reviewInputs, setReviewInputs] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getMigrationItems(jobId).then(setItems).catch(() => setItems([]));
    const dispose = subscribeProgress(jobId, (e) => {
      const payload = JSON.parse(e.data) as ProgressEvent;
      if (payload.job) setJob(payload.job);
      if (payload.items) setItems(payload.items);
    });
    return dispose;
  }, [jobId]);

  return (
    <section className="card">
      <h2>Progress · {jobId}</h2>
      <p className="muted">
        {job ? `${job.status}: ${job.done}/${job.total} done, ${job.failed} failed` : "waiting…"}
      </p>
      {job?.error ? <p className="warn">{job.error}</p> : null}
      {error ? <p className="warn">{error}</p> : null}
      <div className="progress-list">
        {items.length === 0 ? (
          <p className="muted">Waiting for tracks…</p>
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
