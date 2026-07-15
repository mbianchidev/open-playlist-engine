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
  collaborative: boolean | null;
  snapshot_id: string | null;
  tracks_href: string | null;
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

export type GeneratorBackend = "openai_compatible" | "copilot_sdk";

export interface GeneratorLimitsView {
  max_prompt_chars: number;
  max_output_chars: number;
  max_tracks: number;
}

export interface GeneratorConfigView {
  available: boolean;
  backend: GeneratorBackend;
  model: string;
  message: string;
  limits: GeneratorLimitsView;
}

export interface GeneratorPreferenceSummary {
  top_artists: string[];
  top_genres: string[];
  source_track_count: number;
}

export interface GeneratorPreferenceView {
  enabled: boolean;
  summary: GeneratorPreferenceSummary;
}

export type ExplicitPreference = "allow" | "exclude" | "only";

export interface GeneratorControls {
  genres: string[];
  moods: string[];
  eras: string[];
  energy: number | null;
  track_count: number;
  duration_minutes: number | null;
  seed_artists: string[];
  seed_tracks: string[];
  explicit: ExplicitPreference;
  familiarity: number;
  discovery: number;
}

export interface GenerationSpec {
  prompt: string;
  controls: GeneratorControls;
}

export interface CreateGenerationDraftBody {
  target_provider: string;
  target_account_id: string;
  generation: GenerationSpec;
  use_personalization: boolean;
}

export interface GeneratedTrackIntent {
  title: string;
  artist: string;
  album: string | null;
  release_year: number | null;
  explicit: boolean | null;
  reason: string | null;
}

export interface GeneratorCandidateView {
  provider_track_id: string;
  uri: string;
  title: string;
  artist: string;
  album: string | null;
  duration_s: number | null;
  isrc: string | null;
  explicit: boolean | null;
  market: string | null;
}

export interface GenerationDraftItemView {
  id: string;
  position: number;
  intent: GeneratedTrackIntent;
  candidate: GeneratorCandidateView | null;
  confidence: number | null;
  status: "resolved" | "needs_review" | "unresolved";
  reason: string | null;
}

export interface GenerationDraftView {
  id: string;
  status: string;
  target_provider: string;
  target_account_id: string;
  name: string;
  description: string | null;
  model_backend: GeneratorBackend;
  confirmed_job_id: string | null;
  items: GenerationDraftItemView[];
  playlist: Playlist;
}

export interface GeneratorTrackSearchBody {
  target_provider: string;
  target_account_id: string;
  title: string;
  artist: string;
  album?: string | null;
  limit?: number;
}

export interface GeneratorWarningView {
  code: "generation_warnings";
  message: string;
  warnings: { code: string; message: string }[];
}
