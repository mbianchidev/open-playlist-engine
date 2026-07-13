export function providerLabel(provider: string | null | undefined): string {
  if (!provider) return "provider";
  if (provider === "ytmusic" || provider === "youtube" || provider === "youtube_music") {
    return "YouTube Music";
  }
  if (provider === "spotify") return "Spotify";
  return provider;
}
