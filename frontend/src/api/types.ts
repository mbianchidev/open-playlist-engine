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
  saved_albums: { read: boolean; write: boolean };
  followed_artists: {
    read: boolean;
    write: boolean;
    semantics: "follow" | "favorite" | null;
  };
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

export interface Album {
  id: string | null;
  title: string;
  artists: string[];
  upc: string | null;
  release_date: string | null;
  release_year: number | null;
  artwork_uri: string | null;
  provider_uris: Record<string, string>;
  metadata: Record<string, unknown>;
  source_item_id: string | null;
  added_at: string | null;
}

export interface Artist {
  id: string | null;
  name: string;
  artwork_uri: string | null;
  provider_uris: Record<string, string>;
  metadata: Record<string, unknown>;
  source_item_id: string | null;
  added_at: string | null;
}

export interface SavedAlbumsView {
  source_supported: boolean;
  target_supported: boolean;
  count: number;
  items: Album[];
  source_limitation: string | null;
  target_limitation: string | null;
}

export interface FollowedArtistsView {
  source_supported: boolean;
  target_supported: boolean;
  source_semantics: "follow" | "favorite" | null;
  target_semantics: "follow" | "favorite" | null;
  count: number;
  items: Artist[];
  source_limitation: string | null;
  target_limitation: string | null;
}

export interface LibraryView {
  saved_albums: SavedAlbumsView;
  followed_artists: FollowedArtistsView;
}

export interface CreateMigrationBody {
  source_provider: string;
  target_provider: string;
  source_account_id: string;
  target_account_id: string;
  selection: {
    playlist_ids: string[];
    tracks: Record<string, string[]>;
    saved_album_ids: string[];
    followed_artist_ids: string[];
  };
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
  selection_summary: MigrationSelectionSummary;
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
  saved_album_count: number;
  followed_artist_count: number;
  entity_counts: Record<"track" | "album" | "artist", StatusCounts>;
  playlists: PlaylistStatsView[];
  empty: boolean;
  message: string | null;
}

export interface AggregateMigrationStatsView {
  source_provider: string | null;
  target_provider: string | null;
  total_migrations: number;
  total_playlists: number;
  total_saved_albums: number;
  total_followed_artists: number;
  entity_counts: Record<"track" | "album" | "artist", StatusCounts>;
  counts: StatusCounts;
  empty: boolean;
  message: string | null;
}

export interface MigrationWarningsView {
  code: string;
  message: string;
  warnings: { code: string; message: string }[];
  summary: MigrationSelectionSummary;
}

export interface MigrationSelectionSummary {
  playlists: number;
  tracks: number;
  saved_albums: number;
  followed_artists: number;
}

export interface JobItemView {
  id: string;
  entity_type: "track" | "album" | "artist";
  source_playlist_id: string | null;
  source_playlist_name: string | null;
  target_playlist_id: string | null;
  source_entity_id: string | null;
  source_entity_name: string | null;
  target_entity_id: string | null;
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
