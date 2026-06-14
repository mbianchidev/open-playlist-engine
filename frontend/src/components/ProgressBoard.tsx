import { useEffect, useState } from "react";
import { getMigrationItems, subscribeProgress } from "../api/client";
import type { JobItemView, JobView, ProgressEvent } from "../api/types";

interface Props {
  jobId: string;
}

// Phase 5 — live progress. Reconnect/replay via Last-Event-ID is handled by the
// browser's EventSource plus the backend's persisted job_item cursor.
export default function ProgressBoard({ jobId }: Props) {
  const [job, setJob] = useState<JobView | null>(null);
  const [items, setItems] = useState<JobItemView[]>([]);

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
      <div className="progress-list">
        {items.length === 0 ? (
          <p className="muted">Waiting for tracks…</p>
        ) : (
          items.map((item) => (
            <div key={item.id} className="progress-row">
              <span className={`status status-${item.status}`}>{item.status}</span>
              <span>
                {item.title} — {item.artist}
              </span>
              {item.reason ? <span className="muted">{item.reason}</span> : null}
            </div>
          ))
        )}
      </div>
    </section>
  );
}
