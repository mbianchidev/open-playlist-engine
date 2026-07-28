import type { components } from "./schema";

type ApiSchema<Name extends keyof components["schemas"]> = Required<
  components["schemas"][Name]
>;

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

export interface PlaylistSelection {
  playlist_ids: string[];
  tracks: Record<string, string[]>;
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

export type MigrationEntityType = "track" | "album" | "artist";

export interface MigrationSelectionSummary {
  playlists: number;
  tracks: number;
  saved_albums: number;
  followed_artists: number;
}

export type ExportFormat = "csv" | "txt" | "m3u8" | "xspf" | "json";

export interface CreateExportBody {
  source_provider: string;
  source_account_id: string;
  format: ExportFormat;
  selection: PlaylistSelection;
}

export interface ExportDownloadResult {
  filename: string;
  warningCount: number;
}

export type JobView = ApiSchema<"JobView">;
export type StatusCounts = ApiSchema<"StatusCounts">;
export type MigrationOptionView = ApiSchema<"MigrationOptionView"> & {
  selection_summary: MigrationSelectionSummary;
};
export type AccountHistoryView = ApiSchema<"AccountHistoryView">;
export type PlaylistStatsView = Omit<ApiSchema<"PlaylistStatsView">, "counts"> & {
  counts: StatusCounts;
};
export type MigrationStatsView = Omit<
  ApiSchema<"MigrationStatsView">,
  "counts" | "playlists" | "source_account" | "target_account" | "warnings"
> & {
  counts: StatusCounts;
  saved_album_count: number;
  followed_artist_count: number;
  entity_counts: Record<MigrationEntityType, StatusCounts>;
  playlists: PlaylistStatsView[];
  source_account: AccountHistoryView | null;
  target_account: AccountHistoryView | null;
  warnings: { code: string; message: string }[];
};
export type AggregateMigrationStatsView = Omit<
  ApiSchema<"AggregateMigrationStatsView">,
  "counts"
> & {
  counts: StatusCounts;
  total_saved_albums: number;
  total_followed_artists: number;
  entity_counts: Record<MigrationEntityType, StatusCounts>;
};
export type MigrationWarningsView = Omit<ApiSchema<"MigrationWarningsView">, "warnings"> & {
  warnings: { code: string; message: string }[];
  summary: MigrationSelectionSummary;
};
export type JobItemView = ApiSchema<"JobItemView"> & {
  entity_type: MigrationEntityType;
  source_entity_id: string | null;
  source_entity_name: string | null;
  target_entity_id: string | null;
};

export interface MigrationItemFilters {
  sourcePlaylistId?: string | null;
  entityTypes?: MigrationEntityType[];
  statuses?: string[];
  minConfidence?: number | null;
  maxConfidence?: number | null;
  reason?: string | null;
  title?: string | null;
  artist?: string | null;
  problemOnly?: boolean;
}

export interface MigrationItemPage {
  items: JobItemView[];
  total: number;
  limit: number;
  offset: number;
}

export interface ProgressEvent {
  job?: JobView;
  items?: JobItemView[];
  job_id?: string;
  missing?: boolean;
}
