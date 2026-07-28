import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CalendarClock,
  Pause,
  Play,
  RefreshCw,
  Repeat2,
  Save,
  Trash2,
} from "lucide-react";
import {
  createSyncRule,
  deleteSyncRule,
  listMigrations,
  listSyncRules,
  pauseSyncRule,
  resumeSyncRule,
  runSyncRule,
  updateSyncRule,
} from "../api/client";
import type {
  MigrationOptionView,
  ProviderView,
  SyncMode,
  SyncRuleView,
  UpdateSyncRuleBody,
} from "../api/types";
import { providerLabel } from "../utils/providers";
import ProgressBoard from "./ProgressBoard";
import ProviderIcon from "./ProviderIcon";

interface Props {
  providers: ProviderView[];
  onReconnectProvider?: (provider: string) => void | Promise<void>;
}

const CADENCES = [
  [15, "Every 15 minutes"],
  [60, "Hourly"],
  [360, "Every 6 hours"],
  [1440, "Daily"],
  [10080, "Weekly"],
] as const;

export default function SyncPanel({ providers, onReconnectProvider }: Props) {
  const [rules, setRules] = useState<SyncRuleView[]>([]);
  const [migrations, setMigrations] = useState<MigrationOptionView[]>([]);
  const [selectedMigrationId, setSelectedMigrationId] = useState("");
  const [mode, setMode] = useState<SyncMode>("add_only");
  const [cadenceMinutes, setCadenceMinutes] = useState(60);
  const [timezone, setTimezone] = useState(defaultTimezone);
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const candidates = useMemo(
    () =>
      migrations.filter(
        (migration) => migration.status === "done" && migration.playlist_names.length === 1,
      ),
    [migrations],
  );
  const selectedMigration =
    candidates.find((migration) => migration.id === selectedMigrationId) ?? null;
  const selectedTarget = providers.find(
    (provider) => provider.name === selectedMigration?.target_provider,
  );
  const hasActiveRun = rules.some((rule) =>
    ["queued", "running"].includes(rule.latest_run?.status ?? rule.status),
  );

  const loadRules = useCallback(async () => {
    const nextRules = await listSyncRules();
    setRules(nextRules);
    return nextRules;
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [nextRules, nextMigrations] = await Promise.all([
        listSyncRules(),
        listMigrations(),
      ]);
      setRules(nextRules);
      setMigrations(nextMigrations);
      const nextCandidates = nextMigrations.filter(
        (migration) => migration.status === "done" && migration.playlist_names.length === 1,
      );
      setSelectedMigrationId((current) =>
        nextCandidates.some((migration) => migration.id === current)
          ? current
          : nextCandidates[0]?.id ?? "",
      );
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (!hasActiveRun) return;
    const timer = window.setInterval(() => {
      void loadRules().catch((e: unknown) => setError(errorMessage(e)));
    }, 5000);
    return () => window.clearInterval(timer);
  }, [hasActiveRun, loadRules]);

  useEffect(() => {
    if (mode === "mirror" && selectedTarget && !selectedTarget.can_mirror) {
      setMode("add_only");
    }
  }, [mode, selectedTarget]);

  async function createRule() {
    if (!selectedMigration) return;
    await mutate("Sync rule created.", async () => {
      await createSyncRule({
        migration_job_id: selectedMigration.id,
        mode,
        cadence_minutes: cadenceMinutes,
        timezone: timezone.trim(),
      });
      await loadAll();
    });
  }

  async function mutate(message: string, action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      setNotice(message);
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="sync-workspace">
      <section className="card flow sync-create-card">
        <div className="section-heading">
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <Repeat2 />
            </span>
            <div>
              <h2>Create recurring sync</h2>
              <p className="muted">
                Start from a completed full-playlist migration and keep its target updated.
              </p>
            </div>
          </div>
          <button className="secondary compact" disabled={loading || busy} onClick={loadAll}>
            <RefreshCw aria-hidden="true" />
            Refresh
          </button>
        </div>
        {error ? <p className="warn">{error}</p> : null}
        {notice ? <p className="notice">{notice}</p> : null}
        {loading && candidates.length === 0 ? (
          <p className="muted">Loading completed migrations...</p>
        ) : candidates.length === 0 ? (
          <p className="empty-guidance">
            Complete a full single-playlist migration before creating a sync rule.
          </p>
        ) : (
          <>
            <div className="sync-create-grid">
              <label className="sync-field" htmlFor="syncMigration">
                Completed migration
                <select
                  id="syncMigration"
                  value={selectedMigrationId}
                  onChange={(event) => setSelectedMigrationId(event.target.value)}
                >
                  {candidates.map((migration) => (
                    <option key={migration.id} value={migration.id}>
                      {migration.playlist_names[0]} · {providerLabel(migration.source_provider)} to{" "}
                      {providerLabel(migration.target_provider)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="sync-field" htmlFor="syncMode">
                Mode
                <select
                  id="syncMode"
                  value={mode}
                  onChange={(event) => setMode(event.target.value as SyncMode)}
                >
                  <option value="add_only">Add only</option>
                  <option value="mirror" disabled={!selectedTarget?.can_mirror}>
                    Mirror
                  </option>
                </select>
              </label>
              <label className="sync-field" htmlFor="syncCadence">
                Cadence
                <select
                  id="syncCadence"
                  value={cadenceMinutes}
                  onChange={(event) => setCadenceMinutes(Number(event.target.value))}
                >
                  {CADENCES.map(([minutes, label]) => (
                    <option key={minutes} value={minutes}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="sync-field" htmlFor="syncTimezone">
                Timezone
                <input
                  id="syncTimezone"
                  value={timezone}
                  onChange={(event) => setTimezone(event.target.value)}
                  placeholder="America/Los_Angeles"
                />
              </label>
            </div>
            {mode === "add_only" ? (
              <p className="sync-mode-note">
                New source tracks are added; target-only tracks and ordering stay untouched.
              </p>
            ) : null}
            {selectedTarget && !selectedTarget.can_mirror ? (
              <p className="sync-mode-note warn">
                {selectedTarget.mirror_unavailable_reason ??
                  `${selectedTarget.display_name} cannot mirror playlists.`}
              </p>
            ) : null}
            <button
              className="primary"
              disabled={!selectedMigrationId || !timezone.trim() || busy}
              onClick={createRule}
            >
              <Repeat2 aria-hidden="true" />
              {busy ? "Creating..." : "Create sync"}
            </button>
          </>
        )}
      </section>

      <section className="card flow sync-list-card">
        <div className="section-heading">
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <CalendarClock />
            </span>
            <div>
              <h2>Scheduled syncs</h2>
              <p className="muted">{rules.length} persisted rule{rules.length === 1 ? "" : "s"}</p>
            </div>
          </div>
        </div>
        {rules.length === 0 && !loading ? (
          <p className="empty-guidance">No scheduled syncs yet.</p>
        ) : (
          <div className="sync-rule-list">
            {rules.map((rule) => (
              <SyncRuleCard
                key={rule.id}
                rule={rule}
                providers={providers}
                busy={busy}
                expandedJobId={expandedJobId}
                onToggleJob={(jobId) =>
                  setExpandedJobId((current) => (current === jobId ? null : jobId))
                }
                onReconnectProvider={onReconnectProvider}
                onRefresh={async () => {
                  await loadRules();
                }}
                onUpdate={(body) =>
                  mutate("Sync settings saved.", async () => {
                    await updateSyncRule(rule.id, body);
                    await loadRules();
                  })
                }
                onRun={() =>
                  mutate("Sync run queued.", async () => {
                    await runSyncRule(rule.id);
                    await loadRules();
                  })
                }
                onPause={() =>
                  mutate("Sync paused.", async () => {
                    await pauseSyncRule(rule.id);
                    await loadRules();
                  })
                }
                onResume={() =>
                  mutate("Sync resumed.", async () => {
                    await resumeSyncRule(rule.id);
                    await loadRules();
                  })
                }
                onDelete={() =>
                  mutate("Sync deleted.", async () => {
                    await deleteSyncRule(rule.id);
                    await loadAll();
                  })
                }
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

interface SyncRuleCardProps {
  rule: SyncRuleView;
  providers: ProviderView[];
  busy: boolean;
  expandedJobId: string | null;
  onToggleJob: (jobId: string) => void;
  onReconnectProvider?: (provider: string) => void | Promise<void>;
  onRefresh: () => Promise<void>;
  onUpdate: (body: UpdateSyncRuleBody) => Promise<void>;
  onRun: () => Promise<void>;
  onPause: () => Promise<void>;
  onResume: () => Promise<void>;
  onDelete: () => Promise<void>;
}

function SyncRuleCard({
  rule,
  providers,
  busy,
  expandedJobId,
  onToggleJob,
  onReconnectProvider,
  onRefresh,
  onUpdate,
  onRun,
  onPause,
  onResume,
  onDelete,
}: SyncRuleCardProps) {
  const [mode, setMode] = useState<SyncMode>(rule.mode);
  const [cadenceMinutes, setCadenceMinutes] = useState(rule.cadence_minutes);
  const [timezone, setTimezone] = useState(rule.timezone);
  const target = providers.find((provider) => provider.name === rule.target_provider);
  const jobId = rule.latest_run?.migration_job_id;
  const active = ["queued", "running"].includes(rule.latest_run?.status ?? rule.status);

  useEffect(() => {
    setMode(rule.mode);
    setCadenceMinutes(rule.cadence_minutes);
    setTimezone(rule.timezone);
  }, [rule.mode, rule.cadence_minutes, rule.timezone]);

  return (
    <article className="sync-rule">
      <div className="sync-rule-heading">
        <div className="sync-route">
          <ProviderIcon provider={rule.source_provider} />
          <div>
            <span className="muted">{providerLabel(rule.source_provider)}</span>
            <strong>{rule.source_playlist_name}</strong>
          </div>
          <span className="sync-route-arrow" aria-hidden="true">
            →
          </span>
          <ProviderIcon provider={rule.target_provider} />
          <div>
            <span className="muted">{providerLabel(rule.target_provider)}</span>
            <strong>{rule.target_playlist_name}</strong>
          </div>
        </div>
        <span className={`badge sync-status sync-status-${rule.status}`}>
          {statusLabel(rule.status)}
        </span>
      </div>

      <div className="sync-metrics">
        <SyncMetric label="Added" value={rule.last_added} />
        <SyncMetric label="Removed" value={rule.last_removed} />
        <SyncMetric label="Reordered" value={rule.last_reordered} />
        <SyncMetric label="Last success" value={formatDate(rule.last_success_at, rule.timezone)} />
        <SyncMetric
          label={rule.enabled ? "Next run" : "Schedule"}
          value={rule.enabled ? formatDate(rule.next_run_at, rule.timezone) : "Paused"}
        />
      </div>

      {rule.last_error ? <p className="sync-error">{rule.last_error}</p> : null}
      <div className="sync-edit-grid">
        <label className="sync-field">
          Mode
          <select value={mode} onChange={(event) => setMode(event.target.value as SyncMode)}>
            <option value="add_only">Add only</option>
            <option value="mirror" disabled={!target?.can_mirror}>
              Mirror
            </option>
          </select>
        </label>
        <label className="sync-field">
          Cadence
          <select
            value={cadenceMinutes}
            onChange={(event) => setCadenceMinutes(Number(event.target.value))}
          >
            {CADENCES.map(([minutes, label]) => (
              <option key={minutes} value={minutes}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="sync-field">
          Timezone
          <input value={timezone} onChange={(event) => setTimezone(event.target.value)} />
        </label>
        <button
          className="secondary compact sync-save"
          disabled={busy || active || !timezone.trim()}
          onClick={() => onUpdate({ mode, cadence_minutes: cadenceMinutes, timezone })}
        >
          <Save aria-hidden="true" />
          Save
        </button>
      </div>
      {mode === "mirror" && target && !target.can_mirror ? (
        <p className="sync-mode-note warn">{target.mirror_unavailable_reason}</p>
      ) : null}

      <div className="toolbar sync-actions">
        <button className="primary" disabled={busy || active} onClick={onRun}>
          <Play aria-hidden="true" />
          {active ? "Running..." : "Run now"}
        </button>
        {rule.enabled ? (
          <button className="secondary compact" disabled={busy} onClick={onPause}>
            <Pause aria-hidden="true" />
            Pause
          </button>
        ) : (
          <button className="secondary compact" disabled={busy || active} onClick={onResume}>
            <Play aria-hidden="true" />
            Resume
          </button>
        )}
        {jobId ? (
          <button className="secondary compact" onClick={() => onToggleJob(jobId)}>
            {expandedJobId === jobId ? "Hide latest result" : "Inspect latest result"}
          </button>
        ) : null}
        <button
          className="secondary compact sync-delete"
          disabled={busy || active}
          onClick={() => {
            if (confirm(`Delete sync for "${rule.source_playlist_name}"?`)) void onDelete();
          }}
        >
          <Trash2 aria-hidden="true" />
          Delete
        </button>
      </div>
      {jobId && expandedJobId === jobId ? (
        <ProgressBoard
          className="sync-progress"
          jobId={jobId}
          onMigrationChanged={onRefresh}
          onReconnectProvider={onReconnectProvider}
        />
      ) : null}
    </article>
  );
}

function SyncMetric({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function statusLabel(status: string): string {
  return status
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatDate(value: string | null, timezone: string): string {
  if (!value) return "Not yet";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: timezone,
  }).format(new Date(value));
}

function defaultTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
