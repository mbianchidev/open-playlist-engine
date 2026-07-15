import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  BarChart3,
  Check,
  CircleGauge,
  FileText,
  Link2,
  ListMusic,
  Music2,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Wifi,
} from "lucide-react";
import {
  ApiError,
  beginAuth,
  completeAuth,
  createMigration,
  getAccounts,
  getPlaylist,
  getPlaylists,
  getProviders,
  preflightMigration,
  previewImport,
  testAccountConnection,
} from "./api/client";
import type {
  AccountView,
  AuthChallenge,
  CreateMigrationBody,
  ImportPreview,
  MigrationWarningsView,
  PlaylistRef,
  ProviderView,
  Track,
} from "./api/types";
import MigrationStatsPanel from "./components/MigrationStatsPanel";
import ProviderPicker from "./components/ProviderPicker";
import ProviderIcon from "./components/ProviderIcon";
import ProgressBoard from "./components/ProgressBoard";
import { providerLabel } from "./utils/providers";

export default function App() {
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("migration");
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [accounts, setAccounts] = useState<AccountView[]>([]);
  const [playlists, setPlaylists] = useState<PlaylistRef[]>([]);
  const [selectedPlaylists, setSelectedPlaylists] = useState<Set<string>>(new Set());
  const [playlistTracks, setPlaylistTracks] = useState<Record<string, Track[]>>({});
  const [selectedTracks, setSelectedTracks] = useState<Record<string, Set<string>>>({});
  const [ytHeaders, setYtHeaders] = useState("");
  const [ytHeaderFallback, setYtHeaderFallback] = useState(false);
  const [appleAuthChallenge, setAppleAuthChallenge] = useState<AppleMusicChallenge | null>(null);
  const [appleMusicConfigured, setAppleMusicConfigured] = useState(false);
  const [appleUserToken, setAppleUserToken] = useState("");
  const [musicKitReady, setMusicKitReady] = useState(() => Boolean(window.MusicKit));
  const [deviceChallenge, setDeviceChallenge] = useState<DeviceChallenge | null>(null);
  const [activeAuthProvider, setActiveAuthProvider] = useState<string | null>(null);
  const [blockingAlert, setBlockingAlert] = useState<BlockingAlert | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playlistError, setPlaylistError] = useState<string | null>(null);
  const [playlistLoading, setPlaylistLoading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [sourceMode, setSourceMode] = useState<SourceMode>("account");
  const [source, setSource] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [importUrl, setImportUrl] = useState("");
  const [importText, setImportText] = useState("");
  const [importName, setImportName] = useState("");
  const [importPreview, setImportPreview] = useState<ImportPreview | null>(null);
  const [importRequiredProvider, setImportRequiredProvider] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [showMigratedPlaylists, setShowMigratedPlaylists] = useState(false);
  const [showBlockedSpotifyPlaylists, setShowBlockedSpotifyPlaylists] = useState(false);
  const [statsRefreshKey, setStatsRefreshKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const authPollId = useRef(0);
  const playlistLoadId = useRef(0);
  const configuredAppleToken = useRef<string | null>(null);
  const migrationTabRef = useRef<HTMLButtonElement>(null);
  const statsTabRef = useRef<HTMLButtonElement>(null);

  const sourceAccount = accounts.find((a) => a.provider === source) ?? null;
  const importSourceAccount =
    accounts.find((account) => account.provider === importRequiredProvider) ?? null;
  const targetAccount = accounts.find((a) => a.provider === target) ?? null;
  const blockedSpotifyPlaylists = playlists.filter((playlist) =>
    sourceMode === "account" && isSpotifyCopyRequiredPlaylist(playlist, source, sourceAccount),
  );
  const blockedSpotifyPlaylistIds = new Set(blockedSpotifyPlaylists.map((playlist) => playlist.id));
  const availablePlaylists = playlists.filter((playlist) => !blockedSpotifyPlaylistIds.has(playlist.id));
  const selectedMigrationPlaylistIds = getSelectedMigrationPlaylistIds(
    selectedPlaylists,
    playlistTracks,
    selectedTracks,
  ).filter((id) => !blockedSpotifyPlaylistIds.has(id));
  const selectedMigrationPlaylists = selectedMigrationPlaylistIds
    .map((id) => availablePlaylists.find((playlist) => playlist.id === id))
    .filter((playlist): playlist is PlaylistRef => Boolean(playlist));
  const startDisabled =
    !target ||
    !targetAccount ||
    (sourceMode === "account" ? !source || !sourceAccount : !importPreview) ||
    selectedMigrationPlaylistIds.length === 0 ||
    busy;
  const ytHeaderStatus = getYtHeaderStatus(ytHeaders);
  const migratedPlaylists = availablePlaylists.filter(isAnnotatedMigratedPlaylist);
  const migrationCandidatePlaylists = availablePlaylists.filter(
    (playlist) => !isAnnotatedMigratedPlaylist(playlist),
  );
  const playlistErrorTitle = playlistError ? playlistErrorHeading(playlistError) : null;
  const showBlockedPlaylistDetails =
    showBlockedSpotifyPlaylists ||
    (migrationCandidatePlaylists.length === 0 && blockedSpotifyPlaylists.length > 0);
  const showMigratedPlaylistDetails =
    showMigratedPlaylists ||
    (migrationCandidatePlaylists.length === 0 &&
      blockedSpotifyPlaylists.length === 0 &&
      migratedPlaylists.length > 0);
  const selectedCandidateCount = migrationCandidatePlaylists.filter((playlist) =>
    selectedPlaylists.has(playlist.id),
  ).length;
  const sourceLabel =
    sourceMode === "account"
      ? source
        ? providers.find((provider) => provider.name === source)?.display_name ??
          providerLabel(source)
        : "Choose source"
      : importPreview?.source.label ?? (sourceMode === "url" ? "Public URL" : "Pasted text");
  const targetLabel = target
    ? providers.find((provider) => provider.name === target)?.display_name ?? providerLabel(target)
    : "Choose target";

  const refreshSourcePlaylists = useCallback(
    async (options: { resetSelection?: boolean; forceRefresh?: boolean } = {}) => {
      if (
        sourceMode !== "account" ||
        !source ||
        !sourceAccount ||
        !target ||
        !targetAccount
      ) {
        setPlaylistLoading(false);
        return;
      }
      const loadId = playlistLoadId.current + 1;
      playlistLoadId.current = loadId;
      setPlaylistLoading(true);
      try {
        const rows = await getPlaylists(source, sourceAccount.id, {
          targetProvider: target,
          targetAccountId: targetAccount.id,
          refresh: options.forceRefresh,
        });
        if (loadId !== playlistLoadId.current) return;
        setPlaylists(rows);
        setPlaylistError(null);
        if (options.resetSelection) setSelectedPlaylists(new Set());
      } catch (e: unknown) {
        if (loadId !== playlistLoadId.current) return;
        const message = errorMessage(e);
        setPlaylistError(message);
        const alert = spotifyRateLimitAlert(e, message);
        if (alert) {
          setBlockingAlert(alert);
          setError(null);
        } else {
          setError(message);
        }
      } finally {
        if (loadId === playlistLoadId.current) setPlaylistLoading(false);
      }
    },
    [sourceMode, source, sourceAccount?.id, target, targetAccount?.id],
  );

  const handleMigrationChanged = useCallback(async () => {
    setStatsRefreshKey((value) => value + 1);
    await refreshSourcePlaylists();
  }, [refreshSourcePlaylists]);

  useEffect(() => {
    getProviders().then(setProviders).catch((e: unknown) => setError(errorMessage(e)));
    refreshAccounts();
  }, []);

  useEffect(() => {
    function handleMusicKitLoaded() {
      setMusicKitReady(true);
    }
    if (window.MusicKit) handleMusicKitLoaded();
    document.addEventListener("musickitloaded", handleMusicKitLoaded);
    return () => document.removeEventListener("musickitloaded", handleMusicKitLoaded);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const musicKit = window.MusicKit;
    if (!appleAuthChallenge || !musicKitReady || !musicKit) {
      setAppleMusicConfigured(false);
      return;
    }
    if (configuredAppleToken.current === appleAuthChallenge.developerToken) {
      setAppleMusicConfigured(true);
      return;
    }
    setAppleMusicConfigured(false);
    void musicKit
      .configure({
        developerToken: appleAuthChallenge.developerToken,
        app: { name: "Open Playlist Engine", build: "0.1.0" },
      })
      .then(() => {
        if (cancelled) return;
        configuredAppleToken.current = appleAuthChallenge.developerToken;
        setAppleMusicConfigured(true);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(`Apple MusicKit setup failed: ${errorMessage(e)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [appleAuthChallenge, musicKitReady]);

  useEffect(() => {
    authPollId.current += 1;
    setDeviceChallenge(null);
    setYtHeaderFallback(false);
    setAppleAuthChallenge(null);
    setAppleMusicConfigured(false);
    setAppleUserToken("");
    setActiveAuthProvider(null);
  }, [source, target]);

  useEffect(() => {
    if (sourceMode !== "account") return;
    playlistLoadId.current += 1;
    setPlaylists([]);
    setPlaylistError(null);
    setPlaylistLoading(false);
    setSelectedPlaylists(new Set());
    setPlaylistTracks({});
    setSelectedTracks({});
    void refreshSourcePlaylists({ resetSelection: true });
  }, [refreshSourcePlaylists, sourceMode]);

  useEffect(() => {
    if (!blockingAlert) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setBlockingAlert(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [blockingAlert]);

  function showAppError(error: unknown) {
    const message = errorMessage(error);
    const alert = spotifyExternalPlaylistAlert(message);
    if (alert) {
      setBlockingAlert(alert);
      setError(null);
      setNotice(null);
      return;
    }
    setError(message);
  }

  function showSpotifyCopyInstructions() {
    const alert = spotifyExternalPlaylistAlert(SPOTIFY_EXTERNAL_PLAYLIST_MESSAGE);
    if (alert) setBlockingAlert(alert);
  }

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
    setAppleAuthChallenge(null);
    setAppleMusicConfigured(false);
    setAppleUserToken("");
    setActiveAuthProvider(provider);
    if (provider === "ytmusic") setYtHeaderFallback(false);
    try {
      const challenge = await beginAuth(provider);
      if (challenge.shape === "redirect" && challenge.redirect_url) {
        window.open(challenge.redirect_url, "_blank", "noopener,noreferrer");
        const name = providers.find((item) => item.name === provider)?.display_name ?? provider;
        setNotice(`Finish ${name} auth in the new tab, then refresh accounts.`);
        return;
      }
      if (challenge.shape === "form") {
        if (provider === "ytmusic") showYtHeaderFallback(provider);
        if (provider === "applemusic") {
          const developerToken = appleMusicDeveloperToken(challenge);
          if (!developerToken) {
            throw new Error("Apple Music auth challenge is missing a developer token.");
          }
          setAppleAuthChallenge({
            developerToken,
            instructions: challenge.instructions,
          });
        }
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

  async function connectAppleMusic() {
    const musicKit = window.MusicKit;
    if (!appleAuthChallenge || !musicKit || !appleMusicConfigured) {
      setError("Apple MusicKit is still loading. Try again in a moment.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const authorization = musicKit.getInstance().authorize();
      const musicUserToken = await authorization;
      await finishAppleMusicConnection(musicUserToken);
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function connectAppleMusicToken() {
    if (!appleUserToken.trim()) {
      setError("Paste a Music User Token first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await finishAppleMusicConnection(appleUserToken.trim());
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function finishAppleMusicConnection(musicUserToken: string) {
    if (!musicUserToken) throw new Error("Apple Music did not return a Music User Token.");
    await completeAuth("applemusic", { music_user_token: musicUserToken });
    setAppleAuthChallenge(null);
    setAppleMusicConfigured(false);
    setAppleUserToken("");
    setActiveAuthProvider(null);
    setNotice("Apple Music connected.");
    await refreshAccounts();
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

  function chooseSourceMode(mode: SourceMode) {
    if (mode === sourceMode) return;
    playlistLoadId.current += 1;
    authPollId.current += 1;
    setSourceMode(mode);
    setDeviceChallenge(null);
    setYtHeaderFallback(false);
    setAppleAuthChallenge(null);
    setAppleMusicConfigured(false);
    setAppleUserToken("");
    setActiveAuthProvider(null);
    setImportPreview(null);
    setImportRequiredProvider(null);
    setPlaylists([]);
    setPlaylistTracks({});
    setSelectedTracks({});
    setSelectedPlaylists(new Set());
    setPlaylistError(null);
    setError(null);
    setNotice(null);
  }

  function invalidateImportPreview() {
    setImportPreview(null);
    setImportRequiredProvider(null);
    setPlaylists([]);
    setPlaylistTracks({});
    setSelectedTracks({});
    setSelectedPlaylists(new Set());
    setError(null);
    setNotice(null);
  }

  async function previewImportedSource() {
    if (sourceMode === "account") return;
    if (sourceMode === "url" && !importUrl.trim()) {
      setError("Paste a public playlist URL first.");
      return;
    }
    if (sourceMode === "text" && !importText.trim()) {
      setError("Paste at least one track first.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    setImportPreview(null);
    setImportRequiredProvider(null);
    setPlaylists([]);
    setPlaylistTracks({});
    setSelectedTracks({});
    setSelectedPlaylists(new Set());
    try {
      const accountId = importSourceAccount?.id;
      const body =
        sourceMode === "url"
          ? {
              kind: "url" as const,
              url: importUrl.trim(),
              source_account_id: accountId,
            }
          : {
              kind: "text" as const,
              text: importText,
              name: importName.trim() || null,
            };
      let preview: ImportPreview;
      try {
        preview = await previewImport(body);
      } catch (e: unknown) {
        const action = importConnectionAction(e);
        const connected = action
          ? accounts.find((account) => account.provider === action.provider)
          : null;
        if (sourceMode === "url" && action && connected && !accountId) {
          preview = await previewImport({
            kind: "url",
            url: importUrl.trim(),
            source_account_id: connected.id,
          });
        } else {
          if (action) {
            setImportRequiredProvider(action.provider);
            setSource(action.provider);
          }
          throw e;
        }
      }
      applyImportPreview(preview);
    } catch (e: unknown) {
      showAppError(e);
    } finally {
      setBusy(false);
    }
  }

  function applyImportPreview(preview: ImportPreview) {
    const ref = importPlaylistRef(preview);
    const keys = unmigratedTrackKeys(preview.playlist.tracks);
    setImportPreview(preview);
    setImportRequiredProvider(null);
    setPlaylists([ref]);
    setPlaylistTracks({ [ref.id]: preview.playlist.tracks });
    setSelectedTracks({ [ref.id]: new Set(keys) });
    setSelectedPlaylists(keys.length > 0 ? new Set([ref.id]) : new Set());
    setPlaylistError(null);
    setError(null);
    const issueCount = preview.issues.length;
    setNotice(
      `Previewed ${preview.track_count} tracks${
        issueCount ? ` with ${issueCount} parsing or compatibility warning${issueCount === 1 ? "" : "s"}` : ""
      }.`,
    );
  }

  async function start() {
    if (!target || !targetAccount) return;
    const playlistIds = selectedMigrationPlaylistIds;
    const tracks = Object.fromEntries(
      playlistIds
        .filter((id) => playlistTracks[id])
        .map((id) => [id, [...(selectedTracks[id] ?? new Set<string>())]]),
    );
    const body: CreateMigrationBody = {
      target_provider: target,
      target_account_id: targetAccount.id,
      selection: { playlist_ids: playlistIds, tracks },
    };
    if (sourceMode === "account") {
      if (!source || !sourceAccount) return;
      body.source_provider = source;
      body.source_account_id = sourceAccount.id;
    } else {
      if (!importPreview) return;
      body.source_import_id = importPreview.import_id;
    }
    setBusy(true);
    setError(null);
    try {
      const preflight = await preflightMigration(body);
      if (preflight.warnings.length > 0 && !confirm(warningMessage(preflight))) return;
      const job = await createMigration({ ...body, acknowledge_warnings: true });
      setJobId(job.id);
      setStatsRefreshKey((value) => value + 1);
      deselectStartedPlaylists(playlistIds);
    } catch (e: unknown) {
      if (isMigrationWarning(e) && confirm(warningMessage(e.detail))) {
        try {
          const job = await createMigration({ ...body, acknowledge_warnings: true });
          setJobId(job.id);
          setStatsRefreshKey((value) => value + 1);
          deselectStartedPlaylists(playlistIds);
        } catch (retryError: unknown) {
          showAppError(retryError);
        }
        return;
      }
      showAppError(e);
    } finally {
      setBusy(false);
    }
  }

  function togglePlaylist(id: string) {
    const playlist = playlists.find((item) => item.id === id);
    if (playlist && isSpotifyCopyRequiredPlaylist(playlist, source, sourceAccount)) {
      showSpotifyCopyInstructions();
      return;
    }
    if (selectedPlaylists.has(id)) {
      setSelectedPlaylists((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
      closePlaylistSongs(id);
      return;
    }
    const importedTracks =
      importPreview?.playlist.id === id ? importPreview.playlist.tracks : undefined;
    const loaded = playlistTracks[id] ?? importedTracks;
    if (loaded) {
      const selectableKeys = unmigratedTrackKeys(loaded);
      if (selectableKeys.length === 0) {
        if (hasUnsupportedRemainingTracks(loaded)) {
          setNotice("This playlist has no supported tracks to migrate.");
        } else {
          markPlaylistFullyMigrated(id, loaded.length);
        }
        closePlaylistSongs(id);
        return;
      }
      setSelectedPlaylists((prev) => {
        const next = new Set(prev);
        next.add(id);
        return next;
      });
      setSelectedTracks((prevTracks) => ({
        ...prevTracks,
        [id]: new Set(selectableKeys),
      }));
      if (!playlistTracks[id]) {
        setPlaylistTracks((prevTracks) => ({ ...prevTracks, [id]: loaded }));
      }
      return;
    }
    setSelectedPlaylists((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
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

  async function loadTracks(playlist: PlaylistRef, options: { forceRefresh?: boolean } = {}) {
    if (sourceMode !== "account") {
      if (!importPreview || importPreview.playlist.id !== playlist.id) return;
      const tracks = importPreview.playlist.tracks;
      const defaultSelected = unmigratedTrackKeys(tracks);
      setPlaylistTracks((prev) => ({ ...prev, [playlist.id]: tracks }));
      setSelectedTracks((prev) => ({
        ...prev,
        [playlist.id]: new Set(defaultSelected),
      }));
      return;
    }
    if (!source || !sourceAccount || !target || !targetAccount) return;
    setBusy(true);
    setError(null);
    try {
      const detail = await getPlaylist(source, sourceAccount.id, playlist.id, {
        targetProvider: target,
        targetAccountId: targetAccount.id,
        refresh: options.forceRefresh,
      });
      const defaultSelected = unmigratedTrackKeys(detail.tracks);
      if (detail.tracks.length > 0 && defaultSelected.length === 0) {
        if (hasUnsupportedRemainingTracks(detail.tracks)) {
          setPlaylistTracks((prev) => ({ ...prev, [playlist.id]: detail.tracks }));
          setSelectedTracks((prev) => ({ ...prev, [playlist.id]: new Set() }));
          setNotice("This playlist has no supported tracks to migrate.");
        } else {
          markPlaylistFullyMigrated(playlist.id, detail.tracks.length);
          closePlaylistSongs(playlist.id);
        }
        return;
      }
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
      showAppError(e);
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
      .filter(
        (track) =>
          isTrackSelectable(track) &&
          (mode === "all" || track.migration_status !== "migrated"),
      )
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

  function markPlaylistFullyMigrated(playlistId: string, migratedTrackCount: number) {
    setSelectedPlaylists((prev) => {
      const next = new Set(prev);
      next.delete(playlistId);
      return next;
    });
    setPlaylists((prev) =>
      prev.map((playlist) =>
        playlist.id === playlistId
          ? {
              ...playlist,
              migration_status: "migrated",
              migrated_track_count: playlist.track_count ?? migratedTrackCount,
              remaining_track_count: 0,
              migration_note: "Migrated",
            }
          : playlist,
      ),
    );
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
        {selectedPlaylists.has(playlist.id) && !playlistTracks[playlist.id] ? (
          <button
            className="secondary compact"
            disabled={busy}
            onClick={() => loadTracks(playlist)}
          >
            {playlist.migration_status === "delta" ? "Choose new tracks" : "Choose tracks"}
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
              {sourceMode === "account" ? (
                <button
                  className="secondary compact"
                  disabled={busy}
                  onClick={() => loadTracks(playlist, { forceRefresh: true })}
                >
                  Refresh songs
                </button>
              ) : null}
            </div>
            {playlistTracks[playlist.id].map((track) => {
              const key = trackKey(track);
              const selectable = isTrackSelectable(track);
              return (
                <label key={key} className="track-row">
                  <input
                    type="checkbox"
                    checked={selectedTracks[playlist.id]?.has(key) ?? false}
                    disabled={!selectable}
                    onChange={() => toggleTrack(playlist.id, key)}
                  />
                  <span>
                    {track.title} — {track.artist}
                    {track.explicit ? <span className="badge inline">explicit</span> : null}
                    {!selectable ? (
                      <span className="badge inline migration-blocked">
                        {track.unsupported_reason ?? "unsupported"}
                      </span>
                    ) : null}
                    {track.migration_status === "migrated" ? (
                      <span className="badge inline migration-migrated">migrated</span>
                    ) : playlist.migration_status === "delta" ? (
                      <span className="badge inline migration-delta">new</span>
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

  function renderBlockedPlaylistCard(playlist: PlaylistRef) {
    return (
      <div key={playlist.id} className="playlist-card blocked-playlist-card">
        <div className="playlist-row blocked-playlist-row">
          <span className="blocked-lock" aria-hidden="true">
            !
          </span>
          <span>{playlist.name}</span>
          <span className="badge migration-blocked">Copy first</span>
          <span className="muted">
            {playlist.track_count === null ? "" : `${playlist.track_count} tracks`}
          </span>
        </div>
        <p className="blocked-playlist-note">
          Spotify blocks track access here. Copy it into your own playlist before migrating.
        </p>
        <button className="secondary compact" disabled={busy} onClick={showSpotifyCopyInstructions}>
          Show copy instructions
        </button>
      </div>
    );
  }

  function handleTabKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const nextTab =
      event.key === "Home"
        ? "migration"
        : event.key === "End"
          ? "stats"
          : activeTab === "migration"
            ? "stats"
            : "migration";
    setActiveTab(nextTab);
    (nextTab === "migration" ? migrationTabRef : statsTabRef).current?.focus();
  }

  return (
    <div className="app">
      {blockingAlert ? (
        <BlockingAlertBanner alert={blockingAlert} onClose={() => setBlockingAlert(null)} />
      ) : null}
      <header className="app-header">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            <Music2 />
            <ArrowRight />
          </span>
          <div>
            <h1>Open Playlist Engine</h1>
            <p className="subtitle">Your music, free to move.</p>
          </div>
        </div>
        <div className="product-promise">
          <ShieldCheck aria-hidden="true" />
          <span>Local-first migration</span>
        </div>
      </header>

      <div
        className="workspace-tabs"
        role="tablist"
        aria-label="Open Playlist Engine workspace"
        onKeyDown={handleTabKeyDown}
      >
        <button
          ref={migrationTabRef}
          id="migration-tab"
          className="workspace-tab"
          type="button"
          role="tab"
          aria-label="Migration"
          aria-selected={activeTab === "migration"}
          aria-controls="migration-panel"
          tabIndex={activeTab === "migration" ? 0 : -1}
          onClick={() => setActiveTab("migration")}
        >
          <span>
            <ListMusic aria-hidden="true" />
            Migration
          </span>
          <small>Move playlists</small>
        </button>
        <button
          ref={statsTabRef}
          id="stats-tab"
          className="workspace-tab"
          type="button"
          role="tab"
          aria-label="Stats"
          aria-selected={activeTab === "stats"}
          aria-controls="stats-panel"
          tabIndex={activeTab === "stats" ? 0 : -1}
          onClick={() => setActiveTab("stats")}
        >
          <span>
            <BarChart3 aria-hidden="true" />
            Stats
          </span>
          <small>Review history</small>
        </button>
      </div>

      {activeTab === "migration" ? (
        <div
          id="migration-panel"
          className="workspace-panel"
          role="tabpanel"
          aria-labelledby="migration-tab"
        >
          {error ? <p className="warn">⚠ {error}</p> : null}
          {notice ? <p className="notice">{notice}</p> : null}

          <section
            className="migration-route"
            aria-label={`Move playlists from ${sourceLabel} to ${targetLabel}`}
          >
            <div className="route-summary">
              <div className="route-endpoint">
                <ProviderIcon
                  provider={
                    sourceMode === "account"
                      ? source
                      : importPreview?.source.provider ?? sourceMode
                  }
                />
                <span>
                  <small>Source</small>
                  <strong>{sourceLabel}</strong>
                </span>
              </div>
              <div className="route-rail" aria-hidden="true">
                <span />
                <ArrowRight />
              </div>
              <div className="route-endpoint route-endpoint-target">
                <ProviderIcon provider={target} />
                <span>
                  <small>Target</small>
                  <strong>{targetLabel}</strong>
                </span>
              </div>
            </div>
            <div className="source-mode-switch" aria-label="Source type">
              <button
                type="button"
                aria-pressed={sourceMode === "account"}
                onClick={() => chooseSourceMode("account")}
              >
                <Wifi aria-hidden="true" />
                Connected account
              </button>
              <button
                type="button"
                aria-pressed={sourceMode === "url"}
                onClick={() => chooseSourceMode("url")}
              >
                <Link2 aria-hidden="true" />
                Public URL
              </button>
              <button
                type="button"
                aria-pressed={sourceMode === "text"}
                onClick={() => chooseSourceMode("text")}
              >
                <FileText aria-hidden="true" />
                Pasted text
              </button>
            </div>
            <div className="lanes">
              {sourceMode === "account" ? (
                <ProviderPicker
                  title="From"
                  role="source"
                  providers={providers}
                  selected={source}
                  onSelect={setSource}
                />
              ) : (
                <section className="card provider-picker provider-picker-source import-source-panel">
                  <div className="provider-picker-heading">
                    <span className="step-number">1</span>
                    <div>
                      <h2>{sourceMode === "url" ? "Import a shared playlist" : "Paste a track list"}</h2>
                      <p className="muted">
                        {sourceMode === "url"
                          ? "Only supported playlist hosts are accepted."
                          : "Use one track per line, artist - title, or tabular columns."}
                      </p>
                    </div>
                  </div>
                  {sourceMode === "url" ? (
                    <div className="import-fields">
                      <label htmlFor="publicPlaylistUrl">Public playlist URL</label>
                      <input
                        id="publicPlaylistUrl"
                        type="url"
                        value={importUrl}
                        onChange={(event) => {
                          setImportUrl(event.target.value);
                          invalidateImportPreview();
                        }}
                        placeholder="https://music.youtube.com/playlist?list=..."
                      />
                      <p className="muted import-security-note">
                        HTTPS only. Redirects, response size, and public-network destinations are
                        strictly limited.
                      </p>
                    </div>
                  ) : (
                    <div className="import-fields">
                      <label htmlFor="importPlaylistName">Playlist name</label>
                      <input
                        id="importPlaylistName"
                        value={importName}
                        onChange={(event) => {
                          setImportName(event.target.value);
                          invalidateImportPreview();
                        }}
                        placeholder="Imported track list"
                      />
                      <label htmlFor="importTrackText">Tracks</label>
                      <textarea
                        id="importTrackText"
                        value={importText}
                        onChange={(event) => {
                          setImportText(event.target.value);
                          invalidateImportPreview();
                        }}
                        placeholder={"# Comments start with #\nBjörk - Jóga\nMassive Attack\tTeardrop\tMezzanine"}
                      />
                    </div>
                  )}
                  <button
                    className="primary import-preview-button"
                    disabled={
                      busy ||
                      (sourceMode === "url" ? !importUrl.trim() : !importText.trim())
                    }
                    onClick={previewImportedSource}
                  >
                    {busy ? "Previewing…" : importPreview ? "Refresh preview" : "Preview import"}
                  </button>
                  {importPreview ? (
                    <div className="import-preview-summary" aria-live="polite">
                      <strong>{importPreview.playlist.name}</strong>
                      <span>
                        {importPreview.track_count} tracks from {importPreview.source.label}
                      </span>
                      {importPreview.playlist.owner_id ? (
                        <span>Owner: {importPreview.playlist.owner_id}</span>
                      ) : null}
                    </div>
                  ) : null}
                </section>
              )}
              <ProviderPicker
                title="To"
                role="target"
                providers={providers}
                selected={target}
                onSelect={setTarget}
              />
            </div>
          </section>

          <section className="card flow">
            <div className="section-heading">
              <div className="section-title">
                <span className="section-icon" aria-hidden="true">
                  <Wifi />
                </span>
                <div>
                  <h2>Connect accounts</h2>
                  <p className="muted">
                    {sourceMode === "account"
                      ? "Authorize both services before choosing playlists."
                      : "Only the target is always required. Some playlist URLs also need source access."}
                  </p>
                </div>
              </div>
            </div>
            <div className="account-grid">
              {sourceMode === "account" ? (
                <AccountPanel
                  label="Source"
                  provider={source}
                  account={sourceAccount}
                  busy={busy}
                  onConnect={connect}
                  onTest={testConnection}
                />
              ) : importRequiredProvider ? (
                <AccountPanel
                  label="Source access required"
                  provider={importRequiredProvider}
                  account={importSourceAccount}
                  busy={busy}
                  onConnect={connect}
                  onTest={testConnection}
                />
              ) : (
                <div className="account-panel import-access-status">
                  <p className="eyebrow">Source access</p>
                  <strong>{importPreview ? "Preview ready" : "Not required yet"}</strong>
                  <p className="muted">
                    Public APIs are used where available. Private or restricted links will ask
                    for the matching provider connection.
                  </p>
                </div>
              )}
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
        {activeAuthProvider === "applemusic" && appleAuthChallenge ? (
          <div className="form-block apple-music-guide">
            <div className="guide-heading">
              <div>
                <p className="eyebrow">Official MusicKit authorization</p>
                <h3>Connect your Apple Music library</h3>
                <p className="muted">
                  Apple uses a signed developer token and a browser-issued Music User Token
                  instead of OAuth client credentials.
                </p>
              </div>
              <a
                className="button-link"
                href="https://music.apple.com"
                target="_blank"
                rel="noreferrer"
              >
                Open Apple Music
              </a>
            </div>
            <div className="apple-auth-status" aria-live="polite">
              <span className={`header-check ${musicKitReady ? "ok" : ""}`}>
                {musicKitReady ? "MusicKit loaded" : "Loading MusicKit"}
              </span>
              <span className={`header-check ${appleMusicConfigured ? "ok" : ""}`}>
                {appleMusicConfigured ? "Developer token ready" : "Preparing developer token"}
              </span>
            </div>
            <p className="muted">
              Apple will open a sign-in window and ask permission to access your music library.
              An active Apple Music subscription is required.
            </p>
            <button
              className="primary"
              disabled={busy || !appleMusicConfigured}
              onClick={connectAppleMusic}
            >
              {busy ? "Connecting..." : "Authorize with Apple Music"}
            </button>
            <details className="apple-token-fallback">
              <summary>Advanced: use an existing Music User Token</summary>
              <p className="muted">
                Use this only for local testing. Music User Tokens grant access to your library
                and must stay private.
              </p>
              <label htmlFor="appleUserToken">Music User Token</label>
              <textarea
                id="appleUserToken"
                value={appleUserToken}
                onChange={(event) => setAppleUserToken(event.target.value)}
                placeholder="Paste a Music User Token"
              />
              <button
                className="secondary"
                disabled={busy || !appleUserToken.trim()}
                onClick={connectAppleMusicToken}
              >
                Connect with token
              </button>
            </details>
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
              <RefreshCw aria-hidden="true" />
              Refresh accounts
            </button>
          </section>

          {(sourceMode === "account" && source && sourceAccount) ||
          (sourceMode !== "account" && importPreview) ? (
            <section className="card flow">
          <div className="section-heading">
            <div className="section-title">
              <span className="section-icon" aria-hidden="true">
                <ListMusic />
              </span>
              <div>
              <h2>Pick playlists</h2>
              <p className="muted">
                {selectedMigrationPlaylistIds.length} of {availablePlaylists.length} migratable selected
              </p>
              </div>
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
                <Check aria-hidden="true" />
                Select all
              </button>
              <button
                className="secondary compact"
                disabled={busy || selectedPlaylists.size === 0}
                onClick={deselectAllPlaylists}
              >
                Deselect all
              </button>
              {sourceMode === "account" ? (
                <button
                  className="secondary compact"
                  disabled={busy || playlistLoading}
                  onClick={() =>
                    void refreshSourcePlaylists({ resetSelection: true, forceRefresh: true })
                  }
                >
                  <RefreshCw aria-hidden="true" />
                  {playlistLoading ? "Refreshing…" : "Refresh playlists"}
                </button>
              ) : null}
            </div>
          </div>
          {sourceMode === "account" ? (
            <p className="cache-guidance">
              Playlist lists are cached to avoid Spotify rate limits. Use Refresh playlists only
              for new playlists or changed snapshots; songs are cached per playlist until Spotify
              reports a new snapshot.
            </p>
          ) : importPreview ? (
            <div className="import-preview-details">
              <p className="cache-guidance">
                Snapshot saved from {importPreview.source.locator}. The migration worker uses this
                exact preview, even if the source changes later.
              </p>
              {importPreview.issues.length > 0 ? (
                <div className="import-issues" role="status">
                  <strong>
                    {importPreview.issues.length} parsing or compatibility warning
                    {importPreview.issues.length === 1 ? "" : "s"}
                  </strong>
                  <ul>
                    {importPreview.issues.slice(0, 20).map((issue, index) => (
                      <li key={`${issue.line ?? "item"}-${issue.code}-${index}`}>
                        {issue.line ? `Line ${issue.line}: ` : ""}
                        {issue.message}
                      </li>
                    ))}
                  </ul>
                  {importPreview.issues.length > 20 ? (
                    <p className="muted">
                      {importPreview.issues.length - 20} more warnings are not shown.
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="migration-top-stack">
            <div className="migration-action-bar">
              <div>
                <p className="action-label">
                  <CircleGauge aria-hidden="true" />
                  Ready to migrate
                </p>
                <p className="muted">
                  {selectedMigrationPlaylistIds.length} playlist
                  {selectedMigrationPlaylistIds.length === 1 ? "" : "s"} selected for migration
                </p>
                {selectedMigrationPlaylists.length >= 2 ? (
                  <ul className="selected-playlist-names" aria-label="Selected playlists">
                    {selectedMigrationPlaylists.map((playlist) => (
                      <li key={playlist.id}>{playlist.name}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
              <button className="primary" disabled={startDisabled} onClick={() => start()}>
                <Play aria-hidden="true" />
                {busy ? "Starting…" : "Start migration"}
              </button>
            </div>
            {jobId ? (
              <div className="migration-progress-slot">
                <ProgressBoard
                  className="progress-popover"
                  jobId={jobId}
                  onMigrationChanged={handleMigrationChanged}
                  onReconnectProvider={connect}
                />
              </div>
            ) : null}
          </div>
          {playlistError && playlistErrorTitle ? (
            <div className="playlist-error-panel error-guidance" role="alert">
              <div>
                <strong>{playlistErrorTitle}</strong>
                <p>{playlistErrorHelp(playlistError)}</p>
              </div>
              <button
                className="secondary compact"
                disabled={playlistLoading}
                onClick={() => void refreshSourcePlaylists()}
              >
                {playlistLoading ? "Checking…" : "Retry now"}
              </button>
            </div>
          ) : null}
          {migratedPlaylists.length > 0 ? (
            <div className="migrated-playlists-panel">
              <button
                className="migrated-playlists-toggle"
                type="button"
                aria-expanded={showMigratedPlaylistDetails}
                onClick={() => setShowMigratedPlaylists((open) => !open)}
              >
                <span>Migrated playlists</span>
                <span className="muted">
                  {migratedPlaylists.length} migrated or partially migrated
                </span>
                <span aria-hidden="true">{showMigratedPlaylistDetails ? "Hide" : "Show"}</span>
              </button>
              {showMigratedPlaylistDetails ? (
                <div className="playlist-list migrated-playlist-list">
                  {migratedPlaylists.map(renderPlaylistCard)}
                </div>
              ) : null}
            </div>
          ) : null}
          {blockedSpotifyPlaylists.length > 0 ? (
            <div className="blocked-playlists-panel">
              <button
                className="blocked-playlists-toggle"
                type="button"
                aria-expanded={showBlockedPlaylistDetails}
                onClick={() => setShowBlockedSpotifyPlaylists((open) => !open)}
              >
                <span>Spotify playlists to copy first</span>
                <span className="muted">
                  {blockedSpotifyPlaylists.length} owned by someone else
                </span>
                <span aria-hidden="true">{showBlockedPlaylistDetails ? "Hide" : "Show"}</span>
              </button>
              {showBlockedPlaylistDetails ? (
                <div className="playlist-list blocked-playlist-list">
                  {blockedSpotifyPlaylists.map(renderBlockedPlaylistCard)}
                </div>
              ) : null}
            </div>
          ) : null}
          {playlistLoading && playlists.length === 0 ? (
            <p className="empty-guidance">Loading playlists…</p>
          ) : playlistError && playlists.length === 0 ? (
            <p className="empty-guidance error-guidance">
              {playlistErrorHelp(playlistError)}
            </p>
          ) : playlists.length === 0 ? (
            <p className="muted">No playlists found yet.</p>
          ) : migrationCandidatePlaylists.length === 0 && blockedSpotifyPlaylists.length > 0 ? (
            <p className="empty-guidance">
              No Spotify playlists can be migrated directly. Copy the playlists above into ones
              you own, then refresh.
            </p>
          ) : migrationCandidatePlaylists.length === 0 && migratedPlaylists.length > 0 ? (
            <p className="empty-guidance">
              No new playlist work right now. Migrated and partial playlists are shown above.
            </p>
          ) : migrationCandidatePlaylists.length === 0 ? (
            <p className="muted">No migratable playlists left.</p>
          ) : (
            <div className="playlist-list">
              {migrationCandidatePlaylists.map(renderPlaylistCard)}
            </div>
          )}
            </section>
          ) : null}
        </div>
      ) : (
        <div
          id="stats-panel"
          className="workspace-panel"
          role="tabpanel"
          aria-labelledby="stats-tab"
        >
          <MigrationStatsPanel providers={providers} refreshKey={statsRefreshKey} />
        </div>
      )}
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

function unmigratedTrackKeys(tracks: Track[]): string[] {
  return tracks.filter(isTrackSelectable).map(trackKey);
}

function isTrackSelectable(track: Track): boolean {
  return (
    track.migration_status !== "migrated" &&
    track.media_type === "track" &&
    !track.is_local &&
    !track.unsupported_reason
  );
}

function hasUnsupportedRemainingTracks(tracks: Track[]): boolean {
  return tracks.some(
    (track) => track.migration_status !== "migrated" && !isTrackSelectable(track),
  );
}

function trackKey(track: Track): string {
  return track.source_item_id ?? track.id ?? String(track.position ?? track.title);
}

function importPlaylistRef(preview: ImportPreview): PlaylistRef {
  return {
    id: preview.playlist.id ?? preview.import_id,
    name: preview.playlist.name,
    track_count: preview.track_count,
    owner_id: preview.playlist.owner_id,
    collaborative: null,
    snapshot_id: null,
    tracks_href: null,
    migration_status: null,
    migrated_track_count: 0,
    remaining_track_count: preview.track_count,
    migration_note: preview.unsupported_count
      ? `${preview.unsupported_count} unsupported`
      : null,
    kind: preview.playlist.kind,
  };
}

function isMigrationWarning(error: unknown): error is ApiError & { detail: MigrationWarningsView } {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  if (!error.detail || typeof error.detail !== "object") return false;
  const detail = error.detail as Partial<MigrationWarningsView>;
  return detail.code === "migration_warnings" && Array.isArray(detail.warnings);
}

function warningMessage(detail: MigrationWarningsView): string {
  return [
    detail.message,
    "",
    ...detail.warnings.map((warning) => `- ${warning.message}`),
    "",
    "Continue anyway?",
  ].join("\n");
}

interface ImportConnectionAction {
  code: "source_connection_required";
  message: string;
  provider: string;
  action: "connect_source";
}

function importConnectionAction(error: unknown): ImportConnectionAction | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  if (!error.detail || typeof error.detail !== "object") return null;
  const detail = error.detail as Partial<ImportConnectionAction>;
  if (
    detail.code !== "source_connection_required" ||
    detail.action !== "connect_source" ||
    typeof detail.message !== "string" ||
    typeof detail.provider !== "string"
  ) {
    return null;
  }
  return detail as ImportConnectionAction;
}

interface DeviceChallenge {
  provider: string;
  userCode: string;
  verificationUrl: string;
  state: string;
  pollIntervalS: number;
}

type WorkspaceTab = "migration" | "stats";
type SourceMode = "account" | "url" | "text";

interface AppleMusicChallenge {
  developerToken: string;
  instructions: string | null;
}

interface BlockingAlert {
  title: string;
  message: string;
  action: string;
}

const SPOTIFY_EXTERNAL_PLAYLIST_PREFIX =
  "Spotify does not allow this app to read tracks from playlists you do not own";
const SPOTIFY_EXTERNAL_PLAYLIST_MESSAGE =
  "Spotify does not allow this app to read tracks from playlists you do not own or collaborate on. In Spotify, use 'Add to other playlist' to copy it into a playlist you own, then migrate that copy. Delta migration is not available for the original external playlist because Spotify blocks track access.";

function spotifyExternalPlaylistAlert(message: string): BlockingAlert | null {
  if (!message.includes(SPOTIFY_EXTERNAL_PLAYLIST_PREFIX)) return null;
  return {
    title: "Spotify blocks this playlist",
    message,
    action: "Copy it in Spotify with Add to other playlist, then migrate your copy.",
  };
}

function spotifyRateLimitAlert(error: unknown, message: string): BlockingAlert | null {
  if (!isRateLimitError(error, message)) return null;
  return {
    title: "Spotify rate limit",
    message: playlistErrorHelp(message),
    action: "Wait for Spotify's retry window, then refresh playlists.",
  };
}

function playlistErrorHeading(message: string): string {
  return isRateLimitMessage(message) ? "Spotify rate limit" : "Could not load playlists";
}

function playlistErrorHelp(message: string): string {
  const retryAfter = retryAfterSeconds(message);
  if (isRateLimitMessage(message)) {
    const wait = retryAfter === null ? null : formatWait(retryAfter);
    return wait
      ? `Spotify asked us to retry after ${wait}. I stopped waiting so the app does not hang.`
      : "Spotify is rate limiting playlist requests. Wait a bit, then refresh playlists.";
  }
  return message;
}

function isRateLimitError(error: unknown, message: string): boolean {
  return (
    (error instanceof ApiError && (error.status === 420 || error.status === 429)) ||
    isRateLimitMessage(message)
  );
}

function isRateLimitMessage(message: string): boolean {
  return message.toLowerCase().includes("rate limited");
}

function retryAfterSeconds(message: string): number | null {
  const match = message.match(/retry after ([0-9.]+) seconds/i);
  if (!match) return null;
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : null;
}

function formatWait(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const restSeconds = rounded % 60;
  const parts = [];
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  if (!hours && restSeconds) parts.push(`${restSeconds}s`);
  return parts.join(" ") || "0s";
}

function isSpotifyCopyRequiredPlaylist(
  playlist: PlaylistRef,
  source: string | null,
  sourceAccount: AccountView | null,
): boolean {
  return (
    source === "spotify" &&
    playlist.collaborative !== true &&
    Boolean(playlist.owner_id) &&
    Boolean(sourceAccount?.provider_user_id) &&
    playlist.owner_id !== sourceAccount?.provider_user_id
  );
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

interface BlockingAlertBannerProps {
  alert: BlockingAlert;
  onClose: () => void;
}

function BlockingAlertBanner({ alert, onClose }: BlockingAlertBannerProps) {
  return (
    <div className="blocking-alert-layer" role="presentation">
      <section
        className="blocking-alert"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="blocking-alert-title"
        aria-describedby="blocking-alert-message"
      >
        <span className="blocking-alert-icon" aria-hidden="true">
          ⚠
        </span>
        <div className="blocking-alert-copy">
          <p className="eyebrow">Spotify access limit</p>
          <h2 id="blocking-alert-title">{alert.title}</h2>
          <p id="blocking-alert-message">{alert.message}</p>
          <p className="blocking-alert-action">{alert.action}</p>
        </div>
        <button className="blocking-alert-close" type="button" onClick={onClose} autoFocus>
          Close
        </button>
      </section>
    </div>
  );
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
    <div className="account-panel">
      <div className="account-heading">
        <ProviderIcon provider={provider} />
        <div>
          <span className="account-role">{label}</span>
          <h3>{provider ? providerLabel(provider) : "No provider selected"}</h3>
        </div>
      </div>
      {!provider ? <p className="muted">Pick a provider first.</p> : null}
      {provider && account ? (
        <>
          <p className="connected">
            <Check aria-hidden="true" />
            Connected as {account.display_name ?? account.provider_user_id ?? account.id}
          </p>
          <button className="secondary compact" disabled={busy} onClick={() => onTest(account)}>
            <Wifi aria-hidden="true" />
            Test connection
          </button>
          <button className="secondary compact" disabled={busy} onClick={() => onConnect(provider)}>
            <RotateCcw aria-hidden="true" />
            Reconnect
          </button>
        </>
      ) : null}
      {provider && !account ? (
        <button className="secondary" disabled={busy} onClick={() => onConnect(provider)}>
          <ProviderIcon provider={provider} className="provider-icon-inline" />
          Connect {providerLabel(provider)}
        </button>
      ) : null}
    </div>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function appleMusicDeveloperToken(challenge: AuthChallenge): string | null {
  const field = challenge.form_schema?.music_user_token;
  if (!field || typeof field !== "object") return null;
  const token = (field as Record<string, unknown>).developer_token;
  return typeof token === "string" && token ? token : null;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}
