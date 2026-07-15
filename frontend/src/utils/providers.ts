export function providerLabel(provider: string | null | undefined): string {
  if (!provider) return "provider";
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    return "YouTube Music";
  }
  if (provider === "spotify") return "Spotify";
  if (provider === "applemusic" || provider === "apple_music") return "Apple Music";
  if (provider === "tidal") return "Tidal";
  if (provider === "deezer") return "Deezer";
  if (provider === "text") return "Pasted text";
  if (provider === "openplaylist") return "Open Playlist Engine";
  return provider
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}
