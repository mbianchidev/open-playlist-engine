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
  can_unfollow_playlist: boolean;
  can_delete_playlist: boolean;
  can_remove_tracks: boolean;
  max_remove_batch: number;
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

export interface ConnectionTestView {
  status: string;
  provider: string;
  account_id: string;
  message: string;
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
  owner_name: string | null;
  is_owned: boolean | null;
  is_followed: boolean | null;
  collaborative: boolean | null;
  snapshot_id: string | null;
  tracks_href: string | null;
  created_at: string | null;
  updated_at: string | null;
  migration_status: string | null;
  migrated_track_count: number;
  remaining_track_count: number | null;
  migration_note: string | null;
  kind: "standard" | "liked_tracks";
}

export interface Credit {
  role: string;
  name: string;
  instrument: string | null;
  uri: string | null;
}

export interface Track {
  id: string | null;
  title: string;
  artist: string;
  album: string | null;
  duration_s: number | null;
  release_date: string | null;
  release_year: number | null;
  genre: string | null;
  track_number: number | null;
  disc_number: number | null;
  explicit: boolean | null;
  composer: string | null;
  credits: Credit[];
  label: string | null;
  isrc: string | null;
  artwork_uri: string | null;
  provider_uris: Record<string, string>;
  metadata: Record<string, unknown>;
  position: number | null;
  media_type: string;
  is_local: boolean;
  source_item_id: string | null;
  added_at: string | null;
  unsupported_reason: string | null;
  migration_status: string | null;
  migrated_target_playlist_id: string | null;
  migrated_target_uri: string | null;
}

export interface Playlist {
  id: string | null;
  name: string;
  description: string | null;
  photo: string | null;
  tracks: Track[];
  owner_id: string | null;
  snapshot_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  kind: "standard" | "liked_tracks";
}

export interface CreateMigrationBody {
  source_provider: string;
  target_provider: string;
  source_account_id: string;
  target_account_id: string;
  selection: { playlist_ids: string[]; tracks: Record<string, string[]> };
  acknowledge_warnings?: boolean;
}

export interface JobView {
  id: string;
  status: string;
  source_provider: string;
  target_provider: string;
  total: number;
  done: number;
  failed: number;
  error: string | null;
}

export interface StatusCounts {
  total: number;
  pending: number;
  matched: number;
  needs_review: number;
  written: number;
  skipped: number;
  failed: number;
  other: Record<string, number>;
}

export interface MigrationOptionView {
  id: string;
  label: string;
  playlist_names: string[];
  status: string;
  source_provider: string;
  target_provider: string;
  created_at: string | null;
}

export interface PlaylistStatsView {
  source_playlist_id: string;
  source_playlist_name: string | null;
  target_playlist_id: string | null;
  counts: StatusCounts;
}

export interface MigrationStatsView {
  id: string;
  label: string;
  playlist_names: string[];
  status: string;
  source_provider: string;
  target_provider: string;
  created_at: string | null;
  counts: StatusCounts;
  playlist_count: number;
  playlists: PlaylistStatsView[];
  empty: boolean;
  message: string | null;
}

export interface AggregateMigrationStatsView {
  source_provider: string | null;
  target_provider: string | null;
  total_migrations: number;
  total_playlists: number;
  counts: StatusCounts;
  empty: boolean;
  message: string | null;
}

export interface MigrationWarningsView {
  code: string;
  message: string;
  warnings: { code: string; message: string }[];
}

export interface JobItemView {
  id: string;
  source_playlist_id: string;
  source_playlist_name: string | null;
  target_playlist_id: string | null;
  position: number;
  title: string;
  artist: string;
  album: string | null;
  duration_s: number | null;
  release_year: number | null;
  explicit: boolean | null;
  isrc: string | null;
  source_metadata: Record<string, unknown>;
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

export type OrganizerIntent = "remove" | "delete" | "remove_tracks";
export type OrganizerAction = "unfollow_playlist" | "delete_playlist" | "remove_tracks";

export interface OrganizerTrackSelection {
  position: number;
  source_item_id?: string | null;
}

export interface OrganizerSelection {
  playlist_actions: { playlist_id: string; intent: OrganizerIntent }[];
  track_removals: { playlist_id: string; tracks: OrganizerTrackSelection[] }[];
}

export interface OrganizerRequestBody {
  provider: string;
  account_id: string;
  selection: OrganizerSelection;
  confirmation?: string | null;
}

export interface OrganizerPlaylistView {
  playlist: PlaylistRef;
  ownership: "owned" | "collaborative" | "followed" | "unknown";
  available_intents: OrganizerIntent[];
  requires_ownership_check: boolean;
  notes: string[];
}

export interface OrganizerPreflightItemView {
  playlist_id: string;
  playlist_name: string;
  action: OrganizerAction;
  destructive: boolean;
  ownership: string;
  collaborative: boolean | null;
  selected_track_count: number;
  recovery: string;
}

export interface OrganizerPreflightGroupView {
  action: OrganizerAction;
  label: string;
  destructive: boolean;
  recovery: string;
  items: OrganizerPreflightItemView[];
}

export interface OrganizerUnsupportedItem {
  playlist_id: string;
  playlist_name: string;
  intent: OrganizerIntent;
  reason: string;
}

export interface OrganizerPreflightView {
  code: "organizer_preflight" | "organizer_confirmation_required";
  groups: OrganizerPreflightGroupView[];
  unsupported: OrganizerUnsupportedItem[];
  confirmation_required: boolean;
  confirmation_phrase: string | null;
  total_playlists: number;
  total_tracks: number;
}

export interface DuplicateCandidateView {
  playlist_ids: [string, string];
  playlist_names: [string, string];
  normalized_name: string;
  overlap_count: number;
  overlap_ratio: number;
  reasons: string[];
}

export interface OrganizerItemView {
  id: string;
  playlist_id: string;
  playlist_name: string;
  action: OrganizerAction;
  destructive: boolean;
  ownership: string;
  collaborative: boolean | null;
  status: string;
  attempts: number;
  retryable: boolean;
  error: string | null;
  request_payload: Record<string, unknown>;
  result_payload: Record<string, unknown>;
}

export interface OrganizerJobView {
  id: string;
  provider: string;
  account_id: string;
  status: string;
  total: number;
  done: number;
  failed: number;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  items: OrganizerItemView[];
}
