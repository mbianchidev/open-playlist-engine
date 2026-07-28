export function providerLabel(provider: string | null | undefined): string {
  if (!provider) return "provider";
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    return "YouTube Music";
  }
  if (provider === "spotify") return "Spotify";
  if (provider === "applemusic" || provider === "apple_music") return "Apple Music";
  if (provider === "tidal") return "Tidal";
  if (provider === "deezer") return "Deezer";
  if (provider === "local_file") return "Local playlist file";
  if (provider === "public_url") return "Public playlist URL";
  if (provider === "pasted_text") return "Pasted playlist text";
  if (provider === "openplaylist") return "Open Playlist Engine";
  return provider
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function targetPlaylistUrl(provider: string, playlistId: string): string | null {
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    return `https://music.youtube.com/playlist?list=${encodeURIComponent(playlistId)}`;
  }
  if (provider === "spotify") {
    return `https://open.spotify.com/playlist/${encodeURIComponent(playlistId)}`;
  }
  if (provider === "tidal") {
    return `https://tidal.com/browse/playlist/${encodeURIComponent(playlistId)}`;
  }
  return null;
}

export function providerEntityUrl(
  provider: string,
  uri: string,
  entityType: "track" | "album" | "artist",
): string | null {
  const trimmed = uri.trim();
  if (/^https?:\/\//.test(trimmed)) return trimmed;
  if (provider === "spotify") {
    const match = trimmed.match(
      new RegExp(`^spotify:${entityType}:([^/?#&\\s]+)$`),
    );
    return match
      ? `https://open.spotify.com/${entityType}/${encodeURIComponent(match[1])}`
      : null;
  }
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    if (entityType !== "track") return null;
    const match = trimmed.match(/^ytmusic:video:([^/?#&\s]+)$/);
    return match ? `https://music.youtube.com/watch?v=${encodeURIComponent(match[1])}` : null;
  }
  if (provider === "tidal") {
    const match = trimmed.match(
      new RegExp(`^(?:tidal:${entityType}:)?([^/?#&\\s]+)$`),
    );
    return match
      ? `https://tidal.com/browse/${entityType}/${encodeURIComponent(match[1])}`
      : null;
  }
  return null;
}

export function providerTrackUrl(provider: string, uri: string): string | null {
  return providerEntityUrl(provider, uri, "track");
}
