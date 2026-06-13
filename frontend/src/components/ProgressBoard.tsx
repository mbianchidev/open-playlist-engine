import { useEffect, useState } from "react";
import { subscribeProgress } from "../api/client";

interface Props {
  jobId: string;
}

// Phase 5 — live progress. Reconnect/replay via Last-Event-ID is handled by the
// browser's EventSource plus the backend's persisted job_item cursor.
export default function ProgressBoard({ jobId }: Props) {
  const [lines, setLines] = useState<string[]>([]);

  useEffect(() => {
    const dispose = subscribeProgress(jobId, (e) => {
      setLines((prev) => [...prev, e.data]);
    });
    return dispose;
  }, [jobId]);

  return (
    <section className="card">
      <h2>Progress · {jobId}</h2>
      <div className="events">
        {lines.length === 0 ? "waiting for events…" : lines.join("\n")}
      </div>
    </section>
  );
}
