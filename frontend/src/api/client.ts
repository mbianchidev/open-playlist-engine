import type {
  AccountView,
  AggregateMigrationStatsView,
  AuthChallenge,
  ConnectionView,
  CreateShareBody,
  ConnectionTestView,
  CreateExportBody,
  CreateMigrationBody,
  ExportDownloadResult,
  ExportFormat,
  JobItemView,
  JobView,
  LibraryView,
  MigrationItemFilters,
  MigrationItemPage,
  MigrationOptionView,
  MigrationStatsView,
  MigrationWarningsView,
  DuplicateCandidateView,
  OrganizerJobView,
  OrganizerPlaylistView,
  OrganizerPreflightView,
  OrganizerRequestBody,
  OwnerSessionView,
  PortableFormat,
  Playlist,
  PlaylistRef,
  ProviderView,
  PublicShareView,
  ShareConfigView,
  ShareDetailView,
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
    await throwApiError(res);
  }
  return (await res.json()) as T;
}

async function throwApiError(res: Response): Promise<never> {
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  throw new ApiError(res.status, res.statusText, body?.detail ?? null);
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

export async function getOwnerSession(): Promise<OwnerSessionView> {
  return json<OwnerSessionView>(await fetch("/api/session"));
}

export async function loginOwner(accessToken: string): Promise<OwnerSessionView> {
  return json<OwnerSessionView>(
    await fetch("/api/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ access_token: accessToken }),
    }),
  );
}

export async function logoutOwner(): Promise<OwnerSessionView> {
  return json<OwnerSessionView>(await fetch("/api/session", { method: "DELETE" }));
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

export async function getOrganizerPlaylists(
  provider: string,
  accountId: string,
  refresh = false,
): Promise<OrganizerPlaylistView[]> {
  const params = new URLSearchParams({ provider, account_id: accountId });
  if (refresh) params.set("refresh", "true");
  return json<OrganizerPlaylistView[]>(await fetch(`/api/organizer/playlists?${params}`));
}

export async function preflightOrganizer(
  body: OrganizerRequestBody,
): Promise<OrganizerPreflightView> {
  return json<OrganizerPreflightView>(
    await fetch("/api/organizer/preflight", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function downloadPlaylistExport(
  body: CreateExportBody,
): Promise<ExportDownloadResult> {
  return download(
    await fetch("/api/exports", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function createOrganizerJob(
  body: OrganizerRequestBody,
): Promise<OrganizerJobView> {
  return json<OrganizerJobView>(
    await fetch("/api/organizer/jobs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function downloadMigrationExport(
  jobId: string,
  format: ExportFormat,
): Promise<ExportDownloadResult> {
  return download(
    await fetch(`/api/exports/migrations/${encodeURIComponent(jobId)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ format }),
    }),
  );
}

export async function analyzeOrganizerDuplicates(
  provider: string,
  accountId: string,
): Promise<DuplicateCandidateView[]> {
  return json<DuplicateCandidateView[]>(
    await fetch("/api/organizer/duplicates", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ provider, account_id: accountId }),
    }),
  );
}

export async function listOrganizerJobs(): Promise<OrganizerJobView[]> {
  return json<OrganizerJobView[]>(await fetch("/api/organizer/jobs"));
}

export async function getOrganizerJob(jobId: string): Promise<OrganizerJobView> {
  return json<OrganizerJobView>(
    await fetch(`/api/organizer/jobs/${encodeURIComponent(jobId)}`),
  );
}

export async function retryOrganizerJob(jobId: string): Promise<OrganizerJobView> {
  return json<OrganizerJobView>(
    await fetch(`/api/organizer/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: "POST",
    }),
  );
}

export async function getLibrary(
  provider: string,
  accountId: string,
  context?: PlaylistContext,
): Promise<LibraryView> {
  const params = playlistParams(provider, accountId, context);
  return json<LibraryView>(await fetch(`/api/library?${params}`));
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

export async function getMigrationItems(
  jobId: string,
  filters: MigrationItemFilters = {},
): Promise<JobItemView[]> {
  const params = migrationItemParams(filters);
  const suffix = params.size ? `?${params}` : "";
  return json<JobItemView[]>(
    await fetch(`/api/migrations/${encodeURIComponent(jobId)}/items${suffix}`),
  );
}

export async function getMigrationItemPage(
  jobId: string,
  filters: MigrationItemFilters,
  options: { limit: number; offset: number },
): Promise<MigrationItemPage> {
  const params = migrationItemParams(filters);
  params.set("limit", String(options.limit));
  params.set("offset", String(options.offset));
  const response = await fetch(`/api/migrations/${encodeURIComponent(jobId)}/items?${params}`);
  const items = await json<JobItemView[]>(response);
  const totalHeader = response.headers.get("x-total-count");
  const total = totalHeader === null ? items.length : Number(totalHeader);
  return {
    items,
    total: Number.isFinite(total) ? total : items.length,
    limit: options.limit,
    offset: options.offset,
  };
}

export function migrationReportUrl(
  jobId: string,
  format: "csv" | "json",
  scope: "all" | "problems",
  filters: MigrationItemFilters = {},
): string {
  const params = migrationItemParams(filters);
  params.set("format", format);
  params.set("scope", scope);
  return `/api/migrations/${encodeURIComponent(jobId)}/report?${params}`;
}

export async function reviewMigrationItem(
  jobId: string,
  itemId: string,
  body: { action: "approve" | "skip"; target_uri?: string | null },
): Promise<JobItemView> {
  return json<JobItemView>(
    await fetch(
      `/api/migrations/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/review`,
      {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      },
    ),
  );
}

export async function reviewMigrationItems(
  jobId: string,
  body: { action: "approve" | "skip"; item_ids: string[] },
): Promise<JobItemView[]> {
  return json<JobItemView[]>(
    await fetch(`/api/migrations/${encodeURIComponent(jobId)}/items/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

// SSE stream of migration progress. Returns a disposer.
export function subscribeProgress(jobId: string, onMessage: (e: MessageEvent) => void): () => void {
  const source = new EventSource(`/api/migrations/${encodeURIComponent(jobId)}/events`);
  source.addEventListener("progress", onMessage as EventListener);
  return () => source.close();
}

export interface MigrationProgressApi {
  getItems(jobId: string): Promise<JobItemView[]>;
  reviewItem(
    jobId: string,
    itemId: string,
    body: { action: "approve" | "skip"; target_uri?: string | null },
  ): Promise<JobItemView>;
  reviewItems(
    jobId: string,
    body: { action: "approve" | "skip"; item_ids: string[] },
  ): Promise<JobItemView[]>;
  subscribe(jobId: string, onMessage: (event: MessageEvent) => void): () => void;
}

export const ownerMigrationProgressApi: MigrationProgressApi = {
  getItems: getMigrationItems,
  reviewItem: reviewMigrationItem,
  reviewItems: reviewMigrationItems,
  subscribe: subscribeProgress,
};

export async function getShareConfig(): Promise<ShareConfigView> {
  return json<ShareConfigView>(await fetch("/api/shares/config"));
}

export async function listShares(): Promise<ShareDetailView[]> {
  return json<ShareDetailView[]>(await fetch("/api/shares"));
}

export async function createShare(body: CreateShareBody): Promise<ShareDetailView> {
  return json<ShareDetailView>(
    await fetch("/api/shares", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function updateShare(
  shareId: string,
  body: { visibility?: "public" | "unlisted"; expires_at?: string | null },
): Promise<ShareDetailView> {
  return json<ShareDetailView>(
    await fetch(`/api/shares/${encodeURIComponent(shareId)}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function expireShare(shareId: string): Promise<ShareDetailView> {
  return json<ShareDetailView>(
    await fetch(`/api/shares/${encodeURIComponent(shareId)}/expire`, { method: "POST" }),
  );
}

export async function revokeShare(shareId: string): Promise<ShareDetailView> {
  return json<ShareDetailView>(
    await fetch(`/api/shares/${encodeURIComponent(shareId)}/revoke`, { method: "POST" }),
  );
}

export async function getPublicShare(token: string): Promise<PublicShareView> {
  return json<PublicShareView>(
    await fetch(`/api/public/shares/${encodeURIComponent(token)}`),
  );
}

export function publicShareDownloadUrl(token: string, format: PortableFormat): string {
  const params = new URLSearchParams({ format });
  return `/api/public/shares/${encodeURIComponent(token)}/download?${params}`;
}

export async function getRecipientAccounts(token: string): Promise<AccountView[]> {
  return json<AccountView[]>(
    await fetch(`/api/public/shares/${encodeURIComponent(token)}/accounts`),
  );
}

export async function beginRecipientAuth(
  token: string,
  provider: string,
): Promise<AuthChallenge> {
  return json<AuthChallenge>(
    await fetch(
      `/api/public/shares/${encodeURIComponent(token)}/auth/${encodeURIComponent(provider)}/begin`,
      { method: "POST" },
    ),
  );
}

export async function completeRecipientAuth(
  token: string,
  provider: string,
  callback: Record<string, unknown>,
): Promise<ConnectionView> {
  return json<ConnectionView>(
    await fetch(
      `/api/public/shares/${encodeURIComponent(token)}/auth/${encodeURIComponent(provider)}/complete`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(callback),
      },
    ),
  );
}

export async function importPublicShare(
  token: string,
  body: {
    target_provider: string;
    target_account_id: string;
    acknowledge_warnings?: boolean;
  },
): Promise<JobView> {
  return json<JobView>(
    await fetch(`/api/public/shares/${encodeURIComponent(token)}/imports`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export function recipientMigrationProgressApi(token: string): MigrationProgressApi {
  const prefix = `/api/public/shares/${encodeURIComponent(token)}/imports`;
  return {
    getItems: async (jobId) =>
      json<JobItemView[]>(await fetch(`${prefix}/${encodeURIComponent(jobId)}/items`)),
    reviewItem: async (jobId, itemId, body) =>
      json<JobItemView>(
        await fetch(
          `${prefix}/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/review`,
          {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          },
        ),
      ),
    reviewItems: async (jobId, body) =>
      json<JobItemView[]>(
        await fetch(`${prefix}/${encodeURIComponent(jobId)}/items/review`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        }),
      ),
    subscribe: (jobId, onMessage) => {
      const source = new EventSource(`${prefix}/${encodeURIComponent(jobId)}/events`);
      source.addEventListener("progress", onMessage as EventListener);
      return () => source.close();
    },
  };
}

async function download(res: Response): Promise<ExportDownloadResult> {
  if (!res.ok) await throwApiError(res);
  const filename = downloadFilename(res.headers.get("content-disposition")) ?? "playlist-export";
  const warningCount = Number.parseInt(
    res.headers.get("x-open-playlist-warning-count") ?? "0",
    10,
  );
  const objectUrl = URL.createObjectURL(await res.blob());
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  link.hidden = true;
  document.body.append(link);
  try {
    link.click();
  } finally {
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
  return {
    filename,
    warningCount: Number.isFinite(warningCount) ? warningCount : 0,
  };
}

function downloadFilename(contentDisposition: string | null): string | null {
  if (!contentDisposition) return null;
  const encoded = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded);
    } catch {
      return encoded;
    }
  }
  return contentDisposition.match(/filename="?([^";]+)"?/i)?.[1] ?? null;
}

function migrationItemParams(filters: MigrationItemFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.sourcePlaylistId) params.set("source_playlist_id", filters.sourcePlaylistId);
  for (const entityType of filters.entityTypes ?? []) params.append("entity_type", entityType);
  for (const status of filters.statuses ?? []) params.append("status", status);
  if (filters.minConfidence !== null && filters.minConfidence !== undefined) {
    params.set("min_confidence", String(filters.minConfidence));
  }
  if (filters.maxConfidence !== null && filters.maxConfidence !== undefined) {
    params.set("max_confidence", String(filters.maxConfidence));
  }
  if (filters.reason) params.set("reason", filters.reason);
  if (filters.title) params.set("title", filters.title);
  if (filters.artist) params.set("artist", filters.artist);
  if (filters.problemOnly) params.set("problem_only", "true");
  return params;
}
