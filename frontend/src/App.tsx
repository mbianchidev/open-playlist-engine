import { useEffect, useState } from "react";
import { createMigration, getProviders } from "./api/client";
import type { ProviderView } from "./api/types";
import ProviderPicker from "./components/ProviderPicker";
import ProgressBoard from "./components/ProgressBoard";

export default function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getProviders().then(setProviders).catch((e: unknown) => setError(String(e)));
  }, []);

  async function start() {
    if (!source || !target) return;
    setBusy(true);
    setError(null);
    try {
      // Account selection + playlist picking land between connect and migrate;
      // this kicks the job off with empty selection for now.
      const job = await createMigration({
        source_provider: source,
        target_provider: target,
        source_account_id: "local",
        target_account_id: "local",
        selection: { playlist_ids: [], tracks: {} },
      });
      setJobId(job.id);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <h1>Open Playlist Engine</h1>
      <p className="subtitle">Migrate playlists between any two music services.</p>

      {error ? <p className="warn">⚠ {error}</p> : null}

      <div className="lanes">
        <ProviderPicker
          title="From"
          role="source"
          providers={providers}
          selected={source}
          onSelect={setSource}
        />
        <ProviderPicker
          title="To"
          role="target"
          providers={providers}
          selected={target}
          onSelect={setTarget}
        />
      </div>

      <button className="primary" disabled={!source || !target || busy} onClick={start}>
        {busy ? "Starting…" : "Start migration"}
      </button>

      {jobId ? <ProgressBoard jobId={jobId} /> : null}
    </div>
  );
}
