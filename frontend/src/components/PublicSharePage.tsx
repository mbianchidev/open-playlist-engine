import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  Clock3,
  Disc3,
  Download,
  ExternalLink,
  EyeOff,
  Globe2,
  LockKeyhole,
  Music2,
  ShieldCheck,
  UploadCloud,
  Wifi,
} from "lucide-react";
import {
  ApiError,
  beginRecipientAuth,
  completeRecipientAuth,
  getProviders,
  getPublicShare,
  getRecipientAccounts,
  importPublicShare,
  publicShareDownloadUrl,
  recipientMigrationProgressApi,
} from "../api/client";
import type {
  AccountView,
  AuthChallenge,
  MigrationWarningsView,
  PortableFormat,
  ProviderView,
  PublicShareView,
} from "../api/types";
import { providerLabel } from "../utils/providers";
import ProgressBoard from "./ProgressBoard";
import ProviderIcon from "./ProviderIcon";

interface Props {
  token: string;
}

interface ActiveChallenge {
  provider: string;
  challenge: AuthChallenge;
}

export default function PublicSharePage({ token }: Props) {
  const [share, setShare] = useState<PublicShareView | null>(null);
  const [providers, setProviders] = useState<ProviderView[]>([]);
  const [accounts, setAccounts] = useState<AccountView[]>([]);
  const [target, setTarget] = useState("");
  const [activeChallenge, setActiveChallenge] = useState<ActiveChallenge | null>(null);
  const [formValue, setFormValue] = useState("");
  const [musicKitReady, setMusicKitReady] = useState(() => Boolean(window.MusicKit));
  const [appleConfigured, setAppleConfigured] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const authRun = useRef(0);
  const progressApi = useMemo(() => recipientMigrationProgressApi(token), [token]);

  const targetProviders = providers.filter((provider) => provider.can_target);
  const targetAccount = accounts.find((account) => account.provider === target) ?? null;
  const challengeField = activeChallenge ? firstFormField(activeChallenge.challenge) : null;

  useEffect(() => {
    let cancelled = false;
    Promise.all([getPublicShare(token), getProviders(), getRecipientAccounts(token)])
      .then(([nextShare, nextProviders, nextAccounts]) => {
        if (cancelled) return;
        setShare(nextShare);
        setProviders(nextProviders);
        setAccounts(nextAccounts);
        const targets = nextProviders.filter((provider) => provider.can_target);
        setTarget(
          nextAccounts.find((account) =>
            targets.some((provider) => provider.name === account.provider),
          )?.provider ??
            targets[0]?.name ??
            "",
        );
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        const message = errorMessage(reason);
        if (reason instanceof ApiError && [404, 410].includes(reason.status)) {
          setUnavailable(message);
        } else {
          setError(message);
        }
      });
    return () => {
      cancelled = true;
      authRun.current += 1;
    };
  }, [token]);

  useEffect(() => {
    function onMusicKitLoaded() {
      setMusicKitReady(true);
    }
    document.addEventListener("musickitloaded", onMusicKitLoaded);
    return () => document.removeEventListener("musickitloaded", onMusicKitLoaded);
  }, []);

  useEffect(() => {
    setActiveChallenge(null);
    setFormValue("");
    setAppleConfigured(false);
    authRun.current += 1;
  }, [target]);

  useEffect(() => {
    let cancelled = false;
    const developerToken =
      activeChallenge?.provider === "applemusic"
        ? appleDeveloperToken(activeChallenge.challenge)
        : null;
    if (!developerToken || !musicKitReady || !window.MusicKit) {
      setAppleConfigured(false);
      return;
    }
    void window.MusicKit.configure({
      developerToken,
      app: { name: "Open Playlist Engine", build: "0.1.0" },
    })
      .then(() => {
        if (!cancelled) setAppleConfigured(true);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(`Apple Music setup failed: ${errorMessage(reason)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [activeChallenge, musicKitReady]);

  if (unavailable) {
    return (
      <main className="public-share-page public-share-unavailable">
        <section className="public-unavailable-card">
          <LockKeyhole aria-hidden="true" />
          <p className="eyebrow">Playlist unavailable</p>
          <h1>This share link can no longer be opened</h1>
          <p>{unavailable}</p>
        </section>
      </main>
    );
  }

  if (!share) {
    return (
      <main className="public-share-page public-share-loading">
        <Disc3 aria-hidden="true" />
        <p>{error ?? "Opening playlist snapshot…"}</p>
      </main>
    );
  }

  const snapshot = share.snapshot;
  return (
    <main className="public-share-page">
      <header className="public-share-brand">
        <a href="/" className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            <Music2 />
            <ArrowRight />
          </span>
          <span>
            <strong>Open Playlist Engine</strong>
            <small>Portable playlist snapshot</small>
          </span>
        </a>
        <span className="public-trust-mark">
          <ShieldCheck aria-hidden="true" />
          View-only until you connect your account
        </span>
      </header>

      <section className="playlist-passport">
        <div className="share-vinyl" data-has-cover={Boolean(snapshot.cover_url)}>
          {snapshot.cover_url ? (
            <img
              src={snapshot.cover_url}
              alt=""
              referrerPolicy="no-referrer"
              loading="eager"
            />
          ) : (
            <Disc3 aria-hidden="true" />
          )}
        </div>
        <div className="passport-copy">
          <div className="passport-stamps">
            <span>
              <ProviderIcon provider={snapshot.source.provider} />
              From {providerLabel(snapshot.source.provider)}
            </span>
            <span>
              {share.visibility === "public" ? <Globe2 /> : <EyeOff />}
              {share.visibility}
            </span>
            {share.expires_at ? (
              <span>
                <Clock3 />
                Expires {formatDate(share.expires_at)}
              </span>
            ) : null}
          </div>
          <p className="eyebrow">Shared playlist passport</p>
          <h1>{snapshot.name}</h1>
          {snapshot.attribution ? <p className="passport-attribution">{snapshot.attribution}</p> : null}
          {snapshot.description ? <p className="passport-description">{snapshot.description}</p> : null}
          <div className="passport-actions">
            {snapshot.source.url ? (
              <a className="button-link" href={snapshot.source.url} target="_blank" rel="noreferrer">
                Open original
                <ExternalLink aria-hidden="true" />
              </a>
            ) : null}
            <span>{snapshot.tracks.length} tracks frozen in this snapshot</span>
          </div>
        </div>
      </section>

      <div className="public-share-grid">
        <section className="card public-track-manifest">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Manifest</p>
              <h2>Track list</h2>
            </div>
            <span className="manifest-count">{snapshot.tracks.length}</span>
          </div>
          <ol>
            {snapshot.tracks.map((track, index) => (
              <li key={`${track.position}:${track.title}:${track.artist}`}>
                <span className="track-index">{String(index + 1).padStart(2, "0")}</span>
                <span className="track-copy">
                  <strong>{track.title}</strong>
                  <span>
                    {track.artist}
                    {track.album ? ` · ${track.album}` : ""}
                  </span>
                  {track.unsupported_reason ? (
                    <small>{track.unsupported_reason}</small>
                  ) : null}
                </span>
                {track.duration_s !== null ? (
                  <time>{formatDuration(track.duration_s)}</time>
                ) : null}
                {track.source_url ? (
                  <a
                    className="track-source-link"
                    href={track.source_url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`Open ${track.title} at the source`}
                  >
                    <ExternalLink />
                  </a>
                ) : null}
              </li>
            ))}
          </ol>
        </section>

        <aside className="public-action-stack">
          <section className="card download-deck">
            <p className="eyebrow">Keep a local copy</p>
            <h2>Download the snapshot</h2>
            <p className="muted">Choose a portable format. Downloads contain metadata, never audio.</p>
            <div className="download-format-grid">
              {share.download_formats.map((format) => (
                <a
                  key={format}
                  href={publicShareDownloadUrl(token, format)}
                  className="download-format"
                >
                  <Download aria-hidden="true" />
                  <span>
                    <strong>{formatLabel(format)}</strong>
                    <small>{formatDescription(format)}</small>
                  </span>
                </a>
              ))}
            </div>
          </section>

          <section className="card import-deck">
            <p className="eyebrow">Send it to your service</p>
            <h2>Import with your own account</h2>
            <p className="muted">
              The publisher’s accounts are never available here. Connect a target account that
              belongs to you.
            </p>
            <label>
              Target service
              <span className="select-with-icon">
                <ProviderIcon provider={target} />
                <select value={target} onChange={(event) => setTarget(event.target.value)}>
                  {targetProviders.map((provider) => (
                    <option key={provider.name} value={provider.name}>
                      {provider.display_name}
                    </option>
                  ))}
                </select>
              </span>
            </label>

            {targetAccount ? (
              <div className="recipient-account">
                <Wifi aria-hidden="true" />
                <span>
                  <strong>Connected</strong>
                  <small>
                    {targetAccount.display_name ??
                      targetAccount.provider_user_id ??
                      targetAccount.id}
                  </small>
                </span>
                <button className="secondary compact" onClick={() => void connectProvider(target)}>
                  Reconnect
                </button>
              </div>
            ) : (
              <button
                className="secondary recipient-connect"
                disabled={busy || !target}
                onClick={() => void connectProvider(target)}
              >
                <ProviderIcon provider={target} />
                Connect {target ? providerLabel(target) : "a target service"}
              </button>
            )}

            {activeChallenge?.challenge.shape === "device_code" ? (
              <div className="recipient-challenge">
                <p>Open the verification page and enter:</p>
                <code>{activeChallenge.challenge.user_code}</code>
                <a
                  href={activeChallenge.challenge.verification_url ?? "#"}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open verification page
                  <ExternalLink />
                </a>
                <small>Waiting for authorization…</small>
              </div>
            ) : null}

            {activeChallenge?.challenge.shape === "form" && challengeField ? (
              <div className="recipient-challenge">
                <p>{activeChallenge.challenge.instructions}</p>
                {challengeField.format === "musickit" ? (
                  <button
                    className="primary"
                    disabled={busy || !appleConfigured}
                    onClick={() => void authorizeAppleMusic()}
                  >
                    {appleConfigured ? "Authorize with Apple Music" : "Preparing MusicKit…"}
                  </button>
                ) : (
                  <>
                    <label htmlFor="recipientAuthValue">
                      {challengeField.name === "headers_raw"
                        ? "YouTube Music request headers"
                        : "Provider credential"}
                    </label>
                    <textarea
                      id="recipientAuthValue"
                      value={formValue}
                      onChange={(event) => setFormValue(event.target.value)}
                      placeholder="Paste only into a self-host you trust."
                    />
                    <p className="warn">
                      This credential is encrypted on the publisher’s server and expires from its
                      recipient account store.
                    </p>
                    <button
                      className="primary"
                      disabled={busy || !formValue.trim()}
                      onClick={() => void completeFormAuth()}
                    >
                      Connect account
                    </button>
                  </>
                )}
              </div>
            ) : null}

            <button
              className="primary recipient-import"
              disabled={busy || !target || !targetAccount}
              onClick={() => void startImport(false)}
            >
              <UploadCloud aria-hidden="true" />
              {busy ? "Starting import…" : "Import this snapshot"}
            </button>
            {notice ? <p className="notice">{notice}</p> : null}
            {error ? <p className="warn">{error}</p> : null}
          </section>
        </aside>
      </div>

      {jobId ? (
        <section className="public-progress">
          <ProgressBoard
            jobId={jobId}
            api={progressApi}
            onReconnectProvider={connectProvider}
          />
        </section>
      ) : null}
    </main>
  );

  async function refreshAccounts(provider?: string): Promise<AccountView[]> {
    const next = await getRecipientAccounts(token);
    setAccounts(next);
    if (provider && next.some((account) => account.provider === provider)) {
      setNotice(`${providerLabel(provider)} connected.`);
    }
    return next;
  }

  async function connectProvider(providerName: string) {
    if (!providerName) return;
    if (providerName !== target) setTarget(providerName);
    const runId = authRun.current + 1;
    authRun.current = runId;
    setBusy(true);
    setError(null);
    setNotice(null);
    setFormValue("");
    try {
      const challenge = await beginRecipientAuth(token, providerName);
      setActiveChallenge({ provider: providerName, challenge });
      if (challenge.shape === "redirect" && challenge.redirect_url) {
        window.open(challenge.redirect_url, "_blank", "noopener,noreferrer");
        setNotice(`Finish ${providerLabel(providerName)} authorization in the new tab.`);
        void pollForAccount(providerName, runId);
      } else if (challenge.shape === "device_code" && challenge.state) {
        void pollDeviceAuth(providerName, challenge, runId);
      }
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function pollForAccount(providerName: string, runId: number) {
    for (let attempt = 0; attempt < 60 && authRun.current === runId; attempt += 1) {
      await sleep(2000);
      const next = await refreshAccounts();
      if (next.some((account) => account.provider === providerName)) {
        setActiveChallenge(null);
        setNotice(`${providerLabel(providerName)} connected.`);
        return;
      }
    }
  }

  async function pollDeviceAuth(
    providerName: string,
    challenge: AuthChallenge,
    runId: number,
  ) {
    let intervalS = Math.max(1, challenge.poll_interval_s ?? 5);
    while (authRun.current === runId) {
      await sleep(intervalS * 1000);
      try {
        await completeRecipientAuth(token, providerName, { state: challenge.state });
        if (authRun.current !== runId) return;
        setActiveChallenge(null);
        await refreshAccounts(providerName);
        return;
      } catch (reason: unknown) {
        const message = errorMessage(reason);
        if (message === "authorization_pending") continue;
        if (message === "slow_down") {
          intervalS += 5;
          continue;
        }
        setError(message);
        return;
      }
    }
  }

  async function completeFormAuth() {
    if (!activeChallenge || !challengeField) return;
    setBusy(true);
    setError(null);
    try {
      await completeRecipientAuth(token, activeChallenge.provider, {
        [challengeField.name]: formValue.trim(),
      });
      setActiveChallenge(null);
      setFormValue("");
      await refreshAccounts(activeChallenge.provider);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function authorizeAppleMusic() {
    if (!activeChallenge || !window.MusicKit || !appleConfigured) return;
    setBusy(true);
    setError(null);
    try {
      const musicUserToken = await window.MusicKit.getInstance().authorize();
      await completeRecipientAuth(token, activeChallenge.provider, {
        music_user_token: musicUserToken,
      });
      setActiveChallenge(null);
      await refreshAccounts(activeChallenge.provider);
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function startImport(acknowledgeWarnings: boolean) {
    if (!targetAccount) return;
    setBusy(true);
    setError(null);
    try {
      const job = await importPublicShare(token, {
        target_provider: target,
        target_account_id: targetAccount.id,
        acknowledge_warnings: acknowledgeWarnings,
      });
      setJobId(job.id);
      setNotice("Import started. Keep this page open for review.");
    } catch (reason: unknown) {
      if (isMigrationWarning(reason) && !acknowledgeWarnings) {
        const approved = window.confirm(warningMessage(reason.detail));
        if (approved) {
          setBusy(false);
          await startImport(true);
          return;
        }
      } else {
        setError(errorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  }
}

function firstFormField(challenge: AuthChallenge): { name: string; format: string | null } | null {
  if (!challenge.form_schema) return null;
  const entry = Object.entries(challenge.form_schema)[0];
  if (!entry) return null;
  const [name, raw] = entry;
  const field = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return { name, format: typeof field.format === "string" ? field.format : null };
}

function appleDeveloperToken(challenge: AuthChallenge): string | null {
  const raw = challenge.form_schema?.music_user_token;
  if (!raw || typeof raw !== "object") return null;
  const token = (raw as Record<string, unknown>).developer_token;
  return typeof token === "string" && token ? token : null;
}

function isMigrationWarning(
  error: unknown,
): error is ApiError & { detail: MigrationWarningsView } {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  if (!error.detail || typeof error.detail !== "object") return false;
  const detail = error.detail as Partial<MigrationWarningsView>;
  return detail.code === "migration_warnings" && Array.isArray(detail.warnings);
}

function warningMessage(detail: MigrationWarningsView): string {
  return [detail.message, "", ...detail.warnings.map((warning) => warning.message), "", "Continue?"].join(
    "\n",
  );
}

function formatDuration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(value));
}

function formatLabel(format: PortableFormat): string {
  return format === "m3u8" ? "M3U8" : format.toUpperCase();
}

function formatDescription(format: PortableFormat): string {
  return {
    json: "Lossless round trip",
    csv: "Spreadsheet friendly",
    txt: "Simple track list",
    m3u8: "Playlist players",
    xspf: "Open XML playlist",
  }[format];
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}
