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
  can_mirror: boolean;
  mirror_unavailable_reason: string | null;
  can_unfollow_playlist: boolean;
  can_delete_playlist: boolean;
  can_remove_tracks: boolean;
  max_remove_batch: number;
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

export interface OwnerSessionView {
  required: boolean;
  authenticated: boolean;
  sharing_enabled: boolean;
  sharing_disabled_reason: string;
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

export type SyncMode = "add_only" | "mirror";

export interface CreateSyncRuleBody {
  migration_job_id: string;
  mode: SyncMode;
  cadence_minutes: number;
  timezone: string;
}

export interface UpdateSyncRuleBody {
  mode?: SyncMode;
  cadence_minutes?: number;
  timezone?: string;
}

export interface SyncRunView {
  id: string;
  trigger: string;
  status: string;
  migration_job_id: string | null;
  added: number;
  removed: number;
  reordered: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

export interface SyncRuleView {
  id: string;
  source_provider: string;
  source_account_id: string;
  source_playlist_id: string;
  source_playlist_name: string;
  target_provider: string;
  target_account_id: string;
  target_playlist_id: string;
  target_playlist_name: string;
  mode: SyncMode;
  cadence_minutes: number;
  timezone: string;
  enabled: boolean;
  status: string;
  last_run_at: string | null;
  last_success_at: string | null;
  next_run_at: string | null;
  last_error: string | null;
  last_added: number;
  last_removed: number;
  last_reordered: number;
  latest_run: SyncRunView | null;
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

export type ShareVisibility = "public" | "unlisted";
export type PortableFormat = "json" | "csv" | "txt" | "m3u8" | "xspf";

export interface SharedSource {
  provider: string;
  url: string | null;
}

export interface SharedTrack {
  position: number;
  title: string;
  artist: string;
  album: string | null;
  duration_s: number | null;
  release_year: number | null;
  explicit: boolean | null;
  isrc: string | null;
  artwork_url: string | null;
  source_url: string | null;
  media_type: string;
  unsupported_reason: string | null;
}

export interface SharedPlaylistSnapshot {
  schema_version: string;
  name: string;
  description: string | null;
  cover_url: string | null;
  attribution: string | null;
  source: SharedSource;
  tracks: SharedTrack[];
}

export interface ShareConfigView {
  enabled: boolean;
  disabled_reason: string;
  public_base_url: string | null;
  max_tracks: number;
  max_expiry_days: number;
  supported_download_formats: PortableFormat[];
}

export interface ShareDetailView {
  id: string;
  url: string;
  status: "active" | "expired" | "revoked";
  visibility: ShareVisibility;
  snapshot: SharedPlaylistSnapshot;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface PublicShareView {
  visibility: ShareVisibility;
  snapshot: SharedPlaylistSnapshot;
  expires_at: string | null;
  download_formats: PortableFormat[];
}

export interface CreateShareBody {
  provider: string;
  account_id: string;
  playlist_id: string;
  attribution?: string | null;
  visibility: ShareVisibility;
  expires_at?: string | null;
}
