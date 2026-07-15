import { useEffect, useMemo, useState } from "react";
import {
  Ban,
  CalendarClock,
  Copy,
  ExternalLink,
  EyeOff,
  Globe2,
  ListMusic,
  RefreshCw,
  Share2,
} from "lucide-react";
import {
  createShare,
  expireShare,
  getPlaylists,
  getShareConfig,
  listShares,
  revokeShare,
  updateShare,
} from "../api/client";
import type {
  AccountView,
  PlaylistRef,
  ProviderView,
  ShareConfigView,
  ShareDetailView,
  ShareVisibility,
} from "../api/types";
import { providerLabel } from "../utils/providers";
import ProviderIcon from "./ProviderIcon";

interface Props {
  providers: ProviderView[];
  accounts: AccountView[];
}

export default function ShareManager({ providers, accounts }: Props) {
  const [config, setConfig] = useState<ShareConfigView | null>(null);
  const [shares, setShares] = useState<ShareDetailView[]>([]);
  const [provider, setProvider] = useState("");
  const [accountId, setAccountId] = useState("");
  const [playlists, setPlaylists] = useState<PlaylistRef[]>([]);
  const [playlistId, setPlaylistId] = useState("");
  const [attribution, setAttribution] = useState("");
  const [visibility, setVisibility] = useState<ShareVisibility>("unlisted");
  const [expiry, setExpiry] = useState("7");
  const [busy, setBusy] = useState(false);
  const [loadingPlaylists, setLoadingPlaylists] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const sourceProviders = useMemo(
    () =>
      providers.filter(
        (candidate) =>
          candidate.can_source && accounts.some((account) => account.provider === candidate.name),
      ),
    [accounts, providers],
  );
  const providerAccounts = accounts.filter((account) => account.provider === provider);

  useEffect(() => {
    void refreshShares();
  }, []);

  useEffect(() => {
    if (provider && sourceProviders.some((candidate) => candidate.name === provider)) return;
    setProvider(sourceProviders[0]?.name ?? "");
  }, [provider, sourceProviders]);

  useEffect(() => {
    if (providerAccounts.some((account) => account.id === accountId)) return;
    setAccountId(providerAccounts[0]?.id ?? "");
  }, [accountId, providerAccounts]);

  useEffect(() => {
    setPlaylists([]);
    setPlaylistId("");
    if (!provider || !accountId || !config?.enabled) return;
    void loadSourcePlaylists();
  }, [accountId, config?.enabled, provider]);

  if (!config) {
    return <section className="card sharing-empty">{error ?? "Loading sharing settings…"}</section>;
  }

  if (!config.enabled) {
    return (
      <section className="card sharing-empty">
        <span className="section-icon" aria-hidden="true">
          <EyeOff />
        </span>
        <div>
          <p className="eyebrow">Sharing is off</p>
          <h2>Public links require explicit server configuration</h2>
          <p className="muted">{config.disabled_reason}</p>
        </div>
      </section>
    );
  }

  return (
    <div className="sharing-workspace">
      {error ? <p className="warn">{error}</p> : null}
      {notice ? <p className="notice">{notice}</p> : null}

      <section className="card share-publisher">
        <div className="section-heading">
          <div className="section-title">
            <span className="section-icon" aria-hidden="true">
              <Share2 />
            </span>
            <div>
              <p className="eyebrow">Immutable snapshot</p>
              <h2>Publish a playlist passport</h2>
              <p className="muted">
                The link keeps this exact title, artwork, and track list even if the source changes.
              </p>
            </div>
          </div>
        </div>

        {sourceProviders.length === 0 ? (
          <p className="empty-guidance">
            Connect a source account in the Migration tab before publishing a playlist.
          </p>
        ) : (
          <div className="share-form-grid">
            <label>
              Source provider
              <span className="select-with-icon">
                <ProviderIcon provider={provider} />
                <select value={provider} onChange={(event) => setProvider(event.target.value)}>
                  {sourceProviders.map((candidate) => (
                    <option key={candidate.name} value={candidate.name}>
                      {candidate.display_name}
                    </option>
                  ))}
                </select>
              </span>
            </label>
            <label>
              Connected account
              <select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
                {providerAccounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.display_name ?? account.provider_user_id ?? account.id}
                  </option>
                ))}
              </select>
            </label>
            <label className="share-playlist-field">
              Playlist snapshot
              <select
                value={playlistId}
                onChange={(event) => setPlaylistId(event.target.value)}
                disabled={loadingPlaylists}
              >
                <option value="">
                  {loadingPlaylists ? "Loading playlists…" : "Choose a playlist"}
                </option>
                {playlists.map((playlist) => (
                  <option key={playlist.id} value={playlist.id}>
                    {playlist.name}
                    {playlist.track_count === null ? "" : ` · ${playlist.track_count} tracks`}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Attribution
              <input
                value={attribution}
                maxLength={500}
                placeholder="Shared by…"
                onChange={(event) => setAttribution(event.target.value)}
              />
            </label>
            <label>
              Visibility
              <select
                value={visibility}
                onChange={(event) => setVisibility(event.target.value as ShareVisibility)}
              >
                <option value="unlisted">Unlisted · no search indexing</option>
                <option value="public">Public · link may be indexed</option>
              </select>
            </label>
            <label>
              Expiration
              <select value={expiry} onChange={(event) => setExpiry(event.target.value)}>
                <option value="7">7 days</option>
                <option value="30">30 days</option>
                <option value="90">90 days</option>
                <option value="never">Never</option>
              </select>
            </label>
          </div>
        )}

        <div className="share-publish-actions">
          <button
            className="secondary compact"
            type="button"
            disabled={busy || loadingPlaylists || !provider || !accountId}
            onClick={() => void loadSourcePlaylists()}
          >
            <RefreshCw aria-hidden="true" />
            Refresh playlists
          </button>
          <button
            className="primary"
            type="button"
            disabled={busy || !provider || !accountId || !playlistId}
            onClick={() => void publish()}
          >
            <Share2 aria-hidden="true" />
            {busy ? "Publishing…" : "Publish immutable snapshot"}
          </button>
        </div>
      </section>

      <section className="sharing-library" aria-labelledby="published-shares-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Published links</p>
            <h2 id="published-shares-title">Share library</h2>
          </div>
          <button className="secondary compact" disabled={busy} onClick={() => void refreshShares()}>
            <RefreshCw aria-hidden="true" />
            Refresh
          </button>
        </div>
        {shares.length === 0 ? (
          <div className="card sharing-empty">
            <ListMusic aria-hidden="true" />
            <p>No playlist snapshots published yet.</p>
          </div>
        ) : (
          <div className="share-card-grid">
            {shares.map((share) => (
              <article key={share.id} className={`card share-card share-status-${share.status}`}>
                <div className="share-card-heading">
                  <div>
                    <span className="share-status">{share.status}</span>
                    <h3>{share.snapshot.name}</h3>
                    <p className="muted">
                      {share.snapshot.tracks.length} tracks · {providerLabel(share.snapshot.source.provider)}
                    </p>
                  </div>
                  {share.visibility === "public" ? (
                    <Globe2 aria-label="Public" />
                  ) : (
                    <EyeOff aria-label="Unlisted" />
                  )}
                </div>
                <p className="share-url">{share.url}</p>
                <p className="muted">
                  {share.expires_at
                    ? `Expires ${formatDate(share.expires_at)}`
                    : "No automatic expiration"}
                </p>
                <div className="share-card-actions">
                  <button className="secondary compact" onClick={() => void copyLink(share)}>
                    <Copy aria-hidden="true" />
                    Copy
                  </button>
                  <a className="button-link compact" href={share.url} target="_blank" rel="noreferrer">
                    Open
                    <ExternalLink aria-hidden="true" />
                  </a>
                  {share.status === "active" ? (
                    <>
                      <button
                        className="secondary compact"
                        onClick={() =>
                          void changeVisibility(
                            share,
                            share.visibility === "public" ? "unlisted" : "public",
                          )
                        }
                      >
                        {share.visibility === "public" ? <EyeOff /> : <Globe2 />}
                        Make {share.visibility === "public" ? "unlisted" : "public"}
                      </button>
                      <button className="secondary compact" onClick={() => void expire(share)}>
                        <CalendarClock aria-hidden="true" />
                        Expire now
                      </button>
                    </>
                  ) : null}
                  {share.status !== "revoked" ? (
                    <button className="secondary compact danger-action" onClick={() => void revoke(share)}>
                      <Ban aria-hidden="true" />
                      Revoke
                    </button>
                  ) : null}
                </div>
                <details className="share-inspector">
                  <summary>Inspect published snapshot</summary>
                  <ol>
                    {share.snapshot.tracks.map((track) => (
                      <li key={`${track.position}:${track.title}:${track.artist}`}>
                        <span>{track.title}</span>
                        <small>{track.artist}</small>
                      </li>
                    ))}
                  </ol>
                </details>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );

  async function refreshShares() {
    setError(null);
    try {
      const nextConfig = await getShareConfig();
      setConfig(nextConfig);
      setShares(nextConfig.enabled ? await listShares() : []);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  }

  async function loadSourcePlaylists() {
    if (!provider || !accountId) return;
    setLoadingPlaylists(true);
    setError(null);
    try {
      const rows = await getPlaylists(provider, accountId);
      setPlaylists(rows);
      setPlaylistId((current) => (rows.some((row) => row.id === current) ? current : ""));
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setLoadingPlaylists(false);
    }
  }

  async function publish() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const days = expiry === "never" ? null : Number(expiry);
      const created = await createShare({
        provider,
        account_id: accountId,
        playlist_id: playlistId,
        attribution: attribution.trim() || null,
        visibility,
        expires_at:
          days === null ? null : new Date(Date.now() + days * 24 * 60 * 60 * 1000).toISOString(),
      });
      setShares((current) => [created, ...current]);
      setNotice(`Published “${created.snapshot.name}”.`);
      setPlaylistId("");
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function copyLink(share: ShareDetailView) {
    try {
      await navigator.clipboard.writeText(share.url);
      setNotice("Share link copied.");
    } catch {
      setNotice(`Copy this link: ${share.url}`);
    }
  }

  async function changeVisibility(share: ShareDetailView, next: ShareVisibility) {
    await replaceShare(updateShare(share.id, { visibility: next }));
  }

  async function expire(share: ShareDetailView) {
    await replaceShare(expireShare(share.id));
  }

  async function revoke(share: ShareDetailView) {
    await replaceShare(revokeShare(share.id));
  }

  async function replaceShare(operation: Promise<ShareDetailView>) {
    setBusy(true);
    setError(null);
    try {
      const updated = await operation;
      setShares((current) => current.map((share) => (share.id === updated.id ? updated : share)));
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
