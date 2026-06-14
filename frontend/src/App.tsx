import { useEffect, useState } from "react";
import {
  beginAuth,
  completeAuth,
  createMigration,
  getAccounts,
  getPlaylists,
  getProviders,
} from "./api/client";
import type { AccountView, PlaylistRef, ProviderView } from "./api/types";
import ProviderPicker from "./components/ProviderPicker";
import ProgressBoard from "./components/ProgressBoard";

export default function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [accounts, setAccounts] = useState<AccountView[]>([]);
  const [playlists, setPlaylists] = useState<PlaylistRef[]>([]);
  const [selectedPlaylists, setSelectedPlaylists] = useState<Set<string>>(new Set());
  const [ytHeaders, setYtHeaders] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const sourceAccount = accounts.find((a) => a.provider === source) ?? null;
  const targetAccount = accounts.find((a) => a.provider === target) ?? null;

  useEffect(() => {
    getProviders().then(setProviders).catch((e: unknown) => setError(errorMessage(e)));
    refreshAccounts();
  }, []);

  useEffect(() => {
    setPlaylists([]);
    setSelectedPlaylists(new Set());
    if (!source || !sourceAccount) return;
    getPlaylists(source, sourceAccount.id)
      .then((rows) => {
        setPlaylists(rows);
        setSelectedPlaylists(new Set(rows.map((p) => p.id)));
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [source, sourceAccount?.id]);

  async function refreshAccounts() {
    try {
      setAccounts(await getAccounts());
    } catch (e: unknown) {
      setError(errorMessage(e));
    }
  }

  async function connect(provider: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const challenge = await beginAuth(provider);
      if (challenge.shape === "redirect" && challenge.redirect_url) {
        window.open(challenge.redirect_url, "_blank", "noopener,noreferrer");
        setNotice("Finish Spotify auth in the new tab, then refresh accounts.");
        return;
      }
      if (challenge.shape === "form") {
        setNotice(challenge.instructions ?? "Paste provider credentials below.");
        return;
      }
      setNotice("Device-code auth is not implemented yet for this provider.");
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function connectYouTubeMusic() {
    if (!ytHeaders.trim()) {
      setError("Paste YouTube Music request headers first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await completeAuth("ytmusic", { headers_raw: ytHeaders });
      setYtHeaders("");
      setNotice("YouTube Music connected.");
      await refreshAccounts();
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    if (!source || !target || !sourceAccount || !targetAccount) return;
    setBusy(true);
    setError(null);
    try {
      const job = await createMigration({
        source_provider: source,
        target_provider: target,
        source_account_id: sourceAccount.id,
        target_account_id: targetAccount.id,
        selection: { playlist_ids: [...selectedPlaylists], tracks: {} },
      });
      setJobId(job.id);
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function togglePlaylist(id: string) {
    setSelectedPlaylists((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="app">
      <h1>Open Playlist Engine</h1>
      <p className="subtitle">Migrate playlists between any two music services.</p>

      {error ? <p className="warn">⚠ {error}</p> : null}
      {notice ? <p className="notice">{notice}</p> : null}

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

      <section className="card flow">
        <h2>Connect accounts</h2>
        <div className="account-grid">
          <AccountPanel
            label="Source"
            provider={source}
            account={sourceAccount}
            busy={busy}
            onConnect={connect}
          />
          <AccountPanel
            label="Target"
            provider={target}
            account={targetAccount}
            busy={busy}
            onConnect={connect}
          />
        </div>
        {target === "ytmusic" && !targetAccount ? (
          <div className="form-block">
            <label htmlFor="ytHeaders">YouTube Music request headers</label>
            <textarea
              id="ytHeaders"
              value={ytHeaders}
              onChange={(e) => setYtHeaders(e.target.value)}
              placeholder="Paste headers copied from an authenticated music.youtube.com /browse POST request."
            />
            <button className="secondary" disabled={busy} onClick={connectYouTubeMusic}>
              Connect YouTube Music
            </button>
          </div>
        ) : null}
        <button className="secondary" disabled={busy} onClick={refreshAccounts}>
          Refresh accounts
        </button>
      </section>

      {source && sourceAccount ? (
        <section className="card flow">
          <h2>Pick playlists</h2>
          {playlists.length === 0 ? (
            <p className="muted">No playlists found yet.</p>
          ) : (
            <div className="playlist-list">
              {playlists.map((playlist) => (
                <label key={playlist.id} className="playlist-row">
                  <input
                    type="checkbox"
                    checked={selectedPlaylists.has(playlist.id)}
                    onChange={() => togglePlaylist(playlist.id)}
                  />
                  <span>{playlist.name}</span>
                  <span className="muted">
                    {playlist.track_count === null ? "" : `${playlist.track_count} tracks`}
                  </span>
                </label>
              ))}
            </div>
          )}
        </section>
      ) : null}

      <button
        className="primary"
        disabled={
          !source ||
          !target ||
          !sourceAccount ||
          !targetAccount ||
          selectedPlaylists.size === 0 ||
          busy
        }
        onClick={start}
      >
        {busy ? "Starting…" : "Start migration"}
      </button>

      {jobId ? <ProgressBoard jobId={jobId} /> : null}
    </div>
  );
}

interface AccountPanelProps {
  label: string;
  provider: string | null;
  account: AccountView | null;
  busy: boolean;
  onConnect: (provider: string) => void;
}

function AccountPanel({ label, provider, account, busy, onConnect }: AccountPanelProps) {
  return (
    <div>
      <h3>{label}</h3>
      {!provider ? <p className="muted">Pick a provider first.</p> : null}
      {provider && account ? (
        <p className="connected">
          Connected: {account.display_name ?? account.provider_user_id ?? account.id}
        </p>
      ) : null}
      {provider && !account ? (
        <button className="secondary" disabled={busy} onClick={() => onConnect(provider)}>
          Connect {provider}
        </button>
      ) : null}
    </div>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
