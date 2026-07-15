export function providerLabel(provider: string | null | undefined): string {
  if (!provider) return "provider";
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    return "YouTube Music";
  }
  if (provider === "spotify") return "Spotify";
  if (provider === "applemusic" || provider === "apple_music") return "Apple Music";
  if (provider === "tidal") return "Tidal";
  if (provider === "deezer") return "Deezer";
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

export function providerTrackUrl(provider: string, uri: string): string | null {
  const trimmed = uri.trim();
  if (/^https?:\/\//.test(trimmed)) return trimmed;
  if (provider === "spotify") {
    const match = trimmed.match(/^spotify:track:([^/?#&\s]+)$/);
    return match ? `https://open.spotify.com/track/${encodeURIComponent(match[1])}` : null;
  }
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    const match = trimmed.match(/^ytmusic:video:([^/?#&\s]+)$/);
    return match ? `https://music.youtube.com/watch?v=${encodeURIComponent(match[1])}` : null;
  }
  if (provider === "tidal") {
    const match = trimmed.match(/^(?:tidal:track:)?([^/?#&\s]+)$/);
    return match ? `https://tidal.com/browse/track/${encodeURIComponent(match[1])}` : null;
  }
  return null;
}
