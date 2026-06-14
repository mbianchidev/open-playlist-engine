// Hand-written until `npm run gen:api` produces schema.d.ts from the backend's
// OpenAPI document. The frontend depends only on these shapes — never on backend
// internals (monorepo, hard-separated).

export interface ProviderView {
  name: string;
  display_name: string;
  auth_kind: string;
  official: boolean;
  stability: string;
  has_isrc: boolean;
  can_source: boolean;
  can_target: boolean;
  warning: string | null;
}

export interface AuthChallenge {
  shape: "redirect" | "device_code" | "form";
  redirect_url: string | null;
  state: string | null;
  user_code: string | null;
  verification_url: string | null;
  poll_interval_s: number | null;
  instructions: string | null;
  form_schema: Record<string, unknown> | null;
}

export interface AccountView {
  id: string;
  provider: string;
  provider_user_id: string | null;
  display_name: string | null;
}

export interface ConnectionView {
  status: string;
  provider: string;
  account: AccountView;
}

export interface PlaylistRef {
  id: string;
  name: string;
  track_count: number | null;
  owner_id: string | null;
}

export interface JobView {
  id: string;
  status: string;
  total: number;
  done: number;
  failed: number;
  error: string | null;
}

export interface JobItemView {
  id: string;
  source_playlist_id: string;
  source_playlist_name: string | null;
  target_playlist_id: string | null;
  position: number;
  title: string;
  artist: string;
  isrc: string | null;
  target_uri: string | null;
  confidence: number | null;
  status: string;
  reason: string | null;
}

export interface ProgressEvent {
  job?: JobView;
  items?: JobItemView[];
  job_id?: string;
  missing?: boolean;
}
