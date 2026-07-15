import type {
  AccountView,
  AggregateMigrationStatsView,
  AuthChallenge,
  ConnectionView,
  ConnectionTestView,
  CreateMigrationBody,
  CreateGenerationDraftBody,
  GenerationDraftView,
  GeneratorCandidateView,
  GeneratorConfigView,
  GeneratorPreferenceView,
  GeneratorTrackSearchBody,
  JobItemView,
  JobView,
  MigrationOptionView,
  MigrationStatsView,
  MigrationWarningsView,
  Playlist,
  PlaylistRef,
  ProviderView,
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

// SSE stream of migration progress. Returns a disposer.
export function subscribeProgress(jobId: string, onMessage: (e: MessageEvent) => void): () => void {
  const source = new EventSource(`/api/migrations/${jobId}/events`);
  source.addEventListener("progress", onMessage as EventListener);
  return () => source.close();
}

export async function getGeneratorConfig(): Promise<GeneratorConfigView> {
  return json<GeneratorConfigView>(await fetch("/api/generator/config"));
}

export async function getGeneratorPreferences(): Promise<GeneratorPreferenceView> {
  return json<GeneratorPreferenceView>(await fetch("/api/generator/preferences"));
}

export async function updateGeneratorPreferences(enabled: boolean): Promise<GeneratorPreferenceView> {
  return json<GeneratorPreferenceView>(
    await fetch("/api/generator/preferences", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ enabled }),
    }),
  );
}

export async function deleteGeneratorPreferences(): Promise<GeneratorPreferenceView> {
  return json<GeneratorPreferenceView>(
    await fetch("/api/generator/preferences", { method: "DELETE" }),
  );
}

export async function createGenerationDraft(
  body: CreateGenerationDraftBody,
): Promise<GenerationDraftView> {
  return json<GenerationDraftView>(
    await fetch("/api/generator/drafts", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function updateGenerationDraft(
  draftId: string,
  body: { name?: string; description?: string | null },
): Promise<GenerationDraftView> {
  return json<GenerationDraftView>(
    await fetch(`/api/generator/drafts/${encodeURIComponent(draftId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function deleteGenerationDraft(draftId: string): Promise<void> {
  const response = await fetch(`/api/generator/drafts/${encodeURIComponent(draftId)}`, {
    method: "DELETE",
  });
  if (!response.ok) await json<never>(response);
}

export async function searchGeneratorTracks(
  body: GeneratorTrackSearchBody,
): Promise<GeneratorCandidateView[]> {
  return json<GeneratorCandidateView[]>(
    await fetch("/api/generator/search", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function addGenerationDraftItem(
  draftId: string,
  candidate: GeneratorCandidateView,
): Promise<GenerationDraftView> {
  return json<GenerationDraftView>(
    await fetch(`/api/generator/drafts/${encodeURIComponent(draftId)}/items`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ candidate: candidateSelection(candidate) }),
    }),
  );
}

export async function updateGenerationDraftItem(
  draftId: string,
  itemId: string,
  body:
    | { action: "approve" }
    | { action: "replace"; candidate: GeneratorCandidateView },
): Promise<GenerationDraftView> {
  const payload =
    body.action === "approve"
      ? body
      : { action: body.action, candidate: candidateSelection(body.candidate) };
  return json<GenerationDraftView>(
    await fetch(
      `/api/generator/drafts/${encodeURIComponent(draftId)}/items/${encodeURIComponent(itemId)}`,
      {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  );
}

export async function deleteGenerationDraftItem(
  draftId: string,
  itemId: string,
): Promise<GenerationDraftView> {
  return json<GenerationDraftView>(
    await fetch(
      `/api/generator/drafts/${encodeURIComponent(draftId)}/items/${encodeURIComponent(itemId)}`,
      { method: "DELETE" },
    ),
  );
}

export async function reorderGenerationDraftItems(
  draftId: string,
  itemIds: string[],
): Promise<GenerationDraftView> {
  return json<GenerationDraftView>(
    await fetch(`/api/generator/drafts/${encodeURIComponent(draftId)}/reorder`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ item_ids: itemIds }),
    }),
  );
}

export async function confirmGenerationDraft(
  draftId: string,
  acknowledgeWarnings = false,
): Promise<JobView> {
  return json<JobView>(
    await fetch(`/api/generator/drafts/${encodeURIComponent(draftId)}/confirm`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ acknowledge_warnings: acknowledgeWarnings }),
    }),
  );
}

function candidateSelection(candidate: GeneratorCandidateView) {
  return {
    uri: candidate.uri,
    title: candidate.title,
    artist: candidate.artist,
    album: candidate.album,
  };
}
