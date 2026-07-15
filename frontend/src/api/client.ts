import type {
  AccountView,
  AggregateMigrationStatsView,
  AuthChallenge,
  ConnectionView,
  ConnectionTestView,
  CreateMigrationBody,
  CreateSyncRuleBody,
  JobItemView,
  JobView,
  MigrationOptionView,
  MigrationStatsView,
  MigrationWarningsView,
  Playlist,
  PlaylistRef,
  ProviderView,
  SyncRuleView,
  SyncRunView,
  UpdateSyncRuleBody,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, statusText: string, detail: unknown) {
    super(errorDetailMessage(detail) ?? `${status} ${statusText}`);
    this.status = status;
    this.detail = detail;
  }
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    throw new ApiError(res.status, res.statusText, body?.detail ?? null);
  }
  return (await res.json()) as T;
}

function errorDetailMessage(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    return typeof message === "string" ? message : null;
  }
  return null;
}

export async function getProviders(): Promise<ProviderView[]> {
  return json<ProviderView[]>(await fetch("/api/providers"));
}

export async function beginAuth(provider: string): Promise<AuthChallenge> {
  return json<AuthChallenge>(await fetch(`/api/auth/${provider}/begin`, { method: "POST" }));
}

export async function completeAuth(
  provider: string,
  callback: Record<string, unknown>,
): Promise<ConnectionView> {
  return json<ConnectionView>(
    await fetch(`/api/auth/${provider}/complete`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(callback),
    }),
  );
}

export async function getAccounts(provider?: string, check = false): Promise<AccountView[]> {
  const params = new URLSearchParams();
  if (provider) params.set("provider", provider);
  if (check) params.set("check", "true");
  const suffix = params.size ? `?${params}` : "";
  return json<AccountView[]>(await fetch(`/api/auth/accounts${suffix}`));
}

export async function testAccountConnection(accountId: string): Promise<ConnectionTestView> {
  return json<ConnectionTestView>(
    await fetch(`/api/auth/accounts/${encodeURIComponent(accountId)}/test`, { method: "POST" }),
  );
}

export interface PlaylistContext {
  targetProvider?: string | null;
  targetAccountId?: string | null;
  refresh?: boolean;
}

function playlistParams(provider: string, accountId: string, context?: PlaylistContext): URLSearchParams {
  const params = new URLSearchParams({ provider, account_id: accountId });
  if (context?.targetProvider && context.targetAccountId) {
    params.set("target_provider", context.targetProvider);
    params.set("target_account_id", context.targetAccountId);
  }
  if (context?.refresh) params.set("refresh", "true");
  return params;
}

export async function getPlaylists(
  provider: string,
  accountId: string,
  context?: PlaylistContext,
): Promise<PlaylistRef[]> {
  const params = playlistParams(provider, accountId, context);
  return json<PlaylistRef[]>(await fetch(`/api/playlists?${params}`));
}

export async function getPlaylist(
  provider: string,
  accountId: string,
  playlistId: string,
  context?: PlaylistContext,
): Promise<Playlist> {
  const params = playlistParams(provider, accountId, context);
  return json<Playlist>(await fetch(`/api/playlists/${encodeURIComponent(playlistId)}?${params}`));
}

export async function createMigration(body: CreateMigrationBody): Promise<JobView> {
  return json<JobView>(
    await fetch("/api/migrations", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function listMigrations(): Promise<MigrationOptionView[]> {
  return json<MigrationOptionView[]>(await fetch("/api/migrations"));
}

export interface AggregateMigrationStatsFilters {
  sourceProvider?: string | null;
  targetProvider?: string | null;
}

export async function getAggregateMigrationStats(
  filters: AggregateMigrationStatsFilters = {},
): Promise<AggregateMigrationStatsView> {
  const params = new URLSearchParams();
  if (filters.sourceProvider) params.set("source_provider", filters.sourceProvider);
  if (filters.targetProvider) params.set("target_provider", filters.targetProvider);
  const suffix = params.size ? `?${params}` : "";
  return json<AggregateMigrationStatsView>(await fetch(`/api/migrations/stats${suffix}`));
}

export async function getMigrationStats(jobId: string): Promise<MigrationStatsView> {
  return json<MigrationStatsView>(
    await fetch(`/api/migrations/${encodeURIComponent(jobId)}/stats`),
  );
}

export async function preflightMigration(
  body: CreateMigrationBody,
): Promise<MigrationWarningsView> {
  return json<MigrationWarningsView>(
    await fetch("/api/migrations/preflight", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function getMigrationItems(jobId: string): Promise<JobItemView[]> {
  return json<JobItemView[]>(await fetch(`/api/migrations/${jobId}/items`));
}

export async function reviewMigrationItem(
  jobId: string,
  itemId: string,
  body: { action: "approve" | "skip"; target_uri?: string | null },
): Promise<JobItemView> {
  return json<JobItemView>(
    await fetch(`/api/migrations/${jobId}/items/${itemId}/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function reviewMigrationItems(
  jobId: string,
  body: { action: "approve" | "skip"; item_ids: string[] },
): Promise<JobItemView[]> {
  return json<JobItemView[]>(
    await fetch(`/api/migrations/${jobId}/items/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function listSyncRules(): Promise<SyncRuleView[]> {
  return json<SyncRuleView[]>(await fetch("/api/syncs"));
}

export async function createSyncRule(body: CreateSyncRuleBody): Promise<SyncRuleView> {
  return json<SyncRuleView>(
    await fetch("/api/syncs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function updateSyncRule(
  ruleId: string,
  body: UpdateSyncRuleBody,
): Promise<SyncRuleView> {
  return json<SyncRuleView>(
    await fetch(`/api/syncs/${encodeURIComponent(ruleId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function pauseSyncRule(ruleId: string): Promise<SyncRuleView> {
  return json<SyncRuleView>(
    await fetch(`/api/syncs/${encodeURIComponent(ruleId)}/pause`, { method: "POST" }),
  );
}

export async function resumeSyncRule(ruleId: string): Promise<SyncRuleView> {
  return json<SyncRuleView>(
    await fetch(`/api/syncs/${encodeURIComponent(ruleId)}/resume`, { method: "POST" }),
  );
}

export async function runSyncRule(ruleId: string): Promise<SyncRunView> {
  return json<SyncRunView>(
    await fetch(`/api/syncs/${encodeURIComponent(ruleId)}/run`, { method: "POST" }),
  );
}

export async function deleteSyncRule(ruleId: string): Promise<void> {
  const response = await fetch(`/api/syncs/${encodeURIComponent(ruleId)}`, {
    method: "DELETE",
  });
  if (!response.ok) await json<never>(response);
}

// SSE stream of migration progress. Returns a disposer.
export function subscribeProgress(jobId: string, onMessage: (e: MessageEvent) => void): () => void {
  const source = new EventSource(`/api/migrations/${jobId}/events`);
  source.addEventListener("progress", onMessage as EventListener);
  return () => source.close();
}
