import { useEffect, useState } from "react";
import {
  beginAuth,
  completeAuth,
  createMigration,
  getAccounts,
  getPlaylist,
  getPlaylists,
  getProviders,
} from "./api/client";
import type { AccountView, PlaylistRef, ProviderView, Track } from "./api/types";
import ProviderPicker from "./components/ProviderPicker";
import ProgressBoard from "./components/ProgressBoard";

export default function App() {
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [accounts, setAccounts] = useState<AccountView[]>([]);
  const [playlists, setPlaylists] = useState<PlaylistRef[]>([]);
  const [selectedPlaylists, setSelectedPlaylists] = useState<Set<string>>(new Set());
  const [playlistTracks, setPlaylistTracks] = useState<Record<string, Track[]>>({});
  const [selectedTracks, setSelectedTracks] = useState<Record<string, Set<string>>>({});
  const [ytHeaders, setYtHeaders] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const sourceAccount = accounts.find((a) => a.provider === source) ?? null;
  const targetAccount = accounts.find((a) => a.provider === target) ?? null;
  const selectedMigrationPlaylistIds = getSelectedMigrationPlaylistIds(
    selectedPlaylists,
    playlistTracks,
    selectedTracks,
  );

  useEffect(() => {
    getProviders().then(setProviders).catch((e: unknown) => setError(errorMessage(e)));
    refreshAccounts();
  }, []);

  useEffect(() => {
    setPlaylists([]);
    setSelectedPlaylists(new Set());
    setPlaylistTracks({});
    setSelectedTracks({});
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
    const tracks = Object.fromEntries(
      selectedMigrationPlaylistIds
        .filter((id) => playlistTracks[id])
        .map((id) => [id, [...(selectedTracks[id] ?? new Set<string>())]]),
    );
    setBusy(true);
    setError(null);
    try {
      const job = await createMigration({
        source_provider: source,
        target_provider: target,
        source_account_id: sourceAccount.id,
        target_account_id: targetAccount.id,
        selection: { playlist_ids: selectedMigrationPlaylistIds, tracks },
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

  function selectAllPlaylists() {
    setSelectedPlaylists(new Set(playlists.map((playlist) => playlist.id)));
  }

  function deselectAllPlaylists() {
    setSelectedPlaylists(new Set());
  }

  async function loadTracks(playlist: PlaylistRef) {
    if (!source || !sourceAccount) return;
    setBusy(true);
    setError(null);
    try {
      const detail = await getPlaylist(source, sourceAccount.id, playlist.id);
      setPlaylistTracks((prev) => ({ ...prev, [playlist.id]: detail.tracks }));
      setSelectedTracks((prev) => ({
        ...prev,
        [playlist.id]: new Set(detail.tracks.map(trackKey)),
      }));
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleTrack(playlistId: string, key: string) {
    setSelectedTracks((prev) => {
      const next = new Set(prev[playlistId] ?? []);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return { ...prev, [playlistId]: next };
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
          <div className="section-heading">
            <div>
              <h2>Pick playlists</h2>
              <p className="muted">
                {selectedPlaylists.size} of {playlists.length} selected
              </p>
            </div>
            <div className="toolbar">
              <button
                className="secondary compact"
                disabled={busy || playlists.length === 0 || selectedPlaylists.size === playlists.length}
                onClick={selectAllPlaylists}
              >
                Select all
              </button>
              <button
                className="secondary compact"
                disabled={busy || selectedPlaylists.size === 0}
                onClick={deselectAllPlaylists}
              >
                Deselect all
              </button>
            </div>
          </div>
          {playlists.length === 0 ? (
            <p className="muted">No playlists found yet.</p>
          ) : (
            <div className="playlist-list">
              {playlists.map((playlist) => (
                <div key={playlist.id} className="playlist-card">
                  <label className="playlist-row">
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
                  {selectedPlaylists.has(playlist.id) ? (
                    <button
                      className="secondary compact"
                      disabled={busy}
                      onClick={() => loadTracks(playlist)}
                    >
                      {playlistTracks[playlist.id] ? "Reload tracks" : "Choose tracks"}
                    </button>
                  ) : null}
                  {playlistTracks[playlist.id] ? (
                    <div className="track-list">
                      {playlistTracks[playlist.id].map((track) => {
                        const key = trackKey(track);
                        return (
                          <label key={key} className="track-row">
                            <input
                              type="checkbox"
                              checked={selectedTracks[playlist.id]?.has(key) ?? false}
                              onChange={() => toggleTrack(playlist.id, key)}
                            />
                            <span>
                              {track.title} — {track.artist}
                              {track.explicit ? <span className="badge inline">explicit</span> : null}
                            </span>
                            <span className="muted">{track.album ?? ""}</span>
                          </label>
                        );
                      })}
                    </div>
                  ) : null}
                </div>
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
          selectedMigrationPlaylistIds.length === 0 ||
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

function getSelectedMigrationPlaylistIds(
  selectedPlaylists: Set<string>,
  playlistTracks: Record<string, Track[]>,
  selectedTracks: Record<string, Set<string>>,
): string[] {
  return [...selectedPlaylists].filter((id) => {
    const loaded = playlistTracks[id];
    if (!loaded) return true;
    return (selectedTracks[id]?.size ?? 0) > 0;
  });
}

function trackKey(track: Track): string {
  return track.source_item_id ?? track.id ?? String(track.position ?? track.title);
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
