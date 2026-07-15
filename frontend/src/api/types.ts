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
  source_provider?: string | null;
  target_provider: string;
  source_account_id?: string | null;
  source_snapshot_id?: string | null;
  target_account_id: string;
  selection: { playlist_ids: string[]; tracks: Record<string, string[]> };
  acknowledge_warnings?: boolean;
}

export interface JobView {
  id: string;
  status: string;
  source_kind: string;
  source_provider: string;
  source_snapshot_id: string | null;
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

export interface SnapshotProfileSourceInput {
  provider: string;
  account_id: string;
  collection_ids: string[];
}

export interface CreateSnapshotProfileBody {
  name: string;
  sources: SnapshotProfileSourceInput[];
  retention_count?: number | null;
  retention_days?: number | null;
}

export interface SnapshotProfileSourceView {
  id: string;
  provider: string;
  account_id: string | null;
  account_label: string | null;
  collection_ids: string[];
}

export interface SnapshotProfileView {
  id: string;
  name: string;
  retention_count: number | null;
  retention_days: number | null;
  sources: SnapshotProfileSourceView[];
  snapshot_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface SnapshotCounts {
  sources: number;
  collections: number;
  items: number;
  failed_collections: number;
}

export interface SnapshotSourceManifest {
  key: string;
  provider: string;
  account_label: string | null;
  selected_collection_count: number;
}

export interface SnapshotFailure {
  source_key: string;
  provider: string;
  collection_id: string | null;
  message: string;
}

export interface SnapshotCollectionManifest {
  id: string;
  source_key: string;
  source_provider: string;
  source_collection_id: string;
  entity_type: "playlist";
  name: string;
  kind: "standard" | "liked_tracks";
  path: string;
  item_count: number;
  payload_bytes: number;
  payload_sha256: string;
  items_sha256: string;
  complete: boolean;
  error: string | null;
}

export interface SnapshotManifest {
  format: "open-playlist-bundle";
  schema_version: number;
  snapshot_id: string;
  library_id: string;
  created_at: string;
  profile_name: string | null;
  status: "complete" | "partial";
  sources: SnapshotSourceManifest[];
  counts: SnapshotCounts;
  collections: SnapshotCollectionManifest[];
  failures: SnapshotFailure[];
}

export interface SnapshotView {
  id: string;
  profile_id: string | null;
  profile_name: string | null;
  bundle_id: string;
  library_id: string;
  source_providers: string[];
  source_labels: string[];
  status: string;
  schema_version: number;
  size_bytes: number;
  counts: SnapshotCounts;
  errors: Record<string, unknown>[];
  verification_status: string;
  verification_error: string | null;
  verified_at: string | null;
  created_at: string | null;
}

export interface SnapshotDetailView extends SnapshotView {
  manifest: SnapshotManifest | null;
}

export interface SnapshotListView {
  snapshots: SnapshotView[];
  total_bytes: number;
}

export interface SnapshotVerificationView {
  snapshot_id: string;
  status: string;
  archive_sha256: string | null;
  verified_at: string | null;
  error: string | null;
}

export interface SnapshotDiffCollection {
  id: string;
  name: string;
  previous_name: string | null;
  item_count: number;
  previous_item_count: number | null;
}

export interface SnapshotDiffView {
  base_snapshot_id: string;
  compare_snapshot_id: string;
  added: SnapshotDiffCollection[];
  removed: SnapshotDiffCollection[];
  renamed: SnapshotDiffCollection[];
  changed: SnapshotDiffCollection[];
  items_added: number;
  items_removed: number;
}

export interface SnapshotCleanupView {
  deleted_count: number;
  deleted_bytes: number;
}
