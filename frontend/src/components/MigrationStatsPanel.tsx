import { useEffect, useMemo, useState } from "react";
import { BarChart3, ExternalLink, History, RefreshCw } from "lucide-react";
import {
  getAggregateMigrationStats,
  getMigrationStats,
  listMigrations,
} from "../api/client";
import type {
  AggregateMigrationStatsView,
  MigrationOptionView,
  MigrationStatsView,
  ProviderView,
  StatusCounts,
} from "../api/types";
import { providerLabel, targetPlaylistUrl } from "../utils/providers";
import MigrationHistoryDetail from "./MigrationHistoryDetail";

interface Props {
  providers: ProviderView[];
  refreshKey: number;
  className?: string;
}

type ProviderField = "source_provider" | "target_provider";

export default function MigrationStatsPanel({ providers, refreshKey, className }: Props) {
  const [options, setOptions] = useState<MigrationOptionView[]>([]);
  const [selectedMigrationId, setSelectedMigrationId] = useState("");
  const [selectedStats, setSelectedStats] = useState<MigrationStatsView | null>(null);
  const [aggregateStats, setAggregateStats] = useState<AggregateMigrationStatsView | null>(null);
  const [sourceFilter, setSourceFilter] = useState("");
  const [targetFilter, setTargetFilter] = useState("");
  const [listLoading, setListLoading] = useState(false);
  const [aggregateLoading, setAggregateLoading] = useState(false);
  const [selectedLoading, setSelectedLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [aggregateError, setAggregateError] = useState<string | null>(null);
  const [selectedError, setSelectedError] = useState<string | null>(null);
  const [manualRefresh, setManualRefresh] = useState(0);
  const sourceProviders = useMemo(
    () => providerChoices(providers, options, "source_provider"),
    [providers, options],
  );
  const targetProviders = useMemo(
    () => providerChoices(providers, options, "target_provider"),
    [providers, options],
  );

  useEffect(() => {
    let cancelled = false;
    async function loadMigrations() {
      setListLoading(true);
      setListError(null);
      try {
        const nextOptions = await listMigrations();
        if (cancelled) return;
        setOptions(nextOptions);
        setSelectedMigrationId((current) =>
          nextOptions.some((option) => option.id === current) ? current : nextOptions[0]?.id ?? "",
        );
      } catch (e: unknown) {
        if (!cancelled) setListError(errorMessage(e));
      } finally {
        if (!cancelled) setListLoading(false);
      }
    }
    void loadMigrations();
    return () => {
      cancelled = true;
    };
  }, [refreshKey, manualRefresh]);

  useEffect(() => {
    let cancelled = false;
    async function loadAggregateStats() {
      setAggregateLoading(true);
      setAggregateError(null);
      setAggregateStats(null);
      try {
        const nextStats = await getAggregateMigrationStats({
          sourceProvider: sourceFilter || null,
          targetProvider: targetFilter || null,
        });
        if (!cancelled) setAggregateStats(nextStats);
      } catch (e: unknown) {
        if (!cancelled) setAggregateError(errorMessage(e));
      } finally {
        if (!cancelled) setAggregateLoading(false);
      }
    }
    void loadAggregateStats();
    return () => {
      cancelled = true;
    };
  }, [refreshKey, manualRefresh, sourceFilter, targetFilter]);

  useEffect(() => {
    let cancelled = false;
    async function loadSelectedStats() {
      if (!selectedMigrationId) {
        setSelectedStats(null);
        setSelectedLoading(false);
        setSelectedError(null);
        return;
      }
      setSelectedLoading(true);
      setSelectedError(null);
      setSelectedStats(null);
      try {
        const nextStats = await getMigrationStats(selectedMigrationId);
        if (!cancelled) setSelectedStats(nextStats);
      } catch (e: unknown) {
        if (!cancelled) setSelectedError(errorMessage(e));
      } finally {
        if (!cancelled) setSelectedLoading(false);
      }
    }
    void loadSelectedStats();
    return () => {
      cancelled = true;
    };
  }, [selectedMigrationId, refreshKey, manualRefresh]);

  return (
    <section className={["card", "flow", "migration-stats", className].filter(Boolean).join(" ")}>
      <div className="section-heading">
        <div className="section-title">
          <span className="section-icon" aria-hidden="true">
            <BarChart3 />
          </span>
          <div>
            <h2>Migration history & stats</h2>
            <p className="muted">Reopen a migration, inspect its ledger, or review all-time totals.</p>
          </div>
        </div>
        <button
          className="secondary compact"
          disabled={listLoading || aggregateLoading || selectedLoading}
          onClick={() => setManualRefresh((value) => value + 1)}
        >
          <RefreshCw aria-hidden="true" />
          {listLoading || aggregateLoading || selectedLoading ? "Refreshing..." : "Refresh history"}
        </button>
      </div>

      <div className="stats-section">
        <div className="stats-subheading">
          <h3>
            <History aria-hidden="true" />
            Migration history
          </h3>
          {selectedStats ? (
            <span className={`badge status-${selectedStats.outcome ?? selectedStats.status}`}>
              {statusLabel(selectedStats.outcome ?? selectedStats.status)}
            </span>
          ) : null}
        </div>
        {listError ? <p className="warn">{listError}</p> : null}
        {listLoading && options.length === 0 ? (
          <p className="muted">Loading migrations...</p>
        ) : options.length === 0 && !listError ? (
          <p className="empty-guidance">No migrations yet. Start a migration to collect stats.</p>
        ) : (
          <>
            <label className="stats-field" htmlFor="migrationStatsSelect">
              Migration
              <select
                id="migrationStatsSelect"
                value={selectedMigrationId}
                disabled={options.length === 0}
                onChange={(event) => setSelectedMigrationId(event.target.value)}
              >
                {options.map((option) => (
                  <option key={option.id} value={option.id}>
                    {migrationOptionLabel(option)}
                  </option>
                ))}
              </select>
            </label>
            {selectedLoading ? <p className="muted">Loading migration stats...</p> : null}
            {selectedError ? <p className="warn">{selectedError}</p> : null}
            {selectedStats ? (
              <>
                <SingleMigrationStats stats={selectedStats} />
                <MigrationHistoryDetail stats={selectedStats} />
              </>
            ) : null}
          </>
        )}
      </div>

      <div className="stats-section">
        <div className="stats-subheading">
          <h3>Aggregate stats</h3>
          <span className="muted">All time</span>
        </div>
        <div className="stats-filter-row">
          <label className="stats-field" htmlFor="sourceStatsFilter">
            From
            <select
              id="sourceStatsFilter"
              value={sourceFilter}
              onChange={(event) => setSourceFilter(event.target.value)}
            >
              <option value="">Any source</option>
              {sourceProviders.map((provider) => (
                <option key={provider} value={provider}>
                  {providerLabel(provider)}
                </option>
              ))}
            </select>
          </label>
          <label className="stats-field" htmlFor="targetStatsFilter">
            To
            <select
              id="targetStatsFilter"
              value={targetFilter}
              onChange={(event) => setTargetFilter(event.target.value)}
            >
              <option value="">Any target</option>
              {targetProviders.map((provider) => (
                <option key={provider} value={provider}>
                  {providerLabel(provider)}
                </option>
              ))}
            </select>
          </label>
        </div>
        {aggregateLoading ? <p className="muted">Loading aggregate stats...</p> : null}
        {aggregateError ? <p className="warn">{aggregateError}</p> : null}
        {aggregateStats ? <AggregateStats stats={aggregateStats} /> : null}
      </div>
    </section>
  );
}

function SingleMigrationStats({ stats }: { stats: MigrationStatsView }) {
  return (
    <div className="stats-detail">
      {stats.empty ? (
        <p className="empty-guidance">{stats.message ?? "No track stats are available yet."}</p>
      ) : null}
      <StatsGrid
        counts={stats.counts}
        leading={[
          ["Playlists", stats.playlist_count],
          ["Tracks", stats.counts.total],
        ]}
      />
      {stats.playlists.length > 0 ? (
        <div className="stats-playlist-list" aria-label="Per-playlist migration stats">
          {stats.playlists.map((playlist) => (
            <div
              key={`${playlist.source_playlist_id}-${playlist.target_playlist_id ?? "pending"}`}
              className="stats-playlist-row"
            >
              <div>
                <strong>{playlist.source_playlist_name ?? "Unnamed playlist"}</strong>
                <p className="muted">{compactCounts(playlist.counts)}</p>
                {playlist.target_playlist_id ? (
                  targetPlaylistUrl(stats.target_provider, playlist.target_playlist_id) ? (
                    <a
                      className="stats-target-link"
                      href={
                        targetPlaylistUrl(stats.target_provider, playlist.target_playlist_id) ??
                        undefined
                      }
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open target
                      <ExternalLink aria-hidden="true" />
                    </a>
                  ) : (
                    <code>{playlist.target_playlist_id}</code>
                  )
                ) : null}
              </div>
              <span className="badge">{playlist.counts.total} tracks</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function AggregateStats({ stats }: { stats: AggregateMigrationStatsView }) {
  return (
    <div className="stats-detail">
      {stats.empty ? (
        <p className="empty-guidance">{stats.message ?? "No aggregate stats are available yet."}</p>
      ) : null}
      <StatsGrid
        counts={stats.counts}
        leading={[
          ["Migrations", stats.total_migrations],
          ["Playlists", stats.total_playlists],
          ["Tracks", stats.counts.total],
        ]}
      />
    </div>
  );
}

function StatsGrid({
  counts,
  leading,
}: {
  counts: StatusCounts;
  leading: Array<[string, number]>;
}) {
  const statusRows: Array<[string, number]> = [
    ["Written", counts.written],
    ["Skipped", counts.skipped],
    ["Needs review", counts.needs_review],
    ["Failed", counts.failed],
    ["Matched", counts.matched],
    ["Pending", counts.pending],
    ...Object.entries(counts.other).map<[string, number]>(([status, count]) => [
      statusLabel(status),
      count,
    ]),
  ];
  return (
    <div className="stats-grid">
      {[...leading, ...statusRows].map(([label, value]) => (
        <div key={label} className="stat-card">
          <span className="muted">{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}

function providerChoices(
  providers: ProviderView[],
  options: MigrationOptionView[],
  field: ProviderField,
): string[] {
  const names = new Set<string>();
  for (const provider of providers) names.add(provider.name);
  for (const option of options) names.add(option[field]);
  return [...names].sort((left, right) => providerLabel(left).localeCompare(providerLabel(right)));
}

function migrationOptionLabel(option: MigrationOptionView): string {
  const route = `${providerLabel(option.source_provider)} to ${providerLabel(option.target_provider)}`;
  const created = formatDate(option.created_at);
  const outcome = option.outcome ? statusLabel(option.outcome) : statusLabel(option.status);
  return [option.label, route, outcome, created].filter(Boolean).join(" - ");
}

function compactCounts(counts: StatusCounts): string {
  const parts = [
    countLabel(counts.written, "written"),
    countLabel(counts.skipped, "skipped"),
    countLabel(counts.needs_review, "need review"),
    countLabel(counts.failed, "failed"),
    countLabel(counts.pending, "pending"),
  ].filter((part): part is string => Boolean(part));
  return parts.length ? parts.join(", ") : "No tracks yet";
}

function countLabel(value: number, label: string): string | null {
  return value > 0 ? `${value} ${label}` : null;
}

function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

function formatDate(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
