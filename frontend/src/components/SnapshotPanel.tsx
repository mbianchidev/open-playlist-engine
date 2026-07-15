import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Archive,
  Download,
  GitCompare,
  HardDrive,
  Play,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";
import {
  cleanupSnapshotProfile,
  createLocalSnapshot,
  createMigration,
  createSnapshotProfile,
  deleteSnapshot,
  deleteSnapshotProfile,
  getPlaylists,
  getSnapshot,
  getSnapshotDiff,
  importSnapshot,
  listSnapshotProfiles,
  listSnapshots,
  preflightMigration,
  snapshotDownloadUrl,
  updateSnapshotProfile,
  verifySnapshot,
} from "../api/client";
import type {
  AccountView,
  CreateMigrationBody,
  PlaylistRef,
  ProviderView,
  SnapshotDetailView,
  SnapshotDiffView,
  SnapshotProfileView,
  SnapshotView,
} from "../api/types";
import { providerLabel } from "../utils/providers";
import ProgressBoard from "./ProgressBoard";
import ProviderIcon from "./ProviderIcon";

interface Props {
  providers: ProviderView[];
  accounts: AccountView[];
  onReconnectProvider: (provider: string) => void;
  onMigrationChanged: () => void | Promise<void>;
}

interface DraftSource {
  provider: string;
  accountId: string;
  accountLabel: string;
  collections: PlaylistRef[];
}

interface RetentionDraft {
  count: string;
  days: string;
}

export default function SnapshotPanel({
  providers,
  accounts,
  onReconnectProvider,
  onMigrationChanged,
}: Props) {
  const [profiles, setProfiles] = useState<SnapshotProfileView[]>([]);
  const [snapshots, setSnapshots] = useState<SnapshotView[]>([]);
  const [totalBytes, setTotalBytes] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [profileName, setProfileName] = useState("");
  const [retentionCount, setRetentionCount] = useState("10");
  const [retentionDays, setRetentionDays] = useState("90");
  const [sourceAccountId, setSourceAccountId] = useState("");
  const [sourceCollections, setSourceCollections] = useState<PlaylistRef[]>([]);
  const [selectedSourceCollections, setSelectedSourceCollections] = useState<Set<string>>(
    new Set(),
  );
  const [sourceLoading, setSourceLoading] = useState(false);
  const [draftSources, setDraftSources] = useState<DraftSource[]>([]);
  const [retentionDrafts, setRetentionDrafts] = useState<Record<string, RetentionDraft>>({});
  const [importFile, setImportFile] = useState<File | null>(null);
  const [diff, setDiff] = useState<SnapshotDiffView | null>(null);
  const [diffSnapshotId, setDiffSnapshotId] = useState<string | null>(null);
  const [restoreSnapshot, setRestoreSnapshot] = useState<SnapshotDetailView | null>(null);
  const [restoreCollections, setRestoreCollections] = useState<Set<string>>(new Set());
  const [restoreTargetProvider, setRestoreTargetProvider] = useState("");
  const [restoreTargetAccountId, setRestoreTargetAccountId] = useState("");
  const [restoreJobId, setRestoreJobId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [profileRows, snapshotRows] = await Promise.all([
      listSnapshotProfiles(),
      listSnapshots(),
    ]);
    setProfiles(profileRows);
    setSnapshots(snapshotRows.snapshots);
    setTotalBytes(snapshotRows.total_bytes);
    setRetentionDrafts((current) =>
      Object.fromEntries(
        profileRows.map((profile) => [
          profile.id,
          current[profile.id] ?? {
            count: profile.retention_count?.toString() ?? "",
            days: profile.retention_days?.toString() ?? "",
          },
        ]),
      ),
    );
  }, []);

  useEffect(() => {
    void refresh().catch((caught: unknown) => setError(errorMessage(caught)));
  }, [refresh]);

  const hasActiveSnapshot = snapshots.some((snapshot) =>
    ["pending", "running"].includes(snapshot.status),
  );

  useEffect(() => {
    if (!hasActiveSnapshot) return;
    const timer = window.setTimeout(() => {
      void refresh().catch((caught: unknown) => setError(errorMessage(caught)));
    }, 2_000);
    return () => window.clearTimeout(timer);
  }, [hasActiveSnapshot, refresh, snapshots]);

  useEffect(() => {
    if (!sourceAccountId) {
      setSourceCollections([]);
      setSelectedSourceCollections(new Set());
      return;
    }
    const account = accounts.find((item) => item.id === sourceAccountId);
    if (!account) return;
    let cancelled = false;
    setSourceLoading(true);
    setError(null);
    void getPlaylists(account.provider, account.id)
      .then((rows) => {
        if (cancelled) return;
        setSourceCollections(rows);
        setSelectedSourceCollections(new Set(rows.map((row) => row.id)));
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(errorMessage(caught));
      })
      .finally(() => {
        if (!cancelled) setSourceLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accounts, sourceAccountId]);

  const targetProviders = useMemo(
    () => providers.filter((provider) => provider.can_target),
    [providers],
  );
  const targetAccounts = accounts.filter(
    (account) => account.provider === restoreTargetProvider,
  );

  useEffect(() => {
    if (!restoreTargetProvider) {
      setRestoreTargetAccountId("");
      return;
    }
    const matching = accounts.filter((account) => account.provider === restoreTargetProvider);
    if (!matching.some((account) => account.id === restoreTargetAccountId)) {
      setRestoreTargetAccountId(matching[0]?.id ?? "");
    }
  }, [accounts, restoreTargetAccountId, restoreTargetProvider]);

  function toggleSourceCollection(collectionId: string) {
    setSelectedSourceCollections((current) => {
      const next = new Set(current);
      if (next.has(collectionId)) next.delete(collectionId);
      else next.add(collectionId);
      return next;
    });
  }

  function addDraftSource() {
    const account = accounts.find((item) => item.id === sourceAccountId);
    if (!account) {
      setError("Choose a connected source account.");
      return;
    }
    if (draftSources.some((source) => source.accountId === account.id)) {
      setError("That account is already included in this profile.");
      return;
    }
    const selected = sourceCollections.filter((collection) =>
      selectedSourceCollections.has(collection.id),
    );
    if (selected.length === 0) {
      setError("Select at least one collection from this account.");
      return;
    }
    setDraftSources((current) => [
      ...current,
      {
        provider: account.provider,
        accountId: account.id,
        accountLabel: account.display_name ?? providerLabel(account.provider),
        collections: selected,
      },
    ]);
    setSourceAccountId("");
    setSourceCollections([]);
    setSelectedSourceCollections(new Set());
    setError(null);
  }

  async function saveProfile() {
    if (!profileName.trim()) {
      setError("Name the snapshot profile.");
      return;
    }
    if (draftSources.length === 0) {
      setError("Add at least one account and collection.");
      return;
    }
    await runAction("create-profile", async () => {
      await createSnapshotProfile({
        name: profileName.trim(),
        sources: draftSources.map((source) => ({
          provider: source.provider,
          account_id: source.accountId,
          collection_ids: source.collections.map((collection) => collection.id),
        })),
        retention_count: positiveNumber(retentionCount),
        retention_days: positiveNumber(retentionDays),
      });
      setProfileName("");
      setDraftSources([]);
      setNotice("Snapshot profile created.");
      await refresh();
    });
  }

  async function saveRetention(profile: SnapshotProfileView) {
    const draft = retentionDrafts[profile.id];
    if (!draft) return;
    await runAction(`retention-${profile.id}`, async () => {
      await updateSnapshotProfile(profile.id, {
        retention_count: positiveNumber(draft.count),
        retention_days: positiveNumber(draft.days),
      });
      setNotice(`Retention saved for ${profile.name}.`);
      await refresh();
    });
  }

  async function createNow(profile: SnapshotProfileView) {
    await runAction(`create-${profile.id}`, async () => {
      await createLocalSnapshot(profile.id);
      setNotice(`Snapshot started for ${profile.name}.`);
      await refresh();
    });
  }

  async function cleanup(profile: SnapshotProfileView) {
    await runAction(`cleanup-${profile.id}`, async () => {
      const result = await cleanupSnapshotProfile(profile.id);
      setNotice(
        result.deleted_count
          ? `Deleted ${result.deleted_count} retained snapshot${result.deleted_count === 1 ? "" : "s"} (${formatBytes(result.deleted_bytes)}).`
          : "Retention is already satisfied.",
      );
      await refresh();
    });
  }

  async function removeProfile(profile: SnapshotProfileView) {
    if (
      !confirm(
        `Delete the profile "${profile.name}"? Existing snapshot archives remain in history.`,
      )
    ) {
      return;
    }
    await runAction(`delete-profile-${profile.id}`, async () => {
      await deleteSnapshotProfile(profile.id);
      setNotice("Snapshot profile deleted. Existing archives were kept.");
      await refresh();
    });
  }

  async function verify(row: SnapshotView) {
    await runAction(`verify-${row.id}`, async () => {
      await verifySnapshot(row.id);
      setNotice("Snapshot integrity verified.");
      await refresh();
    });
  }

  async function compare(row: SnapshotView, previous: SnapshotView) {
    await runAction(`diff-${row.id}`, async () => {
      const result = await getSnapshotDiff(row.id, previous.id);
      setDiff(result);
      setDiffSnapshotId(row.id);
    });
  }

  async function removeSnapshot(row: SnapshotView) {
    if (!confirm(`Delete this ${formatDate(row.created_at)} snapshot archive?`)) return;
    await runAction(`delete-${row.id}`, async () => {
      await deleteSnapshot(row.id);
      if (restoreSnapshot?.id === row.id) setRestoreSnapshot(null);
      setNotice("Snapshot deleted.");
      await refresh();
    });
  }

  async function importArchive() {
    if (!importFile) {
      setError("Choose an Open Playlist snapshot archive.");
      return;
    }
    if (
      !confirm(
        "Import this local archive? Its manifest, checksums, schema, paths, and metadata will be verified first.",
      )
    ) {
      return;
    }
    await runAction("import", async () => {
      await importSnapshot(importFile);
      setImportFile(null);
      setNotice("Portable snapshot imported and verified.");
      await refresh();
    });
  }

  async function openRestore(row: SnapshotView) {
    await runAction(`restore-open-${row.id}`, async () => {
      const detail = await getSnapshot(row.id);
      if (!detail.manifest) throw new Error("Snapshot manifest is unavailable.");
      setRestoreSnapshot(detail);
      setRestoreCollections(
        new Set(detail.manifest.collections.map((collection) => collection.id)),
      );
      const firstTarget = targetProviders.find((provider) =>
        accounts.some((account) => account.provider === provider.name),
      );
      setRestoreTargetProvider(firstTarget?.name ?? "");
      setRestoreJobId(null);
    });
  }

  function toggleRestoreCollection(collectionId: string) {
    setRestoreCollections((current) => {
      const next = new Set(current);
      if (next.has(collectionId)) next.delete(collectionId);
      else next.add(collectionId);
      return next;
    });
  }

  async function startRestore() {
    if (!restoreSnapshot || !restoreTargetProvider || !restoreTargetAccountId) {
      setError("Choose a snapshot, target provider, and connected target account.");
      return;
    }
    if (restoreCollections.size === 0) {
      setError("Select at least one collection to restore.");
      return;
    }
    const body: CreateMigrationBody = {
      source_snapshot_id: restoreSnapshot.id,
      target_provider: restoreTargetProvider,
      target_account_id: restoreTargetAccountId,
      selection: { playlist_ids: [...restoreCollections], tracks: {} },
    };
    await runAction(`restore-${restoreSnapshot.id}`, async () => {
      const preflight = await preflightMigration(body);
      if (
        preflight.warnings.length > 0 &&
        !confirm(
          `Review before restoring:\n\n${preflight.warnings.map((warning) => `• ${warning.message}`).join("\n")}`,
        )
      ) {
        return;
      }
      const job = await createMigration({ ...body, acknowledge_warnings: true });
      setRestoreJobId(job.id);
      setNotice("Snapshot restore started through the migration review pipeline.");
      await onMigrationChanged();
    });
  }

  async function runAction(key: string, action: () => Promise<void>) {
    setBusyKey(key);
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (caught: unknown) {
      setError(errorMessage(caught));
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <div className="snapshot-workspace">
      {error ? <p className="warn snapshot-message">⚠ {error}</p> : null}
      {notice ? <p className="notice snapshot-message">{notice}</p> : null}

      <section className="card snapshot-hero">
        <div>
          <p className="eyebrow">Local archive</p>
          <h2>Keep a portable record of your music library</h2>
          <p className="muted">
            Metadata stays on this instance. Bundles contain no tokens, auth headers, or audio.
          </p>
        </div>
        <div className="snapshot-hero-metrics" aria-label="Snapshot storage summary">
          <span>
            <strong>{snapshots.length}</strong>
            archives
          </span>
          <span>
            <strong>{formatBytes(totalBytes)}</strong>
            local storage
          </span>
          <span>
            <ShieldCheck aria-hidden="true" />
            checksummed
          </span>
        </div>
      </section>

      <section className="card flow snapshot-builder">
        <div className="section-heading">
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <Plus />
            </span>
            <div>
              <h2>Create a snapshot profile</h2>
              <p className="muted">Choose one or more accounts and the collections to preserve.</p>
            </div>
          </div>
        </div>

        <div className="snapshot-builder-grid">
          <label className="snapshot-field">
            Profile name
            <input
              value={profileName}
              onChange={(event) => setProfileName(event.target.value)}
              placeholder="Monthly library backup"
            />
          </label>
          <label className="snapshot-field">
            Connected source account
            <select
              value={sourceAccountId}
              onChange={(event) => setSourceAccountId(event.target.value)}
            >
              <option value="">Choose an account</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {providerLabel(account.provider)} ·{" "}
                  {account.display_name ?? account.provider_user_id ?? "Connected account"}
                </option>
              ))}
            </select>
          </label>
          <label className="snapshot-field">
            Keep newest
            <span className="snapshot-number-field">
              <input
                type="number"
                min="1"
                value={retentionCount}
                onChange={(event) => setRetentionCount(event.target.value)}
              />
              <small>snapshots</small>
            </span>
          </label>
          <label className="snapshot-field">
            Maximum age
            <span className="snapshot-number-field">
              <input
                type="number"
                min="1"
                value={retentionDays}
                onChange={(event) => setRetentionDays(event.target.value)}
              />
              <small>days</small>
            </span>
          </label>
        </div>

        {sourceAccountId ? (
          <div className="snapshot-source-picker">
            <div className="snapshot-source-picker-heading">
              <strong>Select collections</strong>
              <span className="muted">
                {selectedSourceCollections.size} of {sourceCollections.length} selected
              </span>
            </div>
            {sourceLoading ? (
              <p className="muted">Loading library collections…</p>
            ) : sourceCollections.length === 0 ? (
              <p className="muted">No playlist or liked-track collections were found.</p>
            ) : (
              <div className="snapshot-collection-picker">
                {sourceCollections.map((collection) => (
                  <label key={collection.id}>
                    <input
                      type="checkbox"
                      checked={selectedSourceCollections.has(collection.id)}
                      onChange={() => toggleSourceCollection(collection.id)}
                    />
                    <span>{collection.name}</span>
                    {collection.kind === "liked_tracks" ? (
                      <span className="badge">Liked tracks</span>
                    ) : null}
                    <small>{collection.track_count ?? "?"} items</small>
                  </label>
                ))}
              </div>
            )}
            <button
              className="secondary compact"
              type="button"
              disabled={sourceLoading || selectedSourceCollections.size === 0}
              onClick={addDraftSource}
            >
              <Plus aria-hidden="true" />
              Add account to profile
            </button>
          </div>
        ) : null}

        {draftSources.length > 0 ? (
          <div className="snapshot-draft-sources">
            {draftSources.map((source) => (
              <article key={source.accountId}>
                <ProviderIcon provider={source.provider} className="provider-icon-inline" />
                <div>
                  <strong>{source.accountLabel}</strong>
                  <small>
                    {providerLabel(source.provider)} · {source.collections.length} collection
                    {source.collections.length === 1 ? "" : "s"}
                  </small>
                </div>
                <button
                  type="button"
                  className="secondary compact"
                  onClick={() =>
                    setDraftSources((current) =>
                      current.filter((item) => item.accountId !== source.accountId),
                    )
                  }
                >
                  Remove
                </button>
              </article>
            ))}
          </div>
        ) : null}

        <button
          className="primary"
          type="button"
          disabled={busyKey === "create-profile" || !profileName.trim() || draftSources.length === 0}
          onClick={() => void saveProfile()}
        >
          <Save aria-hidden="true" />
          {busyKey === "create-profile" ? "Creating…" : "Create profile"}
        </button>
      </section>

      <section className="card flow snapshot-profiles-section">
        <div className="section-heading">
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <Archive />
            </span>
            <div>
              <h2>Backup profiles</h2>
              <p className="muted">Run a snapshot now or adjust deterministic retention.</p>
            </div>
          </div>
          <button className="secondary compact" type="button" onClick={() => void refresh()}>
            <RefreshCw aria-hidden="true" />
            Refresh
          </button>
        </div>
        {profiles.length === 0 ? (
          <p className="empty-guidance">Create a profile above to start local snapshots.</p>
        ) : (
          <div className="snapshot-profile-grid">
            {profiles.map((profile) => {
              const retention = retentionDrafts[profile.id] ?? { count: "", days: "" };
              return (
                <article key={profile.id} className="snapshot-profile-card">
                  <div className="snapshot-profile-title">
                    <div>
                      <h3>{profile.name}</h3>
                      <p className="muted">
                        {profile.sources.length} account{profile.sources.length === 1 ? "" : "s"} ·{" "}
                        {profile.sources.reduce(
                          (total, source) => total + source.collection_ids.length,
                          0,
                        )}{" "}
                        collections · {profile.snapshot_count} snapshots
                      </p>
                    </div>
                    <button
                      type="button"
                      className="secondary compact danger-action"
                      onClick={() => void removeProfile(profile)}
                    >
                      <Trash2 aria-hidden="true" />
                      Delete
                    </button>
                  </div>
                  <div className="snapshot-profile-sources">
                    {profile.sources.map((source) => (
                      <span key={source.id}>
                        <ProviderIcon provider={source.provider} className="provider-icon-inline" />
                        {source.account_label ?? providerLabel(source.provider)}
                        <small>{source.collection_ids.length}</small>
                      </span>
                    ))}
                  </div>
                  <div className="snapshot-retention-row">
                    <label>
                      Keep
                      <input
                        type="number"
                        min="1"
                        value={retention.count}
                        onChange={(event) =>
                          setRetentionDrafts((current) => ({
                            ...current,
                            [profile.id]: { ...retention, count: event.target.value },
                          }))
                        }
                      />
                    </label>
                    <label>
                      Days
                      <input
                        type="number"
                        min="1"
                        value={retention.days}
                        onChange={(event) =>
                          setRetentionDrafts((current) => ({
                            ...current,
                            [profile.id]: { ...retention, days: event.target.value },
                          }))
                        }
                      />
                    </label>
                    <button
                      type="button"
                      className="secondary compact"
                      onClick={() => void saveRetention(profile)}
                    >
                      <Save aria-hidden="true" />
                      Save
                    </button>
                  </div>
                  <div className="toolbar">
                    <button
                      className="primary"
                      type="button"
                      disabled={busyKey === `create-${profile.id}`}
                      onClick={() => void createNow(profile)}
                    >
                      <Archive aria-hidden="true" />
                      {busyKey === `create-${profile.id}` ? "Starting…" : "Create snapshot"}
                    </button>
                    <button
                      className="secondary"
                      type="button"
                      onClick={() => void cleanup(profile)}
                    >
                      Clean up now
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="card flow snapshot-import">
        <div className="section-title">
          <span className="section-icon" aria-hidden="true">
            <Upload />
          </span>
          <div>
            <h2>Import a portable snapshot</h2>
            <p className="muted">
              Move an <code>.opb</code> archive from another self-hosted instance.
            </p>
          </div>
        </div>
        <div className="snapshot-import-controls">
          <input
            type="file"
            accept=".opb,.zip,application/zip,application/octet-stream"
            onChange={(event) => setImportFile(event.target.files?.[0] ?? null)}
          />
          <button
            className="secondary"
            type="button"
            disabled={!importFile || busyKey === "import"}
            onClick={() => void importArchive()}
          >
            <Upload aria-hidden="true" />
            {busyKey === "import" ? "Verifying…" : "Import and verify"}
          </button>
        </div>
      </section>

      <section className="card flow snapshot-history-section">
        <div className="section-heading">
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <HardDrive />
            </span>
            <div>
              <h2>Snapshot history</h2>
              <p className="muted">Each archive is versioned, checksummed, and portable.</p>
            </div>
          </div>
          <strong className="snapshot-storage-total">{formatBytes(totalBytes)}</strong>
        </div>
        {snapshots.length === 0 ? (
          <p className="empty-guidance">No local snapshots yet.</p>
        ) : (
          <div className="snapshot-history">
            {snapshots.map((row, index) => {
              const previous = snapshots
                .slice(index + 1)
                .find((candidate) => candidate.library_id === row.library_id);
              return (
                <article key={row.id} className="snapshot-history-item">
                  <div className={`snapshot-seal snapshot-seal-${row.status}`} aria-hidden="true">
                    {row.status === "complete" ? <ShieldCheck /> : <Archive />}
                  </div>
                  <div className="snapshot-history-card">
                    <div className="snapshot-history-heading">
                      <div>
                        <p className="eyebrow">{row.profile_name ?? "Imported archive"}</p>
                        <h3>{formatDate(row.created_at)}</h3>
                      </div>
                      <span className={`status status-${row.status}`}>{statusLabel(row.status)}</span>
                    </div>
                    <div className="snapshot-facts">
                      <span>
                        <strong>{row.counts.collections}</strong> collections
                      </span>
                      <span>
                        <strong>{row.counts.items}</strong> items
                      </span>
                      <span>
                        <strong>{formatBytes(row.size_bytes)}</strong> archive
                      </span>
                      <span>
                        <strong>v{row.schema_version}</strong> schema
                      </span>
                    </div>
                    <p className="muted snapshot-source-line">
                      {row.source_labels.length > 0
                        ? row.source_labels.join(", ")
                        : row.source_providers.map(providerLabel).join(", ")}
                    </p>
                    {row.counts.failed_collections > 0 ? (
                      <p className="warn">
                        {row.counts.failed_collections} collection
                        {row.counts.failed_collections === 1 ? "" : "s"} were only partially
                        captured.
                      </p>
                    ) : null}
                    {row.verification_error ? (
                      <p className="warn">{row.verification_error}</p>
                    ) : null}
                    <div className="toolbar snapshot-actions">
                      <button
                        className="secondary compact"
                        type="button"
                        disabled={!["complete", "partial"].includes(row.status)}
                        onClick={() => void verify(row)}
                      >
                        <ShieldCheck aria-hidden="true" />
                        Verify
                      </button>
                      <a
                        className="button-link compact"
                        href={snapshotDownloadUrl(row.id)}
                        aria-disabled={!["complete", "partial"].includes(row.status)}
                      >
                        <Download aria-hidden="true" />
                        Download
                      </a>
                      {previous ? (
                        <button
                          className="secondary compact"
                          type="button"
                          onClick={() => void compare(row, previous)}
                        >
                          <GitCompare aria-hidden="true" />
                          Compare
                        </button>
                      ) : null}
                      <button
                        className="secondary compact"
                        type="button"
                        disabled={!["complete", "partial"].includes(row.status)}
                        onClick={() => void openRestore(row)}
                      >
                        <Play aria-hidden="true" />
                        Restore
                      </button>
                      <button
                        className="secondary compact danger-action"
                        type="button"
                        disabled={["pending", "running"].includes(row.status)}
                        onClick={() => void removeSnapshot(row)}
                      >
                        <Trash2 aria-hidden="true" />
                        Delete
                      </button>
                    </div>
                    {diffSnapshotId === row.id && diff ? <DiffSummary diff={diff} /> : null}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      {restoreSnapshot?.manifest ? (
        <section className="card flow snapshot-restore">
          <div className="section-heading">
            <div className="section-title">
              <span className="section-icon" aria-hidden="true">
                <Play />
              </span>
              <div>
                <h2>Restore {restoreSnapshot.profile_name ?? "imported snapshot"}</h2>
                <p className="muted">
                  Select collections, then use the existing preflight, match, review, and write
                  flow.
                </p>
              </div>
            </div>
            <button
              type="button"
              className="secondary compact"
              onClick={() => {
                setRestoreSnapshot(null);
                setRestoreJobId(null);
              }}
            >
              Close
            </button>
          </div>
          <div className="snapshot-restore-route">
            <label className="snapshot-field">
              Target provider
              <select
                value={restoreTargetProvider}
                onChange={(event) => setRestoreTargetProvider(event.target.value)}
              >
                <option value="">Choose a target</option>
                {targetProviders.map((provider) => (
                  <option key={provider.name} value={provider.name}>
                    {provider.display_name}
                  </option>
                ))}
              </select>
            </label>
            <label className="snapshot-field">
              Connected target account
              <select
                value={restoreTargetAccountId}
                onChange={(event) => setRestoreTargetAccountId(event.target.value)}
              >
                <option value="">Choose an account</option>
                {targetAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.display_name ?? account.provider_user_id ?? "Connected account"}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="snapshot-restore-collections">
            {restoreSnapshot.manifest.collections.map((collection) => (
              <label key={collection.id}>
                <input
                  type="checkbox"
                  checked={restoreCollections.has(collection.id)}
                  onChange={() => toggleRestoreCollection(collection.id)}
                />
                <span>
                  <strong>{collection.name}</strong>
                  <small>
                    {providerLabel(collection.source_provider)} · {collection.item_count} items
                  </small>
                </span>
                {collection.kind === "liked_tracks" ? (
                  <span className="badge">Liked tracks</span>
                ) : null}
                {!collection.complete ? <span className="status status-partial">Partial</span> : null}
              </label>
            ))}
          </div>
          <button
            className="primary"
            type="button"
            disabled={
              !restoreTargetProvider ||
              !restoreTargetAccountId ||
              restoreCollections.size === 0 ||
              busyKey === `restore-${restoreSnapshot.id}`
            }
            onClick={() => void startRestore()}
          >
            <Play aria-hidden="true" />
            {busyKey === `restore-${restoreSnapshot.id}` ? "Checking…" : "Preflight and restore"}
          </button>
          {restoreJobId ? (
            <ProgressBoard
              className="progress-popover snapshot-restore-progress"
              jobId={restoreJobId}
              onMigrationChanged={async () => {
                await refresh();
                await onMigrationChanged();
              }}
              onReconnectProvider={onReconnectProvider}
            />
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

function DiffSummary({ diff }: { diff: SnapshotDiffView }) {
  return (
    <div className="snapshot-diff">
      <strong>Changes from the previous version</strong>
      <div>
        <span>+{diff.added.length} collections</span>
        <span>−{diff.removed.length} collections</span>
        <span>{diff.renamed.length} renamed</span>
        <span>{diff.changed.length} changed</span>
        <span>+{diff.items_added} items</span>
        <span>−{diff.items_removed} items</span>
      </div>
      {[...diff.added, ...diff.removed, ...diff.renamed, ...diff.changed].length > 0 ? (
        <p className="muted">
          {[...new Set([...diff.added, ...diff.removed, ...diff.renamed, ...diff.changed].map((item) => item.name))]
            .slice(0, 6)
            .join(", ")}
        </p>
      ) : (
        <p className="muted">No represented metadata changed.</p>
      )}
    </div>
  );
}

function positiveNumber(value: string): number | null {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

function formatDate(value: string | null): string {
  if (!value) return "Preparing snapshot";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  if (value < 1_073_741_824) return `${(value / 1_048_576).toFixed(1)} MB`;
  return `${(value / 1_073_741_824).toFixed(2)} GB`;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
