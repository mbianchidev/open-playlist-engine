import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  CircleAlert,
  LibraryBig,
  RefreshCw,
  Search,
  Unplug,
} from "lucide-react";
import {
  ApiError,
  createMigration,
  getUnifiedPlaylists,
  preflightMigration,
} from "../api/client";
import type {
  AccountView,
  CreateMigrationBody,
  MigrationWarningsView,
  ProviderView,
  UnifiedPlaylistLibraryView,
  UnifiedPlaylistMemberView,
  UnifiedPlaylistView,
} from "../api/types";
import { providerLabel, providerTrackUrl } from "../utils/providers";
import ProgressBoard from "./ProgressBoard";
import ProviderIcon from "./ProviderIcon";

interface Props {
  providers: ProviderView[];
  accounts: AccountView[];
  onOpenConnections: () => void;
  onMigrationChanged: () => void | Promise<void>;
}

interface StartedMigration {
  jobId: string;
  targetLabel: string;
}

const CADENCES = [
  [15, "Every 15 minutes"],
  [60, "Hourly"],
  [360, "Every 6 hours"],
  [1440, "Daily"],
] as const;

export default function UnifiedPlaylistLibrary({
  providers,
  accounts,
  onOpenConnections,
  onMigrationChanged,
}: Props) {
  const [library, setLibrary] = useState<UnifiedPlaylistLibraryView | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [canonicalByPlaylist, setCanonicalByPlaylist] = useState<Record<string, string>>({});
  const [cadenceByPlaylist, setCadenceByPlaylist] = useState<Record<string, number>>({});
  const [startedByPlaylist, setStartedByPlaylist] = useState<
    Record<string, StartedMigration[]>
  >({});
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const next = await getUnifiedPlaylists(refresh);
      setLibrary(next);
      setCanonicalByPlaylist((current) =>
        Object.fromEntries(
          next.playlists.map((playlist) => [
            playlist.id,
            playlist.members.some((member) => member.key === current[playlist.id])
              ? current[playlist.id]
              : playlist.canonical_member_key,
          ]),
        ),
      );
    } catch (caught: unknown) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const visiblePlaylists = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return library?.playlists ?? [];
    return (library?.playlists ?? []).filter((playlist) => {
      const haystack = [
        playlist.name,
        playlist.description ?? "",
        ...playlist.members.flatMap((member) => [
          member.playlist_name,
          member.account_label,
          providerLabel(member.provider),
        ]),
        ...playlist.tracks.flatMap((track) => [track.title, track.artist, track.album ?? ""]),
      ]
        .join(" ")
        .toLocaleLowerCase();
      return haystack.includes(normalized);
    });
  }, [library?.playlists, query]);

  async function keepEverywhere(playlist: UnifiedPlaylistView) {
    const canonical =
      playlist.members.find(
        (member) => member.key === canonicalByPlaylist[playlist.id],
      ) ?? playlist.members[0];
    if (!canonical) return;
    if (playlist.sync_links.some((link) => link.target_member_key === canonical.key)) {
      setError(
        `Choose the existing sync source for "${playlist.name}" before keeping it everywhere. Reversing a sync would create a feedback loop.`,
      );
      return;
    }
    const managedAccounts = managedTargetAccounts(playlist, canonical.key);
    const targetAccounts = accounts.filter((account) => {
      const provider = providers.find((item) => item.name === account.provider);
      return (
        account.id !== canonical.account_id &&
        provider?.can_target === true &&
        !managedAccounts.has(account.id)
      );
    });
    if (targetAccounts.length === 0) {
      setNotice(`"${playlist.name}" is already managed everywhere it can be written.`);
      return;
    }

    const cadence = cadenceByPlaylist[playlist.id] ?? 60;
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    const migrations = targetAccounts.map((account) => ({
      account,
      body: migrationBody(
        playlist,
        canonical,
        account,
        providers,
        cadence,
        timezone,
      ),
    }));
    setBusyId(playlist.id);
    setError(null);
    setNotice(null);
    try {
      const preflights = await Promise.all(
        migrations.map(async ({ account, body }) => ({
          account,
          result: await preflightMigration(body),
        })),
      );
      const warningLines = preflights.flatMap(({ account, result }) =>
        result.warnings.map(
          (warning) => `${providerLabel(account.provider)}: ${warning.message}`,
        ),
      );
      if (
        warningLines.length > 0 &&
        !confirm(
          [
            `Review before keeping "${playlist.name}" everywhere:`,
            "",
            ...warningLines.map((line) => `• ${line}`),
            "",
            "Continue?",
          ].join("\n"),
        )
      ) {
        return;
      }

      const started: StartedMigration[] = [];
      const failures: string[] = [];
      for (const { account, body } of migrations) {
        try {
          const job = await createMigration({ ...body, acknowledge_warnings: true });
          started.push({
            jobId: job.id,
            targetLabel: account.display_name ?? providerLabel(account.provider),
          });
        } catch (caught: unknown) {
          failures.push(
            `${account.display_name ?? providerLabel(account.provider)}: ${errorMessage(caught)}`,
          );
        }
      }
      if (started.length > 0) {
        setStartedByPlaylist((current) => ({
          ...current,
          [playlist.id]: [...(current[playlist.id] ?? []), ...started],
        }));
        setNotice(
          `Started ${started.length} migration${started.length === 1 ? "" : "s"} for "${playlist.name}". Each successful copy becomes a continuous sync.`,
        );
        await onMigrationChanged();
      }
      if (failures.length > 0) {
        setError(`Some providers could not start:\n${failures.join("\n")}`);
      }
    } catch (caught: unknown) {
      if (isMigrationWarning(caught)) {
        setError(caught.detail.message);
      } else {
        setError(errorMessage(caught));
      }
    } finally {
      setBusyId(null);
    }
  }

  if (accounts.length === 0) {
    return (
      <section className="unified-empty card">
        <span className="unified-empty-mark" aria-hidden="true">
          <Unplug />
        </span>
        <div>
          <h2>Connect a music provider first</h2>
          <p>
            The neutral library reads your connected accounts and maps equivalent playlists
            and songs without replacing provider ownership.
          </p>
        </div>
        <button className="primary" onClick={onOpenConnections}>
          Connect providers
        </button>
      </section>
    );
  }

  return (
    <div className="unified-library">
      <section className="unified-header">
        <div>
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <LibraryBig />
            </span>
            <div>
              <h2>Your playlists, one map</h2>
              <p className="muted">
                One provider-neutral view of every connected copy and every song.
              </p>
            </div>
          </div>
          <div className="unified-provider-rail" aria-label="Connected playlist providers">
            {uniqueConnectedProviders(accounts).map((provider) => (
              <span key={provider}>
                <ProviderIcon provider={provider} />
                {providerLabel(provider)}
              </span>
            ))}
          </div>
        </div>
        <button
          className="secondary compact"
          disabled={loading}
          onClick={() => void load(true)}
        >
          <RefreshCw aria-hidden="true" />
          {loading ? "Scanning..." : "Refresh providers"}
        </button>
      </section>

      {error ? <p className="unified-message warn">{error}</p> : null}
      {notice ? <p className="unified-message notice">{notice}</p> : null}
      {library?.warnings.length ? (
        <div className="unified-warning-list" role="status">
          {library.warnings.map((warning) => (
            <p key={`${warning.account_id}:${warning.playlist_id ?? "account"}`}>
              <CircleAlert aria-hidden="true" />
              <span>
                <strong>{providerLabel(warning.provider)}</strong> {warning.message}
              </span>
            </p>
          ))}
        </div>
      ) : null}

      <div className="unified-toolbar">
        <label className="unified-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search unified playlists</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search playlists, songs, artists"
          />
        </label>
        <p className="muted">
          {visiblePlaylists.length} playlist{visiblePlaylists.length === 1 ? "" : "s"} across{" "}
          {library?.connected_provider_count ?? uniqueConnectedProviders(accounts).length} provider
          {(library?.connected_provider_count ??
            uniqueConnectedProviders(accounts).length) === 1
            ? ""
            : "s"}
        </p>
      </div>

      {loading && !library ? (
        <UnifiedSkeleton />
      ) : visiblePlaylists.length === 0 ? (
        <section className="unified-no-results">
          <LibraryBig aria-hidden="true" />
          <h3>{query ? "No playlist matches that search" : "No playlists found"}</h3>
          <p>
            {query
              ? "Try a playlist name, artist, song, or connected account."
              : "Refresh after creating or following a playlist in a connected provider."}
          </p>
        </section>
      ) : (
        <div className="unified-playlist-list">
          {visiblePlaylists.map((playlist) => {
            const expanded = expandedId === playlist.id;
            const canonicalKey =
              canonicalByPlaylist[playlist.id] ?? playlist.canonical_member_key;
            const canonical =
              playlist.members.find((member) => member.key === canonicalKey) ??
              playlist.members[0];
            const canonicalHasIncomingSync = playlist.sync_links.some(
              (link) => link.target_member_key === canonicalKey,
            );
            const managedCount = canonical
              ? managedTargetAccounts(playlist, canonical.key).size
              : 0;
            const writableTargetCount = accounts.filter(
              (account) =>
                account.id !== canonical?.account_id &&
                providers.some(
                  (provider) => provider.name === account.provider && provider.can_target,
                ),
            ).length;
            return (
              <article key={playlist.id} className="unified-playlist">
                <button
                  className="unified-playlist-summary"
                  type="button"
                  aria-expanded={expanded}
                  aria-controls={`unified-playlist-${playlist.id}`}
                  onClick={() => setExpandedId(expanded ? null : playlist.id)}
                >
                  <PlaylistArtwork playlist={playlist} />
                  <span className="unified-playlist-copy">
                    <strong>{playlist.name}</strong>
                    <span>
                      {playlist.tracks.length} song{playlist.tracks.length === 1 ? "" : "s"} ·{" "}
                      {playlist.members.length} provider cop
                      {playlist.members.length === 1 ? "y" : "ies"}
                    </span>
                  </span>
                  <span className="unified-member-icons" aria-label="Available providers">
                    {uniqueMemberProviders(playlist).map((provider) => (
                      <span key={provider} title={providerLabel(provider)}>
                        <ProviderIcon provider={provider} />
                      </span>
                    ))}
                  </span>
                  <span className={`unified-alignment unified-alignment-${playlist.alignment}`}>
                    {alignmentLabel(playlist.alignment)}
                  </span>
                  <ChevronDown className="unified-chevron" aria-hidden="true" />
                </button>

                {expanded ? (
                  <div id={`unified-playlist-${playlist.id}`} className="unified-playlist-detail">
                    <div className="unified-sync-strip">
                      <div>
                        <strong>Keep this playlist everywhere</strong>
                        <p>
                          Choose the source of truth. Exact mirror is used where supported;
                          other providers receive new songs with add-only sync.
                        </p>
                        {canonicalHasIncomingSync ? (
                          <p className="unified-canonical-warning">
                            This copy is already a sync target. Choose the existing source to
                            avoid a feedback loop.
                          </p>
                        ) : null}
                      </div>
                      <label>
                        Source of truth
                        <select
                          value={canonicalKey}
                          onChange={(event) =>
                            setCanonicalByPlaylist((current) => ({
                              ...current,
                              [playlist.id]: event.target.value,
                            }))
                          }
                        >
                          {playlist.members.map((member) => (
                            <option key={member.key} value={member.key}>
                              {providerLabel(member.provider)} · {member.account_label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Check for changes
                        <select
                          value={cadenceByPlaylist[playlist.id] ?? 60}
                          onChange={(event) =>
                            setCadenceByPlaylist((current) => ({
                              ...current,
                              [playlist.id]: Number(event.target.value),
                            }))
                          }
                        >
                          {CADENCES.map(([minutes, label]) => (
                            <option key={minutes} value={minutes}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button
                        className="primary"
                        disabled={
                          busyId === playlist.id ||
                          canonicalHasIncomingSync ||
                          writableTargetCount === 0 ||
                          managedCount >= writableTargetCount
                        }
                        onClick={() => void keepEverywhere(playlist)}
                      >
                        <RefreshCw aria-hidden="true" />
                        {busyId === playlist.id
                          ? "Starting..."
                          : managedCount >= writableTargetCount && writableTargetCount > 0
                            ? "Managed everywhere"
                            : "Keep everywhere"}
                      </button>
                    </div>

                    <div className="unified-copy-list" aria-label="Playlist copies">
                      {playlist.members.map((member) => {
                        const isCanonical = member.key === canonicalKey;
                        const managed = playlist.sync_links.some(
                          (link) =>
                            link.source_member_key === canonicalKey &&
                            link.target_member_key === member.key,
                        );
                        return (
                          <div key={member.key}>
                            <ProviderIcon provider={member.provider} />
                            <span>
                              <strong>{providerLabel(member.provider)}</strong>
                              <small>
                                {member.account_label} · {member.track_count} songs
                              </small>
                            </span>
                            <span className="unified-copy-state">
                              {isCanonical ? (
                                <>
                                  <Check aria-hidden="true" /> Source
                                </>
                              ) : managed ? (
                                <>
                                  <RefreshCw aria-hidden="true" /> Sync active
                                </>
                              ) : (
                                "Visible"
                              )}
                            </span>
                          </div>
                        );
                      })}
                    </div>

                    {playlist.sync_attempts.some((attempt) => attempt.status !== "active") ? (
                      <div className="unified-attempt-list" role="status">
                        {playlist.sync_attempts
                          .filter((attempt) => attempt.status !== "active")
                          .map((attempt) => {
                            const account = accounts.find(
                              (item) => item.id === attempt.target_account_id,
                            );
                            return (
                              <p key={attempt.migration_job_id}>
                                <CircleAlert aria-hidden="true" />
                                <span>
                                  <strong>
                                    {account?.display_name ??
                                      providerLabel(attempt.target_provider)}
                                  </strong>{" "}
                                  {attempt.status === "pending"
                                    ? "is being copied. Continuous sync starts after migration review finishes."
                                    : `could not start continuous sync: ${attempt.error ?? "unknown error"}`}
                                </span>
                              </p>
                            );
                          })}
                      </div>
                    ) : null}

                    <div className="unified-track-table" role="table" aria-label={playlist.name}>
                      <div className="unified-track-head" role="row">
                        <span role="columnheader">#</span>
                        <span role="columnheader">Song</span>
                        <span role="columnheader">Album</span>
                        <span role="columnheader">Available on</span>
                      </div>
                      {playlist.tracks.map((track, index) => (
                        <div className="unified-track-row" role="row" key={track.key}>
                          <span role="cell">{index + 1}</span>
                          <span role="cell" className="unified-track-title">
                            {track.artwork_uri ? <img src={track.artwork_uri} alt="" /> : null}
                            <span>
                              <strong>{track.title}</strong>
                              <small>{track.artist}</small>
                            </span>
                          </span>
                          <span role="cell" className="unified-track-album">
                            {track.album ?? "—"}
                          </span>
                          <span role="cell" className="unified-track-providers">
                            {track.sources.map((source) => {
                              const url = source.uri
                                ? providerTrackUrl(source.provider, source.uri)
                                : null;
                              const label = `${providerLabel(source.provider)} · ${source.account_label}`;
                              const icon = <ProviderIcon provider={source.provider} />;
                              return url ? (
                                <a
                                  key={`${source.account_id}:${source.playlist_id}:${source.position}`}
                                  href={url}
                                  target="_blank"
                                  rel="noreferrer"
                                  aria-label={`Open ${track.title} on ${label}`}
                                  title={label}
                                >
                                  {icon}
                                </a>
                              ) : (
                                <span
                                  key={`${source.account_id}:${source.playlist_id}:${source.position}`}
                                  aria-label={label}
                                  title={label}
                                >
                                  {icon}
                                </span>
                              );
                            })}
                            {track.sources.length < playlist.members.length ? (
                              <small>
                                {playlist.members.length - track.sources.length} missing
                              </small>
                            ) : null}
                          </span>
                        </div>
                      ))}
                    </div>

                    {(startedByPlaylist[playlist.id] ?? []).map((migration) => (
                      <section className="unified-progress" key={migration.jobId}>
                        <strong>Copying to {migration.targetLabel}</strong>
                        <ProgressBoard
                          jobId={migration.jobId}
                          onMigrationChanged={async () => {
                            await onMigrationChanged();
                            await load();
                          }}
                        />
                      </section>
                    ))}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function migrationBody(
  playlist: UnifiedPlaylistView,
  canonical: UnifiedPlaylistMemberView,
  targetAccount: AccountView,
  providers: ProviderView[],
  cadenceMinutes: number,
  timezone: string,
): CreateMigrationBody {
  const target = providers.find((provider) => provider.name === targetAccount.provider);
  const mode =
    playlist.kind === "standard" && target?.can_mirror === true ? "mirror" : "add_only";
  return {
    source_provider: canonical.provider,
    source_account_id: canonical.account_id,
    target_provider: targetAccount.provider,
    target_account_id: targetAccount.id,
    selection: {
      playlist_ids: [canonical.playlist_id],
      tracks: {},
      saved_album_ids: [],
      followed_artist_ids: [],
      continuous_sync: {
        mode,
        cadence_minutes: cadenceMinutes,
        timezone,
      },
    },
  };
}

function managedTargetAccounts(
  playlist: UnifiedPlaylistView,
  canonicalKey: string,
): Set<string> {
  const members = new Map(playlist.members.map((member) => [member.key, member]));
  const managed = new Set(
    playlist.sync_links
      .filter((link) => link.source_member_key === canonicalKey)
      .map((link) => members.get(link.target_member_key)?.account_id)
      .filter((accountId): accountId is string => Boolean(accountId)),
  );
  for (const attempt of playlist.sync_attempts) {
    if (attempt.source_member_key === canonicalKey && attempt.status !== "failed") {
      managed.add(attempt.target_account_id);
    }
  }
  return managed;
}

function uniqueConnectedProviders(accounts: AccountView[]): string[] {
  return [...new Set(accounts.map((account) => account.provider))].sort();
}

function uniqueMemberProviders(playlist: UnifiedPlaylistView): string[] {
  return [...new Set(playlist.members.map((member) => member.provider))].sort();
}

function alignmentLabel(alignment: UnifiedPlaylistView["alignment"]): string {
  if (alignment === "aligned") return "Aligned";
  if (alignment === "drifted") return "Differences found";
  return "One provider";
}

function PlaylistArtwork({ playlist }: { playlist: UnifiedPlaylistView }) {
  if (playlist.photo) {
    return <img className="unified-playlist-artwork" src={playlist.photo} alt="" />;
  }
  return (
    <span className="unified-playlist-artwork placeholder" aria-hidden="true">
      <LibraryBig />
    </span>
  );
}

function UnifiedSkeleton() {
  return (
    <div className="unified-skeleton" aria-label="Loading unified playlists">
      {[0, 1, 2].map((item) => (
        <span key={item} />
      ))}
    </div>
  );
}

function isMigrationWarning(
  error: unknown,
): error is ApiError & { detail: MigrationWarningsView } {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  if (!error.detail || typeof error.detail !== "object") return false;
  const detail = error.detail as Partial<MigrationWarningsView>;
  return detail.code === "migration_warnings" && Array.isArray(detail.warnings);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
