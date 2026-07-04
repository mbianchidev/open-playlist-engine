import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  beginAuth,
  completeAuth,
  createMigration,
  getAccounts,
  getPlaylist,
  getPlaylists,
  getProviders,
  testAccountConnection,
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
  const [ytHeaderFallback, setYtHeaderFallback] = useState(false);
  const [deviceChallenge, setDeviceChallenge] = useState<DeviceChallenge | null>(null);
  const [activeAuthProvider, setActiveAuthProvider] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [showMigratedPlaylists, setShowMigratedPlaylists] = useState(false);
  const [busy, setBusy] = useState(false);
  const authPollId = useRef(0);

  const sourceAccount = accounts.find((a) => a.provider === source) ?? null;
  const targetAccount = accounts.find((a) => a.provider === target) ?? null;
  const playlistContext = {
    targetProvider: target,
    targetAccountId: targetAccount?.id ?? null,
  };
  const selectedMigrationPlaylistIds = getSelectedMigrationPlaylistIds(
    selectedPlaylists,
    playlistTracks,
    selectedTracks,
  );
  const startDisabled =
    !source ||
    !target ||
    !sourceAccount ||
    !targetAccount ||
    selectedMigrationPlaylistIds.length === 0 ||
    busy;
  const ytHeaderStatus = getYtHeaderStatus(ytHeaders);
  const migratedPlaylists = playlists.filter(isAnnotatedMigratedPlaylist);
  const migrationCandidatePlaylists = playlists.filter(
    (playlist) => !isAnnotatedMigratedPlaylist(playlist),
  );
  const selectedCandidateCount = migrationCandidatePlaylists.filter((playlist) =>
    selectedPlaylists.has(playlist.id),
  ).length;

  const refreshSourcePlaylists = useCallback(
    async (options: { resetSelection?: boolean } = {}) => {
      if (!source || !sourceAccount) return;
      try {
        const rows = await getPlaylists(source, sourceAccount.id, playlistContext);
        setPlaylists(rows);
        if (options.resetSelection) setSelectedPlaylists(new Set());
      } catch (e: unknown) {
        setError(errorMessage(e));
      }
    },
    [source, sourceAccount?.id, target, targetAccount?.id],
  );

  useEffect(() => {
    getProviders().then(setProviders).catch((e: unknown) => setError(errorMessage(e)));
    refreshAccounts();
  }, []);

  useEffect(() => {
    authPollId.current += 1;
    setDeviceChallenge(null);
    setYtHeaderFallback(false);
    setActiveAuthProvider(null);
  }, [source, target]);

  useEffect(() => {
    setPlaylists([]);
    setSelectedPlaylists(new Set());
    setPlaylistTracks({});
    setSelectedTracks({});
    void refreshSourcePlaylists({ resetSelection: true });
  }, [refreshSourcePlaylists]);

  async function refreshAccounts() {
    try {
      const rows = await getAccounts(undefined, true);
      setAccounts((prev) => {
        if (prev.length > rows.length) {
          setNotice("Expired account disconnected. Reconnect before migrating.");
        }
        return rows;
      });
    } catch (e: unknown) {
      setError(errorMessage(e));
    }
  }

  async function connect(provider: string) {
    const pollId = authPollId.current + 1;
    authPollId.current = pollId;
    setBusy(true);
    setError(null);
    setNotice(null);
    setDeviceChallenge(null);
    setActiveAuthProvider(provider);
    if (provider === "ytmusic") setYtHeaderFallback(false);
    try {
      const challenge = await beginAuth(provider);
      if (challenge.shape === "redirect" && challenge.redirect_url) {
        window.open(challenge.redirect_url, "_blank", "noopener,noreferrer");
        setNotice("Finish Spotify auth in the new tab, then refresh accounts.");
        return;
      }
      if (challenge.shape === "form") {
        if (provider === "ytmusic") showYtHeaderFallback(provider);
        setNotice(challenge.instructions ?? "Paste provider credentials below.");
        return;
      }
      if (challenge.shape === "device_code") {
        if (!challenge.user_code || !challenge.verification_url || !challenge.state) {
          throw new Error("Device-code challenge is missing required fields.");
        }
        const nextChallenge = {
          provider,
          userCode: challenge.user_code,
          verificationUrl: challenge.verification_url,
          state: challenge.state,
          pollIntervalS: challenge.poll_interval_s ?? 5,
        };
        setDeviceChallenge(nextChallenge);
        setNotice("Enter the device code in your browser to finish YouTube Music auth.");
        void pollDeviceAuth(nextChallenge, pollId);
        return;
      }
      setNotice("Unsupported auth challenge.");
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function pollDeviceAuth(challenge: DeviceChallenge, pollId: number) {
    let intervalS = Math.max(1, challenge.pollIntervalS);
    while (authPollId.current === pollId) {
      await sleep(intervalS * 1000);
      if (authPollId.current !== pollId) return;
      try {
        await completeAuth(challenge.provider, { state: challenge.state });
        if (authPollId.current !== pollId) return;
        setDeviceChallenge(null);
        setActiveAuthProvider(null);
        setYtHeaderFallback(false);
        setNotice("YouTube Music connected.");
        await refreshAccounts();
        return;
      } catch (e: unknown) {
        const message = errorMessage(e);
        if (message === "authorization_pending") continue;
        if (message === "slow_down") {
          intervalS += 5;
          continue;
        }
        if (authPollId.current !== pollId) return;
        setDeviceChallenge(null);
        if (challenge.provider === "ytmusic" && isGoogleAccessDenied(message)) {
          showYtHeaderFallback(challenge.provider);
          setError(`${message}. Use browser-session headers below instead.`);
          return;
        }
        setError(message);
        return;
      }
    }
  }

  function showYtHeaderFallback(provider = "ytmusic") {
    authPollId.current += 1;
    setDeviceChallenge(null);
    setActiveAuthProvider(provider);
    setYtHeaderFallback(true);
    setError(null);
    setNotice("Use browser-session headers below. Keep them private.");
  }

  async function connectYouTubeMusic() {
    if (!ytHeaders.trim()) {
      setError("Paste YouTube Music request headers first.");
      return;
    }
    if (!ytHeaderStatus.ready) {
      setError(`Missing YouTube Music request headers: ${ytHeaderStatus.missing.join(", ")}.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await completeAuth("ytmusic", { headers_raw: ytHeaders });
      setYtHeaders("");
      setYtHeaderFallback(false);
      setActiveAuthProvider(null);
      setNotice("YouTube Music connected.");
      await refreshAccounts();
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function testConnection(account: AccountView) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await testAccountConnection(account.id);
      setNotice(result.message);
      await refreshAccounts();
    } catch (e: unknown) {
      setError(errorMessage(e));
      await refreshAccounts();
    } finally {
      setBusy(false);
    }
  }

  async function start(acknowledgeWarnings = false) {
    if (!source || !target || !sourceAccount || !targetAccount) return;
    const playlistIds = selectedMigrationPlaylistIds;
    const tracks = Object.fromEntries(
      playlistIds
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
        selection: { playlist_ids: playlistIds, tracks },
        acknowledge_warnings: acknowledgeWarnings,
      });
      setJobId(job.id);
      deselectStartedPlaylists(playlistIds);
    } catch (e: unknown) {
      if (isMigrationWarning(e) && confirm(warningMessage(e.detail))) {
        await start(true);
        return;
      }
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function togglePlaylist(id: string) {
    if (selectedPlaylists.has(id)) {
      setSelectedPlaylists((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      closePlaylistSongs(id);
      return;
    }
    setSelectedPlaylists((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    const loaded = playlistTracks[id];
    if (loaded) {
      setSelectedTracks((prevTracks) => ({
        ...prevTracks,
        [id]: new Set(loaded.map(trackKey)),
      }));
    }
  }

  function selectAllPlaylists() {
    setSelectedPlaylists(new Set(migrationCandidatePlaylists.map((playlist) => playlist.id)));
  }

  function deselectAllPlaylists() {
    setSelectedPlaylists(new Set());
    setPlaylistTracks({});
    setSelectedTracks({});
  }

  function deselectStartedPlaylists(playlistIds: string[]) {
    const started = new Set(playlistIds);
    setSelectedPlaylists((prev) => {
      const next = new Set(prev);
      for (const id of started) next.delete(id);
      return next;
    });
    setPlaylistTracks((prev) => {
      const next = { ...prev };
      for (const id of started) delete next[id];
      return next;
    });
    setSelectedTracks((prev) => {
      const next = { ...prev };
      for (const id of started) delete next[id];
      return next;
    });
  }

  async function loadTracks(playlist: PlaylistRef) {
    if (!source || !sourceAccount) return;
    setBusy(true);
    setError(null);
    try {
      const detail = await getPlaylist(source, sourceAccount.id, playlist.id, playlistContext);
      const defaultSelected = detail.tracks
        .filter((track) => track.migration_status !== "migrated")
        .map(trackKey);
      setPlaylistTracks((prev) => ({ ...prev, [playlist.id]: detail.tracks }));
      setSelectedTracks((prev) => ({
        ...prev,
        [playlist.id]: new Set(defaultSelected),
      }));
      setSelectedPlaylists((prev) => {
        const next = new Set(prev);
        if (defaultSelected.length === 0) next.delete(playlist.id);
        else next.add(playlist.id);
        return next;
      });
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleTrack(playlistId: string, key: string) {
    const next = new Set(selectedTracks[playlistId] ?? []);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    if (next.size === 0) {
      setSelectedPlaylists((selected) => {
        const selectedNext = new Set(selected);
        selectedNext.delete(playlistId);
        return selectedNext;
      });
      closePlaylistSongs(playlistId);
      return;
    }
    setSelectedTracks((prev) => {
      return { ...prev, [playlistId]: next };
    });
  }

  function selectPlaylistSongs(playlistId: string, mode: "all" | "leftovers" | "none") {
    if (mode === "none") {
      setSelectedPlaylists((prev) => {
        const next = new Set(prev);
        next.delete(playlistId);
        return next;
      });
      closePlaylistSongs(playlistId);
      return;
    }
    const tracks = playlistTracks[playlistId] ?? [];
    const keys = tracks
      .filter((track) => mode === "all" || track.migration_status !== "migrated")
      .map(trackKey);
    setSelectedTracks((prev) => ({ ...prev, [playlistId]: new Set(keys) }));
    setSelectedPlaylists((prev) => {
      const next = new Set(prev);
      if (keys.length === 0) next.delete(playlistId);
      else next.add(playlistId);
      return next;
    });
    if (keys.length === 0) closePlaylistSongs(playlistId);
  }

  function closePlaylistSongs(playlistId: string) {
    setPlaylistTracks((prev) => {
      const next = { ...prev };
      delete next[playlistId];
      return next;
    });
    setSelectedTracks((prev) => {
      const next = { ...prev };
      delete next[playlistId];
      return next;
    });
  }

  function renderPlaylistCard(playlist: PlaylistRef) {
    return (
      <div key={playlist.id} className="playlist-card">
        <label className="playlist-row">
          <input
            type="checkbox"
            checked={selectedPlaylists.has(playlist.id)}
            onChange={() => togglePlaylist(playlist.id)}
          />
          <span>{playlist.name}</span>
          {playlist.migration_note ? (
            <span className={`badge migration-${playlist.migration_status ?? "none"}`}>
              {playlist.migration_note}
            </span>
          ) : null}
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
        {selectedPlaylists.has(playlist.id) && playlistTracks[playlist.id] ? (
          <div className="track-list">
            <div className="track-toolbar">
              <button
                className="secondary compact"
                disabled={busy}
                onClick={() => selectPlaylistSongs(playlist.id, "all")}
              >
                Select all songs
              </button>
              <button
                className="secondary compact"
                disabled={busy}
                onClick={() => selectPlaylistSongs(playlist.id, "leftovers")}
              >
                Select leftovers
              </button>
              <button
                className="secondary compact"
                disabled={busy}
                onClick={() => selectPlaylistSongs(playlist.id, "none")}
              >
                Deselect playlist
              </button>
            </div>
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
                    {track.migration_status === "migrated" ? (
                      <span className="badge inline migration-migrated">migrated</span>
                    ) : playlist.migration_status === "partial" ? (
                      <span className="badge inline migration-partial">leftover</span>
                    ) : null}
                  </span>
                  <span className="muted">{track.album ?? ""}</span>
                </label>
              );
            })}
          </div>
        ) : null}
      </div>
    );
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
            onTest={testConnection}
          />
          <AccountPanel
            label="Target"
            provider={target}
            account={targetAccount}
            busy={busy}
            onConnect={connect}
            onTest={testConnection}
          />
        </div>
        {deviceChallenge ? (
          <div className="device-block">
            <p className="muted">Open this page and enter the code:</p>
            <a href={deviceChallenge.verificationUrl} target="_blank" rel="noreferrer">
              {deviceChallenge.verificationUrl}
            </a>
            <code>{deviceChallenge.userCode}</code>
            <p className="muted">Waiting for Google to confirm authorization…</p>
            {deviceChallenge.provider === "ytmusic" ? (
              <div className="fallback-callout">
                <p>
                  Google says the app is not verified? Skip OAuth and use your signed-in browser
                  session instead.
                </p>
                <button
                  className="secondary compact"
                  disabled={busy}
                  onClick={() => showYtHeaderFallback(deviceChallenge.provider)}
                >
                  Use browser-session headers
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {activeAuthProvider === "ytmusic" && ytHeaderFallback ? (
          <div className="form-block header-guide">
            <div className="guide-heading">
              <div>
                <p className="eyebrow">YouTube Music fallback</p>
                <h3>Copy one real request from your browser</h3>
                <p className="muted">
                  These headers act like your YouTube Music session. Keep them local and do not
                  share them.
                </p>
              </div>
              <a className="button-link" href="https://music.youtube.com" target="_blank" rel="noreferrer">
                Open YouTube Music
              </a>
            </div>
            <ol className="header-steps">
              <li>Open YouTube Music while signed in.</li>
              <li>Open DevTools, then choose the Network tab.</li>
              <li>Search for a song or open a playlist so requests appear.</li>
              <li>
                Pick a <code>POST</code> request to <code>music.youtube.com/youtubei/v1</code>.
              </li>
              <li>
                In Headers, copy request headers from <code>authorization</code> through{" "}
                <code>x-youtube-client-version</code>.
              </li>
              <li>Paste below. The checks light up when the important headers are found.</li>
            </ol>
            <div className="endpoint-strip" aria-label="Good request paths">
              <span>/browse</span>
              <span>/search</span>
              <span>/music/get_search_suggestions</span>
            </div>
            <div className="header-checks" aria-live="polite">
              {ytHeaderStatus.checks.map((check) => (
                <span key={check.name} className={`header-check ${check.present ? "ok" : ""}`}>
                  {check.present ? "Found" : "Missing"} {check.name}
                </span>
              ))}
              {ytHeaderStatus.isJson ? (
                <span className="header-check ok">Detected ytmusicapi JSON</span>
              ) : null}
            </div>
            <label htmlFor="ytHeaders">Paste request headers</label>
            <textarea
              id="ytHeaders"
              value={ytHeaders}
              onChange={(e) => setYtHeaders(e.target.value)}
              placeholder={"authorization: SAPISIDHASH ...\ncookie: ...\nx-goog-authuser: 0\nx-youtube-client-version: ..."}
            />
            {ytHeaders.trim() && !ytHeaderStatus.ready ? (
              <p className="warn">Still missing: {ytHeaderStatus.missing.join(", ")}.</p>
            ) : null}
            <div className="toolbar">
              <button
                className="secondary"
                disabled={busy || !ytHeaderStatus.ready}
                onClick={connectYouTubeMusic}
              >
                Connect YouTube Music
              </button>
              <button
                className="secondary"
                disabled={busy || !ytHeaders}
                onClick={() => setYtHeaders("")}
              >
                Clear pasted headers
              </button>
            </div>
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
                disabled={
                  busy ||
                  migrationCandidatePlaylists.length === 0 ||
                  selectedCandidateCount === migrationCandidatePlaylists.length
                }
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
          <div className="migration-top-stack">
            <div className="migration-action-bar">
              <div>
                <p className="eyebrow">Ready to migrate</p>
                <p className="muted">
                  {selectedMigrationPlaylistIds.length} playlist
                  {selectedMigrationPlaylistIds.length === 1 ? "" : "s"} selected for migration
                </p>
              </div>
              <button className="primary" disabled={startDisabled} onClick={() => start()}>
                {busy ? "Starting…" : "Start migration"}
              </button>
            </div>
            {jobId ? (
              <div className="migration-progress-slot">
                <ProgressBoard
                  className="progress-popover"
                  jobId={jobId}
                  onMigrationChanged={refreshSourcePlaylists}
                  onReconnectProvider={connect}
                />
              </div>
            ) : null}
          </div>
          {migratedPlaylists.length > 0 ? (
            <div className="migrated-playlists-panel">
              <button
                className="migrated-playlists-toggle"
                type="button"
                aria-expanded={showMigratedPlaylists}
                onClick={() => setShowMigratedPlaylists((open) => !open)}
              >
                <span>Migrated playlists</span>
                <span className="muted">
                  {migratedPlaylists.length} migrated or partially migrated
                </span>
                <span aria-hidden="true">{showMigratedPlaylists ? "Hide" : "Show"}</span>
              </button>
              {showMigratedPlaylists ? (
                <div className="playlist-list migrated-playlist-list">
                  {migratedPlaylists.map(renderPlaylistCard)}
                </div>
              ) : null}
            </div>
          ) : null}
          {playlists.length === 0 ? (
            <p className="muted">No playlists found yet.</p>
          ) : migrationCandidatePlaylists.length === 0 ? (
            <p className="muted">No unmigrated playlists left.</p>
          ) : (
            <div className="playlist-list">
              {migrationCandidatePlaylists.map(renderPlaylistCard)}
            </div>
          )}
        </section>
      ) : null}

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

function isAnnotatedMigratedPlaylist(playlist: PlaylistRef): boolean {
  return playlist.migration_status === "migrated" || playlist.migration_status === "partial";
}

function trackKey(track: Track): string {
  return track.source_item_id ?? track.id ?? String(track.position ?? track.title);
}

interface MigrationWarningDetail {
  code: string;
  message: string;
  warnings: { code: string; message: string }[];
}

function isMigrationWarning(error: unknown): error is ApiError & { detail: MigrationWarningDetail } {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  if (!error.detail || typeof error.detail !== "object") return false;
  const detail = error.detail as Partial<MigrationWarningDetail>;
  return detail.code === "migration_warnings" && Array.isArray(detail.warnings);
}

function warningMessage(detail: MigrationWarningDetail): string {
  return [
    detail.message,
    "",
    ...detail.warnings.map((warning) => `- ${warning.message}`),
    "",
    "Continue anyway?",
  ].join("\n");
}

interface DeviceChallenge {
  provider: string;
  userCode: string;
  verificationUrl: string;
  state: string;
  pollIntervalS: number;
}

const YT_REQUIRED_HEADERS = ["authorization", "cookie", "x-goog-authuser", "x-youtube-client-version"];

interface HeaderCheck {
  name: string;
  present: boolean;
}

interface YtHeaderStatus {
  checks: HeaderCheck[];
  missing: string[];
  ready: boolean;
  isJson: boolean;
}

function getYtHeaderStatus(raw: string): YtHeaderStatus {
  const trimmed = raw.trim();
  const isJson = trimmed.startsWith("{");
  const lines = trimmed
    .split(/\r?\n/)
    .map((line) => line.trim().toLowerCase())
    .filter(Boolean);
  const checks = YT_REQUIRED_HEADERS.map((name) => ({
    name,
    present: isJson || lines.some((line) => line === name || line.startsWith(`${name}:`)),
  }));
  const missing = checks.filter((check) => !check.present).map((check) => check.name);
  return {
    checks,
    missing,
    ready: isJson || (trimmed.length > 0 && missing.length === 0),
    isJson,
  };
}

function isGoogleAccessDenied(message: string): boolean {
  return message.includes("access_denied") || message.toLowerCase().includes("authorization was denied");
}

interface AccountPanelProps {
  label: string;
  provider: string | null;
  account: AccountView | null;
  busy: boolean;
  onConnect: (provider: string) => void;
  onTest: (account: AccountView) => void;
}

function AccountPanel({ label, provider, account, busy, onConnect, onTest }: AccountPanelProps) {
  return (
    <div>
      <h3>{label}</h3>
      {!provider ? <p className="muted">Pick a provider first.</p> : null}
      {provider && account ? (
        <>
          <p className="connected">
            Connected: {account.display_name ?? account.provider_user_id ?? account.id}
          </p>
          <button className="secondary compact" disabled={busy} onClick={() => onTest(account)}>
            Test connection
          </button>
          <button className="secondary compact" disabled={busy} onClick={() => onConnect(provider)}>
            Reconnect
          </button>
        </>
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}
