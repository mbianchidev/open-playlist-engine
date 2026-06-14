import type {
  AccountView,
  AuthChallenge,
  ConnectionView,
  JobItemView,
  JobView,
  PlaylistRef,
  ProviderView,
} from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
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

export async function getAccounts(provider?: string): Promise<AccountView[]> {
  const params = provider ? `?provider=${encodeURIComponent(provider)}` : "";
  return json<AccountView[]>(await fetch(`/api/auth/accounts${params}`));
}

export async function getPlaylists(provider: string, accountId: string): Promise<PlaylistRef[]> {
  const params = new URLSearchParams({ provider, account_id: accountId });
  return json<PlaylistRef[]>(await fetch(`/api/playlists?${params}`));
}

export interface CreateMigrationBody {
  source_provider: string;
  target_provider: string;
  source_account_id: string;
  target_account_id: string;
  selection: { playlist_ids: string[]; tracks: Record<string, string[]> };
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

export async function getMigrationItems(jobId: string): Promise<JobItemView[]> {
  return json<JobItemView[]>(await fetch(`/api/migrations/${jobId}/items`));
}

// SSE stream of migration progress. Returns a disposer.
export function subscribeProgress(jobId: string, onMessage: (e: MessageEvent) => void): () => void {
  const source = new EventSource(`/api/migrations/${jobId}/events`);
  source.addEventListener("progress", onMessage as EventListener);
  return () => source.close();
}
