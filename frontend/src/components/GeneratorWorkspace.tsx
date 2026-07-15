import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Check,
  ListRestart,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Trash2,
} from "lucide-react";
import {
  ApiError,
  addGenerationDraftItem,
  confirmGenerationDraft,
  createGenerationDraft,
  deleteGenerationDraft,
  deleteGenerationDraftItem,
  deleteGeneratorPreferences,
  getGeneratorConfig,
  getGeneratorPreferences,
  reorderGenerationDraftItems,
  searchGeneratorTracks,
  updateGenerationDraft,
  updateGenerationDraftItem,
  updateGeneratorPreferences,
} from "../api/client";
import type {
  AccountView,
  ExplicitPreference,
  GenerationDraftItemView,
  GenerationDraftView,
  GeneratorCandidateView,
  GeneratorConfigView,
  GeneratorPreferenceView,
  GeneratorWarningView,
  ProviderView,
} from "../api/types";
import { providerLabel } from "../utils/providers";

interface Props {
  providers: ProviderView[];
  accounts: AccountView[];
  onOpenConnections: () => void;
  onJobCreated: (jobId: string) => void;
}

interface SearchContext {
  itemId: string | null;
  title: string;
  artist: string;
  album: string;
}

export default function GeneratorWorkspace({
  providers,
  accounts,
  onOpenConnections,
  onJobCreated,
}: Props) {
  const [config, setConfig] = useState<GeneratorConfigView | null>(null);
  const [preferences, setPreferences] = useState<GeneratorPreferenceView | null>(null);
  const [targetAccountId, setTargetAccountId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [genres, setGenres] = useState("");
  const [moods, setMoods] = useState("");
  const [eras, setEras] = useState("");
  const [energy, setEnergy] = useState(3);
  const [trackCount, setTrackCount] = useState(20);
  const [durationMinutes, setDurationMinutes] = useState(60);
  const [seedArtists, setSeedArtists] = useState("");
  const [seedTracks, setSeedTracks] = useState("");
  const [explicit, setExplicit] = useState<ExplicitPreference>("allow");
  const [familiarity, setFamiliarity] = useState(50);
  const [discovery, setDiscovery] = useState(50);
  const [draft, setDraft] = useState<GenerationDraftView | null>(null);
  const [detailsDirty, setDetailsDirty] = useState(false);
  const [searchContext, setSearchContext] = useState<SearchContext | null>(null);
  const [searchResults, setSearchResults] = useState<GeneratorCandidateView[]>([]);
  const [warnings, setWarnings] = useState<GeneratorWarningView | null>(null);
  const [busy, setBusy] = useState(false);
  const [searchBusy, setSearchBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const targetProviders = useMemo(
    () => new Set(providers.filter((provider) => provider.can_target).map((provider) => provider.name)),
    [providers],
  );
  const targetAccounts = useMemo(
    () => accounts.filter((account) => targetProviders.has(account.provider)),
    [accounts, targetProviders],
  );
  const targetAccount =
    targetAccounts.find((account) => account.id === targetAccountId) ?? targetAccounts[0] ?? null;
  const targetProvider = targetAccount?.provider ?? null;
  const unresolvedCount = draft?.items.filter((item) => item.status === "unresolved").length ?? 0;
  const reviewCount = draft?.items.filter((item) => item.status === "needs_review").length ?? 0;
  const confirmDisabled =
    busy ||
    !draft ||
    draft.items.length === 0 ||
    unresolvedCount > 0 ||
    reviewCount > 0;

  useEffect(() => {
    let cancelled = false;
    Promise.all([getGeneratorConfig(), getGeneratorPreferences()])
      .then(([nextConfig, nextPreferences]) => {
        if (cancelled) return;
        setConfig(nextConfig);
        setPreferences(nextPreferences);
      })
      .catch((nextError: unknown) => {
        if (!cancelled) setError(errorMessage(nextError));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!targetAccount || targetAccounts.some((account) => account.id === targetAccountId)) return;
    setTargetAccountId(targetAccount.id);
  }, [targetAccount, targetAccountId, targetAccounts]);

  async function generatePlaylist() {
    if (!config?.available || !targetAccount || !targetProvider || !prompt.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    setWarnings(null);
    const previousDraft = draft;
    try {
      const nextDraft = await createGenerationDraft({
        target_provider: targetProvider,
        target_account_id: targetAccount.id,
        generation: {
          prompt: prompt.trim(),
          controls: {
            genres: splitList(genres),
            moods: splitList(moods),
            eras: splitList(eras),
            energy,
            track_count: trackCount,
            duration_minutes: durationMinutes,
            seed_artists: splitList(seedArtists),
            seed_tracks: splitList(seedTracks),
            explicit,
            familiarity,
            discovery,
          },
        },
        use_personalization: Boolean(preferences?.enabled),
      });
      setDraft(nextDraft);
      setDetailsDirty(false);
      setSearchContext(null);
      setSearchResults([]);
      setNotice(
        `Resolved ${nextDraft.items.length - unresolvedCountFor(nextDraft)} of ${
          nextDraft.items.length
        } suggestions on ${providerLabel(nextDraft.target_provider)}.`,
      );
      if (previousDraft && previousDraft.status === "draft" && previousDraft.id !== nextDraft.id) {
        try {
          await deleteGenerationDraft(previousDraft.id);
        } catch (cleanupError: unknown) {
          setNotice(
            `New draft created. The previous private draft could not be removed: ${errorMessage(
              cleanupError,
            )}`,
          );
        }
      }
    } catch (nextError: unknown) {
      setError(errorMessage(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function setPersonalization(enabled: boolean) {
    setBusy(true);
    setError(null);
    try {
      const next = await updateGeneratorPreferences(enabled);
      setPreferences(next);
      setNotice(
        enabled
          ? "Local personalization is on. Only the visible aggregate summary is sent."
          : "Local personalization is off.",
      );
    } catch (nextError: unknown) {
      setError(errorMessage(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function resetPersonalization() {
    setBusy(true);
    setError(null);
    try {
      setPreferences(await deleteGeneratorPreferences());
      setNotice("Stored personalization summary deleted.");
    } catch (nextError: unknown) {
      setError(errorMessage(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function saveDraftDetails(): Promise<GenerationDraftView> {
    if (!draft) throw new Error("Generation draft is not available");
    if (!detailsDirty) return draft;
    const next = await updateGenerationDraft(draft.id, {
      name: draft.name.trim(),
      description: draft.description?.trim() || null,
    });
    setDraft(next);
    setDetailsDirty(false);
    return next;
  }

  async function moveItem(itemId: string, direction: -1 | 1) {
    if (!draft) return;
    const index = draft.items.findIndex((item) => item.id === itemId);
    const nextIndex = index + direction;
    if (index < 0 || nextIndex < 0 || nextIndex >= draft.items.length) return;
    const ordered = [...draft.items];
    [ordered[index], ordered[nextIndex]] = [ordered[nextIndex], ordered[index]];
    await updateDraft(() => reorderGenerationDraftItems(draft.id, ordered.map((item) => item.id)));
  }

  async function removeItem(itemId: string) {
    if (!draft) return;
    await updateDraft(() => deleteGenerationDraftItem(draft.id, itemId));
  }

  async function approveItem(itemId: string) {
    if (!draft) return;
    await updateDraft(() => updateGenerationDraftItem(draft.id, itemId, { action: "approve" }));
  }

  async function updateDraft(action: () => Promise<GenerationDraftView>) {
    setBusy(true);
    setError(null);
    setWarnings(null);
    try {
      setDraft(await action());
      setSearchContext(null);
      setSearchResults([]);
    } catch (nextError: unknown) {
      setError(errorMessage(nextError));
    } finally {
      setBusy(false);
    }
  }

  function openSearch(item: GenerationDraftItemView | null) {
    setSearchResults([]);
    setSearchContext({
      itemId: item?.id ?? null,
      title: item?.candidate?.title ?? item?.intent.title ?? "",
      artist: item?.candidate?.artist ?? item?.intent.artist ?? "",
      album: item?.candidate?.album ?? item?.intent.album ?? "",
    });
  }

  async function runSearch() {
    if (!searchContext || !targetAccount || !targetProvider) return;
    setSearchBusy(true);
    setError(null);
    try {
      setSearchResults(
        await searchGeneratorTracks({
          target_provider: targetProvider,
          target_account_id: targetAccount.id,
          title: searchContext.title.trim(),
          artist: searchContext.artist.trim(),
          album: searchContext.album.trim() || null,
          limit: 8,
        }),
      );
    } catch (nextError: unknown) {
      setError(errorMessage(nextError));
    } finally {
      setSearchBusy(false);
    }
  }

  async function chooseCandidate(candidate: GeneratorCandidateView) {
    if (!draft || !searchContext) return;
    await updateDraft(() =>
      searchContext.itemId
        ? updateGenerationDraftItem(draft.id, searchContext.itemId, {
            action: "replace",
            candidate,
          })
        : addGenerationDraftItem(draft.id, candidate),
    );
  }

  async function discardDraft() {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      await deleteGenerationDraft(draft.id);
      setDraft(null);
      setDetailsDirty(false);
      setSearchContext(null);
      setSearchResults([]);
      setWarnings(null);
      setNotice("Private generation draft discarded.");
    } catch (nextError: unknown) {
      setError(errorMessage(nextError));
    } finally {
      setBusy(false);
    }
  }

  async function confirmDraft(acknowledgeWarnings: boolean) {
    if (!draft) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await saveDraftDetails();
      const job = await confirmGenerationDraft(saved.id, acknowledgeWarnings);
      setWarnings(null);
      setNotice("Playlist confirmed. Provider writing has started.");
      onJobCreated(job.id);
    } catch (nextError: unknown) {
      if (isGenerationWarning(nextError)) {
        setWarnings(nextError.detail);
      } else {
        setError(errorMessage(nextError));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="generator-workspace">
      <section className="generator-hero">
        <div>
          <span className="generator-eyebrow">
            <Sparkles aria-hidden="true" />
            Prompt to provider tracks
          </span>
          <h2>Build the idea. Review every song. Create only when ready.</h2>
          <p>
            The model proposes search intents. Open Playlist Engine resolves real tracks on your
            target provider and keeps the draft private until you confirm it.
          </p>
        </div>
        <div className={config?.available ? "model-signal ready" : "model-signal"}>
          <span aria-hidden="true" />
          <strong>{config?.available ? "Model ready" : "Model not configured"}</strong>
          <small>
            {config
              ? `${backendLabel(config.backend)}${config.model ? ` / ${config.model}` : ""}`
              : "Checking local setup..."}
          </small>
        </div>
      </section>

      {error ? <p className="warn generator-message">{error}</p> : null}
      {notice ? <p className="notice generator-message">{notice}</p> : null}

      {!config?.available ? (
        <section className="card generator-setup" role="status">
          <AlertTriangle aria-hidden="true" />
          <div>
            <strong>Generation stays off until an administrator configures a model.</strong>
            <p>{config?.message ?? "Loading generator configuration..."}</p>
          </div>
        </section>
      ) : null}

      <div className="generator-console">
        <section className="card generator-brief">
          <div className="section-heading">
            <div className="section-title">
              <Sparkles aria-hidden="true" />
              <div>
                <h2>Playlist brief</h2>
                <p className="muted">Prompt and controls are sent only when you generate.</p>
              </div>
            </div>
          </div>

          <label className="generator-field">
            <span>Target account</span>
            <select
              value={targetAccount?.id ?? ""}
              onChange={(event) => setTargetAccountId(event.target.value)}
              disabled={busy || targetAccounts.length === 0}
            >
              {targetAccounts.length === 0 ? <option value="">No connected targets</option> : null}
              {targetAccounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {providerLabel(account.provider)} - {account.display_name ?? "Connected account"}
                </option>
              ))}
            </select>
          </label>
          {targetAccounts.length === 0 ? (
            <button className="secondary compact" onClick={onOpenConnections}>
              Open provider connections
            </button>
          ) : null}

          <label className="generator-field generator-prompt">
            <span>Describe the playlist</span>
            <textarea
              value={prompt}
              maxLength={config?.limits.max_prompt_chars ?? 2000}
              placeholder="Late-night train ride through rainy cities, mostly downtempo electronic with a few warm jazz turns."
              onChange={(event) => setPrompt(event.target.value)}
            />
            <small>
              {prompt.length}/{config?.limits.max_prompt_chars ?? 2000} characters
            </small>
          </label>

          <div className="generator-primary-controls">
            <label className="generator-field">
              <span>Tracks</span>
              <input
                type="number"
                min={1}
                max={config?.limits.max_tracks ?? 25}
                value={trackCount}
                onChange={(event) => setTrackCount(Number(event.target.value))}
              />
            </label>
            <label className="generator-field">
              <span>Minutes</span>
              <input
                type="number"
                min={10}
                max={600}
                value={durationMinutes}
                onChange={(event) => setDurationMinutes(Number(event.target.value))}
              />
            </label>
            <label className="generator-field">
              <span>Explicit content</span>
              <select
                value={explicit}
                onChange={(event) => setExplicit(event.target.value as ExplicitPreference)}
              >
                <option value="allow">Allow</option>
                <option value="exclude">Exclude</option>
                <option value="only">Only explicit</option>
              </select>
            </label>
          </div>

          <details className="generator-controls">
            <summary>
              <SlidersHorizontal aria-hidden="true" />
              Shape the result
            </summary>
            <div className="generator-control-grid">
              <TextListField label="Genres" value={genres} onChange={setGenres} />
              <TextListField label="Moods" value={moods} onChange={setMoods} />
              <TextListField label="Eras or decades" value={eras} onChange={setEras} />
              <TextListField label="Seed artists" value={seedArtists} onChange={setSeedArtists} />
              <TextListField label="Seed tracks" value={seedTracks} onChange={setSeedTracks} />
              <RangeField label="Energy" min={1} max={5} value={energy} onChange={setEnergy} />
              <RangeField
                label="Familiarity"
                min={0}
                max={100}
                value={familiarity}
                onChange={setFamiliarity}
              />
              <RangeField
                label="Discovery"
                min={0}
                max={100}
                value={discovery}
                onChange={setDiscovery}
              />
            </div>
          </details>

          <div className="personalization-strip">
            <div>
              <strong>Local personalization</strong>
              <p>
                Uses only a capped summary of locally cached artists and genres. Raw history stays
                on this instance.
              </p>
              {preferences?.enabled ? (
                <div className="preference-chips">
                  {preferences.summary.top_artists.map((artist) => (
                    <span key={`artist-${artist}`}>{artist}</span>
                  ))}
                  {preferences.summary.top_genres.map((genre) => (
                    <span key={`genre-${genre}`}>{genre}</span>
                  ))}
                  {preferences.summary.source_track_count === 0 ? (
                    <span>No cached history yet</span>
                  ) : null}
                </div>
              ) : null}
            </div>
            <div className="personalization-actions">
              <label className="generator-switch">
                <input
                  type="checkbox"
                  checked={Boolean(preferences?.enabled)}
                  disabled={busy || !preferences}
                  onChange={(event) => void setPersonalization(event.target.checked)}
                />
                <span>{preferences?.enabled ? "On" : "Off"}</span>
              </label>
              <button
                className="secondary compact"
                disabled={busy || !preferences}
                onClick={() => void resetPersonalization()}
              >
                <Trash2 aria-hidden="true" />
                Delete data
              </button>
            </div>
          </div>

          <button
            className="primary generator-generate"
            disabled={
              busy ||
              !config?.available ||
              !targetAccount ||
              !prompt.trim() ||
              trackCount < 1 ||
              trackCount > (config?.limits.max_tracks ?? 25)
            }
            onClick={() => void generatePlaylist()}
          >
            {busy ? <RefreshCw className="spin" aria-hidden="true" /> : <Sparkles aria-hidden="true" />}
            {draft ? "Regenerate draft" : "Generate draft"}
          </button>
        </section>

        <section className="card generator-review">
          <div className="section-heading">
            <div className="section-title">
              <ListRestart aria-hidden="true" />
              <div>
                <h2>Resolved cue sheet</h2>
                <p className="muted">Nothing is written from this screen until final confirmation.</p>
              </div>
            </div>
            {draft ? (
              <button className="secondary compact" disabled={busy} onClick={() => void discardDraft()}>
                Discard
              </button>
            ) : null}
          </div>

          {!draft ? (
            <div className="generator-empty">
              <span aria-hidden="true">A</span>
              <p>Generate a draft to see provider-resolved tracks here.</p>
            </div>
          ) : (
            <>
              <div className="draft-metadata">
                <label className="generator-field">
                  <span>Playlist name</span>
                  <input
                    value={draft.name}
                    maxLength={100}
                    onChange={(event) => {
                      setDraft({ ...draft, name: event.target.value });
                      setDetailsDirty(true);
                    }}
                  />
                </label>
                <label className="generator-field">
                  <span>Description</span>
                  <textarea
                    value={draft.description ?? ""}
                    maxLength={500}
                    onChange={(event) => {
                      setDraft({ ...draft, description: event.target.value });
                      setDetailsDirty(true);
                    }}
                  />
                </label>
                {detailsDirty ? (
                  <button
                    className="secondary compact"
                    disabled={busy || !draft.name.trim()}
                    onClick={() => void updateDraft(saveDraftDetails)}
                  >
                    Save details
                  </button>
                ) : null}
              </div>

              <div className="draft-status-line">
                <span>{draft.items.length} unique tracks</span>
                <span className={reviewCount ? "needs-review" : ""}>
                  {reviewCount} need approval
                </span>
                <span className={unresolvedCount ? "unresolved" : ""}>
                  {unresolvedCount} unresolved
                </span>
              </div>

              <ol className="generation-track-list">
                {draft.items.map((item, index) => (
                  <li key={item.id} className={`generation-track ${item.status}`}>
                    <span className="track-index">{String(index + 1).padStart(2, "0")}</span>
                    <span className="resolution-node" aria-hidden="true" />
                    <div className="generation-track-copy">
                      <div className="generation-track-heading">
                        <span>
                          <strong>{item.candidate?.title ?? item.intent.title}</strong>
                          <small>{item.candidate?.artist ?? item.intent.artist}</small>
                        </span>
                        <StatusBadge item={item} />
                      </div>
                      {item.candidate &&
                      (item.candidate.title !== item.intent.title ||
                        item.candidate.artist !== item.intent.artist) ? (
                        <p className="match-origin">
                          Suggested as {item.intent.title} by {item.intent.artist}
                        </p>
                      ) : null}
                      {item.reason ? <p className="track-reason">{item.reason}</p> : null}
                    </div>
                    <div className="generation-track-actions">
                      <button
                        className="icon-button"
                        title="Move up"
                        aria-label={`Move ${item.intent.title} up`}
                        disabled={busy || index === 0}
                        onClick={() => void moveItem(item.id, -1)}
                      >
                        <ArrowUp aria-hidden="true" />
                      </button>
                      <button
                        className="icon-button"
                        title="Move down"
                        aria-label={`Move ${item.intent.title} down`}
                        disabled={busy || index === draft.items.length - 1}
                        onClick={() => void moveItem(item.id, 1)}
                      >
                        <ArrowDown aria-hidden="true" />
                      </button>
                      {item.status === "needs_review" ? (
                        <button
                          className="secondary compact"
                          disabled={busy}
                          onClick={() => void approveItem(item.id)}
                        >
                          <Check aria-hidden="true" />
                          Approve
                        </button>
                      ) : null}
                      <button
                        className="secondary compact"
                        disabled={busy}
                        onClick={() => openSearch(item)}
                      >
                        <Search aria-hidden="true" />
                        {item.status === "unresolved" ? "Resolve" : "Replace"}
                      </button>
                      <button
                        className="icon-button danger"
                        title="Remove track"
                        aria-label={`Remove ${item.intent.title}`}
                        disabled={busy}
                        onClick={() => void removeItem(item.id)}
                      >
                        <Trash2 aria-hidden="true" />
                      </button>
                    </div>
                  </li>
                ))}
              </ol>

              <button className="secondary generator-add-track" disabled={busy} onClick={() => openSearch(null)}>
                <Plus aria-hidden="true" />
                Add a provider track
              </button>

              {searchContext ? (
                <div className="generator-search-panel">
                  <div className="generator-search-heading">
                    <div>
                      <strong>{searchContext.itemId ? "Find a replacement" : "Add a real track"}</strong>
                      <p>Results come directly from {providerLabel(targetProvider)}.</p>
                    </div>
                    <button
                      className="secondary compact"
                      onClick={() => {
                        setSearchContext(null);
                        setSearchResults([]);
                      }}
                    >
                      Close
                    </button>
                  </div>
                  <div className="generator-search-fields">
                    <label className="generator-field">
                      <span>Title</span>
                      <input
                        value={searchContext.title}
                        onChange={(event) =>
                          setSearchContext({ ...searchContext, title: event.target.value })
                        }
                      />
                    </label>
                    <label className="generator-field">
                      <span>Artist</span>
                      <input
                        value={searchContext.artist}
                        onChange={(event) =>
                          setSearchContext({ ...searchContext, artist: event.target.value })
                        }
                      />
                    </label>
                    <label className="generator-field">
                      <span>Album (optional)</span>
                      <input
                        value={searchContext.album}
                        onChange={(event) =>
                          setSearchContext({ ...searchContext, album: event.target.value })
                        }
                      />
                    </label>
                    <button
                      className="primary compact"
                      disabled={
                        searchBusy || !searchContext.title.trim() || !searchContext.artist.trim()
                      }
                      onClick={() => void runSearch()}
                    >
                      <Search aria-hidden="true" />
                      {searchBusy ? "Searching..." : "Search"}
                    </button>
                  </div>
                  {searchResults.length > 0 ? (
                    <div className="generator-search-results">
                      {searchResults.map((candidate) => (
                        <button
                          key={candidate.uri}
                          className="generator-search-result"
                          disabled={busy}
                          onClick={() => void chooseCandidate(candidate)}
                        >
                          <span>
                            <strong>{candidate.title}</strong>
                            <small>
                              {candidate.artist}
                              {candidate.album ? ` / ${candidate.album}` : ""}
                            </small>
                          </span>
                          <span>Select</span>
                        </button>
                      ))}
                    </div>
                  ) : searchBusy ? null : (
                    <p className="muted">Search by exact title and artist for the best match.</p>
                  )}
                </div>
              ) : null}

              {warnings ? (
                <div className="generator-warning-panel" role="alert">
                  <AlertTriangle aria-hidden="true" />
                  <div>
                    <strong>{warnings.message}</strong>
                    <ul>
                      {warnings.warnings.map((warning) => (
                        <li key={warning.code}>{warning.message}</li>
                      ))}
                    </ul>
                  </div>
                  <button className="primary compact" disabled={busy} onClick={() => void confirmDraft(true)}>
                    Create anyway
                  </button>
                </div>
              ) : null}

              <div className="generator-confirm">
                <div>
                  <span className="action-label">
                    <ShieldCheck aria-hidden="true" />
                    Final confirmation
                  </span>
                  <p>
                    {confirmDisabled
                      ? "Resolve or remove every flagged track before creating the playlist."
                      : `Ready to create ${draft.items.length} tracks on ${providerLabel(
                          draft.target_provider,
                        )}.`}
                  </p>
                </div>
                <button
                  className="primary"
                  disabled={confirmDisabled || !draft.name.trim()}
                  onClick={() => void confirmDraft(false)}
                >
                  <Check aria-hidden="true" />
                  {busy ? "Confirming..." : "Confirm and create playlist"}
                </button>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function TextListField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="generator-field">
      <span>{label}</span>
      <input
        value={value}
        placeholder="Comma separated"
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function RangeField({
  label,
  min,
  max,
  value,
  onChange,
}: {
  label: string;
  min: number;
  max: number;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="generator-field generator-range">
      <span>
        {label}
        <strong>{value}</strong>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function StatusBadge({ item }: { item: GenerationDraftItemView }) {
  const confidence =
    item.confidence === null ? null : `${Math.round(item.confidence * 100)}%`;
  if (item.status === "resolved") {
    return <span className="generation-status resolved">Resolved{confidence ? ` ${confidence}` : ""}</span>;
  }
  if (item.status === "needs_review") {
    return <span className="generation-status needs-review">Review{confidence ? ` ${confidence}` : ""}</span>;
  }
  return <span className="generation-status unresolved">Unresolved</span>;
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 10);
}

function unresolvedCountFor(draft: GenerationDraftView): number {
  return draft.items.filter((item) => item.status === "unresolved").length;
}

function backendLabel(backend: GeneratorConfigView["backend"]): string {
  return backend === "copilot_sdk" ? "Copilot SDK" : "OpenAI-compatible";
}

function isGenerationWarning(
  error: unknown,
): error is ApiError & { detail: GeneratorWarningView } {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  if (!error.detail || typeof error.detail !== "object") return false;
  const detail = error.detail as Partial<GeneratorWarningView>;
  return detail.code === "generation_warnings" && Array.isArray(detail.warnings);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
