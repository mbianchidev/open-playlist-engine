import { FileMusic, Music2 } from "lucide-react";
import type { IconType } from "react-icons";
import { SiApplemusic, SiSpotify, SiTidal, SiYoutubemusic } from "react-icons/si";

const PROVIDER_ICONS: Record<string, IconType> = {
  applemusic: SiApplemusic,
  spotify: SiSpotify,
  tidal: SiTidal,
  youtube: SiYoutubemusic,
  youtube_music: SiYoutubemusic,
  ytmusic: SiYoutubemusic,
};

interface Props {
  provider: string | null | undefined;
  className?: string;
}

export default function ProviderIcon({ provider, className }: Props) {
  const normalized = provider?.toLowerCase() ?? "unknown";
  const Icon = PROVIDER_ICONS[normalized];
  const isLocalFile = normalized === "local_file";

  return (
    <span
      className={["provider-icon", className].filter(Boolean).join(" ")}
      data-provider={normalized}
      aria-hidden="true"
    >
      {isLocalFile ? <FileMusic /> : Icon ? <Icon /> : <Music2 />}
    </span>
  );
}
