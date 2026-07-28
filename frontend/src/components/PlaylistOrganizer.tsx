import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ListFilter,
  Music,
  RefreshCw,
  RotateCcw,
  ScanSearch,
  Search,
  ShieldCheck,
  Trash2,
  Unlink,
  X,
} from "lucide-react";
import {
  analyzeOrganizerDuplicates,
  createOrganizerJob,
  getOrganizerJob,
  getOrganizerPlaylists,
  getPlaylist,
  listOrganizerJobs,
  preflightOrganizer,
  retryOrganizerJob,
} from "../api/client";
import type {
  AccountView,
  DuplicateCandidateView,
  OrganizerIntent,
  OrganizerJobView,
  OrganizerPlaylistView,
  OrganizerPreflightView,
  OrganizerRequestBody,
  PlaylistRef,
  ProviderView,
  Track,
} from "../api/types";
import { providerLabel } from "../utils/providers";
import ProviderIcon from "./ProviderIcon";

interface Props {
  providers: ProviderView[];
  accounts: AccountView[];
  authBusy: boolean;
  onConnect: (provider: string) => void | Promise<void>;
}

type OwnershipFilter = "all" | "owned" | "collaborative" | "followed" | "unknown";
type SortMode = "name" | "owner" | "tracks" | "updated";

const TERMINAL_JOB_STATUSES = new Set(["done", "partial", "failed"]);

export default function PlaylistOrganizer({
  providers,
  accounts,
  authBusy,
  onConnect,
}: Props) {
  const [provider, setProvider] = useState<string | null>(null);
  const [rows, setRows] = useState<OrganizerPlaylistView[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sortMode, setSortMode] = useState<SortMode>("name");
  const [ownershipFilter, setOwnershipFilter] = useState<OwnershipFilter>("all");
  const [actionMode, setActionMode] = useState<OrganizerIntent>("remove");
  const [selectedPlaylists, setSelectedPlaylists] = useState<Set<string>>(new Set());
  const [tracksByPlaylist, setTracksByPlaylist] = useState<Record<string, Track[]>>({});
  const [selectedTracks, setSelectedTracks] = useState<Record<string, Set<string>>>({});
  const [expandedPlaylist, setExpandedPlaylist] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<OrganizerPreflightView | null>(null);
  const [pendingBody, setPendingBody] = useState<OrganizerRequestBody | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [duplicates, setDuplicates] = useState<DuplicateCandidateView[]>([]);
  const [duplicateFocus, setDuplicateFocus] = useState<Set<string> | null>(null);
  const [duplicateLoading, setDuplicateLoading] = useState(false);
  const [jobs, setJobs] = useState<OrganizerJobView[]>([]);
  const [activeJob, setActiveJob] = useState<OrganizerJobView | null>(null);

  const account = accounts.find((candidate) => candidate.provider === provider) ?? null;
  const selectedTrackCount = Object.values(selectedTracks).reduce(
    (total, selected) => total + selected.size,
    0,
  );
  const selectionCount =
    actionMode === "remove_tracks" ? selectedTrackCount : selectedPlaylists.size;

  useEffect(() => {
    if (provider || providers.length === 0) return;
    const connected = providers.find((candidate) =>
      accounts.some((account) => account.provider === candidate.name),
    );
    setProvider(connected?.name ?? providers[0]?.name ?? null);
  }, [accounts, provider, providers]);

  const loadJobs = useCallback(async () => {
    try {
      const nextJobs = await listOrganizerJobs();
      setJobs(nextJobs);
      setActiveJob((current) => {
        if (current || !provider || !account) return current;
        return (
          nextJobs.find(
            (job) => job.provider === provider && job.account_id === account.id,
          ) ?? null
        );
      });
    } catch (loadError: unknown) {
      setError(errorMessage(loadError));
    }
  }, [account?.id, provider]);

  const loadPlaylists = useCallback(
    async (refresh = false) => {
      if (!provider || !account) {
        setRows([]);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        setRows(await getOrganizerPlaylists(provider, account.id, refresh));
      } catch (loadError: unknown) {
        setError(errorMessage(loadError));
      } finally {
        setLoading(false);
      }
    },
    [account?.id, provider],
  );

  useEffect(() => {
    setRows([]);
    setSelectedPlaylists(new Set());
    setTracksByPlaylist({});
    setSelectedTracks({});
    setExpandedPlaylist(null);
    setDuplicates([]);
    setDuplicateFocus(null);
    setPreflight(null);
    setPendingBody(null);
    setConfirmation("");
    setActiveJob(null);
    if (account) void loadPlaylists();
  }, [account?.id, loadPlaylists, provider]);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  useEffect(() => {
    if (!activeJob || TERMINAL_JOB_STATUSES.has(activeJob.status)) return;
    const jobId = activeJob.id;
    let cancelled = false;
    let timer: number | undefined;
    async function poll() {
      try {
        const nextJob = await getOrganizerJob(jobId);
        if (cancelled) return;
        setActiveJob(nextJob);
        setJobs((current) => replaceJob(current, nextJob));
        if (TERMINAL_JOB_STATUSES.has(nextJob.status)) {
          await loadPlaylists(true);
          await loadJobs();
          return;
        }
        timer = window.setTimeout(poll, 1500);
      } catch (pollError: unknown) {
        if (!cancelled) setError(errorMessage(pollError));
      }
    }
    timer = window.setTimeout(poll, 700);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [activeJob?.id, activeJob?.status, loadJobs, loadPlaylists]);

  useEffect(() => {
    if (!preflight) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) closePreflight();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [busy, preflight]);

  const visibleRows = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return [...rows]
      .filter((row) => !duplicateFocus || duplicateFocus.has(row.playlist.id))
      .filter((row) => ownershipFilter === "all" || row.ownership === ownershipFilter)
      .filter((row) => {
        if (!normalizedQuery) return true;
        return [row.playlist.name, row.playlist.owner_name ?? "", row.playlist.owner_id ?? ""]
          .join(" ")
          .toLowerCase()
          .includes(normalizedQuery);
      })
      .sort((left, right) => compareRows(left, right, sortMode));
  }, [duplicateFocus, ownershipFilter, query, rows, sortMode]);

  const eligibleVisibleRows = visibleRows.filter((row) =>
    row.available_intents.includes(actionMode),
  );

  function changeActionMode(nextMode: OrganizerIntent) {
    setActionMode(nextMode);
    setSelectedPlaylists(new Set());
    setSelectedTracks({});
    setExpandedPlaylist(null);
    setNotice(null);
  }

  function togglePlaylist(row: OrganizerPlaylistView) {
    if (!row.available_intents.includes(actionMode)) return;
    if (actionMode === "remove_tracks") {
      if (selectedTracks[row.playlist.id]?.size) {
        setSelectedTracks((current) => ({ ...current, [row.playlist.id]: new Set() }));
        return;
      }
      void loadPlaylistTracks(row.playlist);
      return;
    }
    setSelectedPlaylists((current) => {
      const next = new Set(current);
      if (next.has(row.playlist.id)) next.delete(row.playlist.id);
      else next.add(row.playlist.id);
      return next;
    });
  }

  function selectAllVisible() {
    if (actionMode === "remove_tracks") return;
    setSelectedPlaylists(new Set(eligibleVisibleRows.map((row) => row.playlist.id)));
  }

  function clearSelection() {
    setSelectedPlaylists(new Set());
    setSelectedTracks({});
  }

  async function loadPlaylistTracks(playlist: PlaylistRef) {
    if (!provider || !account) return;
    setBusy(true);
    setError(null);
    try {
      const detail = await getPlaylist(provider, account.id, playlist.id, { refresh: true });
      setTracksByPlaylist((current) => ({ ...current, [playlist.id]: detail.tracks }));
      setSelectedTracks((current) => ({
        ...current,
        [playlist.id]: current[playlist.id] ?? new Set<string>(),
      }));
      setExpandedPlaylist(playlist.id);
    } catch (loadError: unknown) {
      setError(errorMessage(loadError));
    } finally {
      setBusy(false);
    }
  }

  function toggleTrack(playlistId: string, track: Track) {
    const key = organizerTrackKey(track);
    setSelectedTracks((current) => {
      const next = new Set(current[playlistId] ?? []);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return { ...current, [playlistId]: next };
    });
  }

  async function previewAction() {
    if (!provider || !account) return;
    const body = buildRequestBody(
      provider,
      account.id,
      actionMode,
      selectedPlaylists,
      selectedTracks,
      tracksByPlaylist,
    );
    setBusy(true);
    setError(null);
    try {
      const nextPreflight = await preflightOrganizer(body);
      setPreflight(nextPreflight);
      setPendingBody(body);
      setConfirmation("");
    } catch (previewError: unknown) {
      setError(errorMessage(previewError));
    } finally {
      setBusy(false);
    }
  }

  async function executeAction() {
    if (!pendingBody || !preflight) return;
    setBusy(true);
    setError(null);
    try {
      const job = await createOrganizerJob({
        ...pendingBody,
        confirmation: preflight.confirmation_required ? confirmation : null,
      });
      setActiveJob(job);
      setJobs((current) => replaceJob(current, job));
      setNotice("Organizer job started. The ledger will keep every playlist result.");
      clearSelection();
      closePreflight();
    } catch (executeError: unknown) {
      setError(errorMessage(executeError));
    } finally {
      setBusy(false);
    }
  }

  function closePreflight() {
    setPreflight(null);
    setPendingBody(null);
    setConfirmation("");
  }

  async function scanDuplicates() {
    if (!provider || !account) return;
    setDuplicateLoading(true);
    setError(null);
    try {
      const candidates = await analyzeOrganizerDuplicates(provider, account.id);
      setDuplicates(candidates);
      setDuplicateFocus(null);
      setNotice(
        candidates.length
          ? `${candidates.length} likely duplicate pair${candidates.length === 1 ? "" : "s"} ready for review.`
          : "No likely duplicates met the name, owner, and track-overlap threshold.",
      );
    } catch (scanError: unknown) {
      setError(errorMessage(scanError));
    } finally {
      setDuplicateLoading(false);
    }
  }

  async function retryFailed(job: OrganizerJobView) {
    setBusy(true);
    setError(null);
    try {
      const retried = await retryOrganizerJob(job.id);
      setActiveJob(retried);
      setJobs((current) => replaceJob(current, retried));
      setNotice("Retry queued. Successful playlist actions will not run again.");
    } catch (retryError: unknown) {
      setError(errorMessage(retryError));
    } finally {
      setBusy(false);
    }
  }

  const activeProvider = providers.find((candidate) => candidate.name === provider) ?? null;
  const destructiveMode = actionMode !== "remove";
  const canPreview = Boolean(provider && account && selectionCount > 0 && !busy);

  return (
    <section className="organizer-workspace">
      {error ? <p className="warn organizer-message">{error}</p> : null}
      {notice ? <p className="notice organizer-message">{notice}</p> : null}

      <div className="organizer-hero">
        <div>
          <p className="eyebrow">Playlist maintenance</p>
          <h2>Clear the clutter without losing the plot.</h2>
          <p className="muted">
            Safe removal stays separate from permanent deletion. Every playlist gets its own
            result and retry state.
          </p>
        </div>
        <div className="organizer-safety-mark" aria-label="Safety defaults">
          <ShieldCheck aria-hidden="true" />
          <span>
            <strong>Safe by default</strong>
            <small>Nothing destructive runs without typed confirmation.</small>
          </span>
        </div>
      </div>

      <section className="card organizer-provider-card">
        <div className="section-heading">
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <Music />
            </span>
            <div>
              <h2>Choose a library</h2>
              <p className="muted">Organizer capabilities are checked per provider and playlist.</p>
            </div>
          </div>
          {account ? (
            <span className="connected">
              <Check aria-hidden="true" />
              {account.display_name ?? account.provider_user_id ?? "Connected"}
            </span>
          ) : null}
        </div>
        <div className="organizer-provider-strip">
          {providers.map((candidate) => (
            <button
              key={candidate.name}
              className="organizer-provider"
              type="button"
              aria-pressed={candidate.name === provider}
              onClick={() => setProvider(candidate.name)}
            >
              <ProviderIcon provider={candidate.name} />
              <span>
                <strong>{candidate.display_name}</strong>
                <small>{providerCapabilitySummary(candidate)}</small>
              </span>
            </button>
          ))}
        </div>
        {provider && !account ? (
          <div className="organizer-connect-callout">
            <p>
              Connect {activeProvider?.display_name ?? providerLabel(provider)} to load its
              playlists and verify ownership.
            </p>
            <button
              className="secondary"
              type="button"
              disabled={authBusy}
              onClick={() => void onConnect(provider)}
            >
              Connect {activeProvider?.display_name ?? providerLabel(provider)}
            </button>
          </div>
        ) : null}
      </section>

      {provider && account ? (
        <div className="organizer-layout">
          <div className="organizer-main">
            <section className="card organizer-controls">
              <div className="organizer-mode-switch" role="group" aria-label="Organizer action">
                <ModeButton
                  active={actionMode === "remove"}
                  icon={<Unlink />}
                  label="Remove from library"
                  detail="Safest available operation"
                  onClick={() => changeActionMode("remove")}
                />
                <ModeButton
                  active={actionMode === "delete"}
                  icon={<Trash2 />}
                  label="Delete permanently"
                  detail="Owned playlists only"
                  danger
                  onClick={() => changeActionMode("delete")}
                />
                <ModeButton
                  active={actionMode === "remove_tracks"}
                  icon={<Music />}
                  label="Remove songs"
                  detail="Exact selected entries"
                  danger
                  onClick={() => changeActionMode("remove_tracks")}
                />
              </div>

              <div className="organizer-filter-row">
                <label className="organizer-search">
                  <Search aria-hidden="true" />
                  <span className="sr-only">Search playlists</span>
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Search name or owner"
                  />
                </label>
                <label>
                  <span className="sr-only">Ownership filter</span>
                  <select
                    value={ownershipFilter}
                    onChange={(event) =>
                      setOwnershipFilter(event.target.value as OwnershipFilter)
                    }
                  >
                    <option value="all">All ownership</option>
                    <option value="owned">Owned</option>
                    <option value="collaborative">Collaborative</option>
                    <option value="followed">Followed</option>
                    <option value="unknown">Ownership unknown</option>
                  </select>
                </label>
                <label>
                  <span className="sr-only">Sort playlists</span>
                  <select
                    value={sortMode}
                    onChange={(event) => setSortMode(event.target.value as SortMode)}
                  >
                    <option value="name">Sort by name</option>
                    <option value="owner">Sort by owner</option>
                    <option value="tracks">Sort by track count</option>
                    <option value="updated">Sort by last update</option>
                  </select>
                </label>
                <button
                  className="secondary compact"
                  type="button"
                  disabled={loading}
                  onClick={() => void loadPlaylists(true)}
                >
                  <RefreshCw aria-hidden="true" />
                  {loading ? "Refreshing…" : "Refresh"}
                </button>
              </div>

              <div className="organizer-list-toolbar">
                <span>
                  <ListFilter aria-hidden="true" />
                  {visibleRows.length} shown · {eligibleVisibleRows.length} support this action
                </span>
                <div className="toolbar">
                  {actionMode !== "remove_tracks" ? (
                    <button
                      className="secondary compact"
                      type="button"
                      disabled={eligibleVisibleRows.length === 0}
                      onClick={selectAllVisible}
                    >
                      Select eligible
                    </button>
                  ) : null}
                  <button
                    className="secondary compact"
                    type="button"
                    disabled={selectionCount === 0}
                    onClick={clearSelection}
                  >
                    Clear
                  </button>
                </div>
              </div>

              {duplicateFocus ? (
                <div className="duplicate-focus-banner">
                  <span>Reviewing one duplicate pair. No playlist was selected automatically.</span>
                  <button type="button" onClick={() => setDuplicateFocus(null)}>
                    Show all playlists
                  </button>
                </div>
              ) : null}

              <div className="organizer-playlist-list">
                {loading && rows.length === 0 ? (
                  <p className="empty-guidance">Loading playlists…</p>
                ) : visibleRows.length === 0 ? (
                  <p className="empty-guidance">
                    No playlists match these filters. Change the search or ownership filter.
                  </p>
                ) : (
                  visibleRows.map((row) => {
                    const eligible = row.available_intents.includes(actionMode);
                    const trackSelection = selectedTracks[row.playlist.id]?.size ?? 0;
                    const selected =
                      actionMode === "remove_tracks"
                        ? trackSelection > 0
                        : selectedPlaylists.has(row.playlist.id);
                    const expanded = expandedPlaylist === row.playlist.id;
                    return (
                      <article
                        key={row.playlist.id}
                        className={[
                          "organizer-playlist",
                          selected ? "is-selected" : "",
                          !eligible ? "is-disabled" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")}
                      >
                        <div className="organizer-playlist-row">
                          <input
                            type="checkbox"
                            aria-label={`Select ${row.playlist.name}`}
                            checked={selected}
                            disabled={!eligible || busy}
                            onChange={() => togglePlaylist(row)}
                          />
                          <div className="organizer-playlist-copy">
                            <div>
                              <strong>{row.playlist.name}</strong>
                              <span className={`ownership-chip ownership-${row.ownership}`}>
                                {ownershipLabel(row.ownership)}
                              </span>
                              {row.playlist.collaborative ? (
                                <span className="badge">collaborative</span>
                              ) : null}
                            </div>
                            <p>
                              {row.playlist.owner_name ??
                                row.playlist.owner_id ??
                                "Owner unavailable"}
                            </p>
                            {row.notes.map((note) => (
                              <small key={note}>{note}</small>
                            ))}
                          </div>
                          <div className="organizer-playlist-meta">
                            <strong>
                              {row.playlist.track_count === null
                                ? "—"
                                : row.playlist.track_count.toLocaleString()}
                            </strong>
                            <span>tracks</span>
                          </div>
                          <div className="organizer-date-meta">
                            <span>{playlistDateLabel(row.playlist)}</span>
                            <small>
                              {row.playlist.updated_at
                                ? formatDate(row.playlist.updated_at)
                                : row.playlist.created_at
                                  ? formatDate(row.playlist.created_at)
                                  : "Provider does not expose dates"}
                            </small>
                          </div>
                          {actionMode === "remove_tracks" && eligible ? (
                            <button
                              className="organizer-expand"
                              type="button"
                              aria-expanded={expanded}
                              disabled={busy}
                              onClick={() =>
                                expanded
                                  ? setExpandedPlaylist(null)
                                  : void loadPlaylistTracks(row.playlist)
                              }
                            >
                              {trackSelection ? `${trackSelection} selected` : "Choose songs"}
                              {expanded ? <ChevronUp /> : <ChevronDown />}
                            </button>
                          ) : null}
                        </div>
                        {expanded && tracksByPlaylist[row.playlist.id] ? (
                          <div className="organizer-track-picker">
                            <div className="organizer-track-heading">
                              <span>Select exact playlist entries. Duplicates stay separate.</span>
                              <button
                                type="button"
                                onClick={() =>
                                  setSelectedTracks((current) => ({
                                    ...current,
                                    [row.playlist.id]: new Set(),
                                  }))
                                }
                              >
                                Clear songs
                              </button>
                            </div>
                            {tracksByPlaylist[row.playlist.id].map((track) => {
                              const key = organizerTrackKey(track);
                              const selectable = track.position !== null;
                              return (
                                <label key={key} className="organizer-track-row">
                                  <input
                                    type="checkbox"
                                    checked={
                                      selectedTracks[row.playlist.id]?.has(key) ?? false
                                    }
                                    disabled={!selectable}
                                    onChange={() => toggleTrack(row.playlist.id, track)}
                                  />
                                  <span>
                                    <strong>{track.title}</strong>
                                    <small>{track.artist}</small>
                                  </span>
                                  <span>{track.album ?? ""}</span>
                                  <span>#{(track.position ?? 0) + 1}</span>
                                </label>
                              );
                            })}
                          </div>
                        ) : null}
                      </article>
                    );
                  })
                )}
              </div>
            </section>

            <section className="card duplicate-review">
              <div className="section-heading">
                <div className="section-title">
                  <span className="section-icon" aria-hidden="true">
                    <ScanSearch />
                  </span>
                  <div>
                    <h2>Duplicate review</h2>
                    <p className="muted">
                      Candidates need matching names, compatible owners, and track overlap.
                    </p>
                  </div>
                </div>
                <button
                  className="secondary compact"
                  type="button"
                  disabled={duplicateLoading}
                  onClick={() => void scanDuplicates()}
                >
                  <ScanSearch aria-hidden="true" />
                  {duplicateLoading ? "Scanning…" : "Scan playlists"}
                </button>
              </div>
              {duplicates.length === 0 ? (
                <p className="empty-guidance">
                  Run a scan to find reviewable pairs. Candidates are never auto-selected or
                  removed.
                </p>
              ) : (
                <div className="duplicate-pairs">
                  {duplicates.map((candidate) => (
                    <article key={candidate.playlist_ids.join(":")} className="duplicate-pair">
                      <div>
                        <strong>
                          {candidate.playlist_names[0]} ↔ {candidate.playlist_names[1]}
                        </strong>
                        <p>
                          {candidate.overlap_count} overlapping tracks ·{" "}
                          {Math.round(candidate.overlap_ratio * 100)}% overlap
                        </p>
                        <small>{candidate.reasons.join(" · ")}</small>
                      </div>
                      <button
                        className="secondary compact"
                        type="button"
                        onClick={() => setDuplicateFocus(new Set(candidate.playlist_ids))}
                      >
                        Review pair
                      </button>
                    </article>
                  ))}
                </div>
              )}
            </section>
          </div>

          <aside className={`organizer-ledger ${destructiveMode ? "is-danger" : "is-safe"}`}>
            <div className="organizer-ledger-heading">
              <span>{destructiveMode ? <AlertTriangle /> : <ShieldCheck />}</span>
              <div>
                <p className="eyebrow">Operation ledger</p>
                <h2>{actionModeLabel(actionMode)}</h2>
              </div>
            </div>
            <div className="organizer-ledger-count">
              <strong>{selectionCount}</strong>
              <span>
                {actionMode === "remove_tracks"
                  ? `song${selectionCount === 1 ? "" : "s"} selected`
                  : `playlist${selectionCount === 1 ? "" : "s"} selected`}
              </span>
            </div>
            <p className="organizer-ledger-note">{actionModeDescription(actionMode)}</p>
            <button
              className={destructiveMode ? "organizer-danger-button" : "primary"}
              type="button"
              disabled={!canPreview}
              onClick={() => void previewAction()}
            >
              {destructiveMode ? <AlertTriangle /> : <ShieldCheck />}
              Review exact operation
            </button>
            <div className="organizer-ledger-rule" />
            <JobLedger
              jobs={jobs}
              activeJob={activeJob}
              busy={busy}
              onSelect={setActiveJob}
              onRetry={retryFailed}
            />
          </aside>
        </div>
      ) : null}

      {preflight ? (
        <PreflightDialog
          preflight={preflight}
          confirmation={confirmation}
          busy={busy}
          onConfirmation={setConfirmation}
          onClose={closePreflight}
          onExecute={executeAction}
        />
      ) : null}
    </section>
  );
}

interface ModeButtonProps {
  active: boolean;
  icon: React.ReactNode;
  label: string;
  detail: string;
  danger?: boolean;
  onClick: () => void;
}

function ModeButton({ active, icon, label, detail, danger = false, onClick }: ModeButtonProps) {
  return (
    <button
      className={["organizer-mode", danger ? "is-danger" : ""].filter(Boolean).join(" ")}
      type="button"
      aria-pressed={active}
      onClick={onClick}
    >
      <span>{icon}</span>
      <strong>{label}</strong>
      <small>{detail}</small>
    </button>
  );
}

interface PreflightDialogProps {
  preflight: OrganizerPreflightView;
  confirmation: string;
  busy: boolean;
  onConfirmation: (value: string) => void;
  onClose: () => void;
  onExecute: () => void | Promise<void>;
}

function PreflightDialog({
  preflight,
  confirmation,
  busy,
  onConfirmation,
  onClose,
  onExecute,
}: PreflightDialogProps) {
  const confirmed =
    !preflight.confirmation_required || confirmation === preflight.confirmation_phrase;
  return (
    <div className="organizer-dialog-layer" role="presentation">
      <section
        className="organizer-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="organizer-dialog-title"
      >
        <div className="organizer-dialog-heading">
          <div>
            <p className="eyebrow">Preflight receipt</p>
            <h2 id="organizer-dialog-title">Review every provider operation</h2>
          </div>
          <button type="button" aria-label="Close preflight" disabled={busy} onClick={onClose}>
            <X aria-hidden="true" />
          </button>
        </div>
        <div className="organizer-preflight-groups">
          {preflight.groups.map((group) => (
            <article
              key={group.action}
              className={`organizer-preflight-group ${group.destructive ? "is-danger" : "is-safe"}`}
            >
              <header>
                <span>{group.destructive ? <AlertTriangle /> : <ShieldCheck />}</span>
                <div>
                  <strong>{group.label}</strong>
                  <p>{group.recovery}</p>
                </div>
              </header>
              <ul>
                {group.items.map((item) => (
                  <li key={`${item.action}:${item.playlist_id}`}>
                    <span>
                      <strong>{item.playlist_name}</strong>
                      <small>
                        {ownershipLabel(item.ownership)}
                        {item.collaborative ? " · collaborative" : ""}
                      </small>
                    </span>
                    <span>
                      {item.selected_track_count
                        ? `${item.selected_track_count} songs`
                        : group.destructive
                          ? "Irreversible"
                          : "Can follow again"}
                    </span>
                  </li>
                ))}
              </ul>
            </article>
          ))}
          {preflight.unsupported.length > 0 ? (
            <article className="organizer-preflight-group is-warning">
              <header>
                <span>
                  <AlertTriangle />
                </span>
                <div>
                  <strong>Unsupported selections</strong>
                  <p>Remove these selections before the job can start.</p>
                </div>
              </header>
              <ul>
                {preflight.unsupported.map((item) => (
                  <li key={`${item.intent}:${item.playlist_id}`}>
                    <span>
                      <strong>{item.playlist_name}</strong>
                      <small>{item.reason}</small>
                    </span>
                  </li>
                ))}
              </ul>
            </article>
          ) : null}
        </div>
        {preflight.confirmation_required && preflight.confirmation_phrase ? (
          <label className="organizer-confirmation">
            <span>
              Type <code>{preflight.confirmation_phrase}</code> to continue.
            </span>
            <input
              autoFocus
              value={confirmation}
              onChange={(event) => onConfirmation(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
        ) : null}
        <div className="organizer-dialog-actions">
          <button className="secondary" type="button" disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button
            className={preflight.confirmation_required ? "organizer-danger-button" : "primary"}
            type="button"
            disabled={busy || !confirmed || preflight.unsupported.length > 0}
            onClick={() => void onExecute()}
          >
            {preflight.confirmation_required ? <AlertTriangle /> : <CheckCircle2 />}
            {busy ? "Starting…" : "Start organizer job"}
          </button>
        </div>
      </section>
    </div>
  );
}

interface JobLedgerProps {
  jobs: OrganizerJobView[];
  activeJob: OrganizerJobView | null;
  busy: boolean;
  onSelect: (job: OrganizerJobView) => void;
  onRetry: (job: OrganizerJobView) => void | Promise<void>;
}

function JobLedger({ jobs, activeJob, busy, onSelect, onRetry }: JobLedgerProps) {
  const retryable = activeJob?.items.some(
    (item) => item.status === "failed" && item.retryable,
  );
  return (
    <div className="organizer-job-ledger">
      <div className="organizer-job-heading">
        <strong>Audit history</strong>
        <span>{jobs.length} recent jobs</span>
      </div>
      {activeJob ? (
        <div className="organizer-active-job">
          <div className="organizer-job-status">
            <span className={`status status-${activeJob.status}`}>{jobStatus(activeJob.status)}</span>
            <small>{shortJobId(activeJob.id)}</small>
          </div>
          <p>
            {activeJob.done}/{activeJob.total} succeeded
            {activeJob.failed ? ` · ${activeJob.failed} failed` : ""}
          </p>
          {activeJob.error ? <p className="warn">{activeJob.error}</p> : null}
          <div className="organizer-job-items">
            {activeJob.items.map((item) => (
              <div key={item.id} className="organizer-job-item">
                <span
                  className={`organizer-job-dot status-${item.status}`}
                  aria-hidden="true"
                />
                <span>
                  <strong>{item.playlist_name}</strong>
                  <small>
                    {actionLabel(item.action)} · {item.status}
                    {item.attempts > 1 ? ` · ${item.attempts} attempts` : ""}
                  </small>
                  {item.error ? <em>{item.error}</em> : null}
                </span>
              </div>
            ))}
          </div>
          {retryable ? (
            <button
              className="secondary compact"
              type="button"
              disabled={busy}
              onClick={() => void onRetry(activeJob)}
            >
              <RotateCcw aria-hidden="true" />
              Retry failed only
            </button>
          ) : null}
        </div>
      ) : (
        <p className="muted">No organizer jobs yet.</p>
      )}
      {jobs.length > 1 ? (
        <div className="organizer-job-history">
          {jobs.slice(0, 8).map((job) => (
            <button
              key={job.id}
              type="button"
              aria-pressed={activeJob?.id === job.id}
              onClick={() => onSelect(job)}
            >
              <span>{formatDate(job.created_at)}</span>
              <strong>{jobStatus(job.status)}</strong>
              <small>
                {job.done}/{job.total}
              </small>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function buildRequestBody(
  provider: string,
  accountId: string,
  actionMode: OrganizerIntent,
  selectedPlaylists: Set<string>,
  selectedTracks: Record<string, Set<string>>,
  tracksByPlaylist: Record<string, Track[]>,
): OrganizerRequestBody {
  if (actionMode !== "remove_tracks") {
    return {
      provider,
      account_id: accountId,
      selection: {
        playlist_actions: [...selectedPlaylists].map((playlistId) => ({
          playlist_id: playlistId,
          intent: actionMode,
        })),
        track_removals: [],
      },
    };
  }
  return {
    provider,
    account_id: accountId,
    selection: {
      playlist_actions: [],
      track_removals: Object.entries(selectedTracks)
        .map(([playlistId, selected]) => ({
          playlist_id: playlistId,
          tracks: (tracksByPlaylist[playlistId] ?? [])
            .filter((track) => selected.has(organizerTrackKey(track)))
            .filter((track) => track.position !== null)
            .map((track) => ({
              position: track.position ?? 0,
              source_item_id: track.source_item_id,
            })),
        }))
        .filter((selection) => selection.tracks.length > 0),
    },
  };
}

function organizerTrackKey(track: Track): string {
  return `${track.source_item_id ?? track.id ?? "track"}:${track.position ?? -1}`;
}

function compareRows(
  left: OrganizerPlaylistView,
  right: OrganizerPlaylistView,
  sortMode: SortMode,
): number {
  if (sortMode === "owner") {
    return (left.playlist.owner_name ?? left.playlist.owner_id ?? "").localeCompare(
      right.playlist.owner_name ?? right.playlist.owner_id ?? "",
    );
  }
  if (sortMode === "tracks") {
    return (right.playlist.track_count ?? -1) - (left.playlist.track_count ?? -1);
  }
  if (sortMode === "updated") {
    return dateValue(right.playlist.updated_at ?? right.playlist.created_at) -
      dateValue(left.playlist.updated_at ?? left.playlist.created_at);
  }
  return left.playlist.name.localeCompare(right.playlist.name);
}

function dateValue(value: string | null): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function playlistDateLabel(playlist: PlaylistRef): string {
  if (playlist.updated_at) return "Updated";
  if (playlist.created_at) return "Created";
  return "Metadata";
}

function formatDate(value: string | null): string {
  if (!value) return "Date unavailable";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "Date unavailable";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(parsed);
}

function providerCapabilitySummary(provider: ProviderView): string {
  const capabilities = [
    provider.can_unfollow_playlist ? "safe remove" : null,
    provider.can_delete_playlist ? "delete" : null,
    provider.can_remove_tracks ? "song removal" : null,
  ].filter(Boolean);
  return capabilities.length ? capabilities.join(" · ") : "view only";
}

function ownershipLabel(value: string): string {
  return {
    owned: "Owned by you",
    collaborative: "Collaborative",
    followed: "Followed",
    unknown: "Ownership checked at preflight",
  }[value] ?? value;
}

function actionModeLabel(mode: OrganizerIntent): string {
  return {
    remove: "Remove from library",
    delete: "Delete permanently",
    remove_tracks: "Remove selected songs",
  }[mode];
}

function actionModeDescription(mode: OrganizerIntent): string {
  return {
    remove:
      "Uses the provider's non-destructive library removal. Permanent deletion is never substituted.",
    delete:
      "Only playlists confirmed as owned can proceed. The provider does not guarantee recovery.",
    remove_tracks:
      "Removes exact playlist entries. Spotify is capped at 100 songs per job to preserve snapshot safety.",
  }[mode];
}

function actionLabel(action: string): string {
  return {
    unfollow_playlist: "Removed from library",
    delete_playlist: "Deleted playlist",
    remove_tracks: "Removed songs",
  }[action] ?? action;
}

function jobStatus(status: string): string {
  return {
    pending: "Queued",
    running: "Running",
    done: "Complete",
    partial: "Partial",
    failed: "Failed",
  }[status] ?? status;
}

function replaceJob(jobs: OrganizerJobView[], nextJob: OrganizerJobView): OrganizerJobView[] {
  return [nextJob, ...jobs.filter((job) => job.id !== nextJob.id)];
}

function shortJobId(jobId: string): string {
  return `#${jobId.slice(0, 8)}`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
