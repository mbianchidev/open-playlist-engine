import { useEffect, useState } from "react";
import { LockKeyhole, ShieldCheck } from "lucide-react";
import App from "../App";
import { getOwnerSession, loginOwner, logoutOwner } from "../api/client";
import type { OwnerSessionView } from "../api/types";

export default function OwnerGate() {
  const [session, setSession] = useState<OwnerSessionView | null>(null);
  const [accessToken, setAccessToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getOwnerSession().then(setSession).catch((reason: unknown) => setError(errorMessage(reason)));
  }, []);

  if (!session) {
    return (
      <main className="owner-gate">
        <div className="owner-gate-card">
          <span className="owner-gate-mark" aria-hidden="true">
            <ShieldCheck />
          </span>
          <p>{error ?? "Checking owner session…"}</p>
        </div>
      </main>
    );
  }

  if (!session.required || session.authenticated) {
    return (
      <>
        {session.required ? (
          <button className="owner-lock-button" type="button" onClick={() => void lock()}>
            <LockKeyhole aria-hidden="true" />
            Lock owner session
          </button>
        ) : null}
        <App />
      </>
    );
  }

  return (
    <main className="owner-gate">
      <section className="owner-gate-card" aria-labelledby="owner-gate-title">
        <span className="owner-gate-mark" aria-hidden="true">
          <LockKeyhole />
        </span>
        <p className="eyebrow">Private owner workspace</p>
        <h1 id="owner-gate-title">Unlock your connected accounts</h1>
        <p className="muted">
          Public playlist pages are isolated from this workspace. Enter the owner access token
          configured on this instance.
        </p>
        {!session.sharing_enabled ? (
          <p className="warn">{session.sharing_disabled_reason}</p>
        ) : (
          <form onSubmit={(event) => void unlock(event)}>
            <label htmlFor="ownerAccessToken">Owner access token</label>
            <input
              id="ownerAccessToken"
              type="password"
              autoComplete="current-password"
              value={accessToken}
              onChange={(event) => setAccessToken(event.target.value)}
              autoFocus
            />
            <button className="primary" type="submit" disabled={busy || !accessToken}>
              {busy ? "Unlocking…" : "Unlock owner workspace"}
            </button>
          </form>
        )}
        {error ? <p className="warn">{error}</p> : null}
      </section>
    </main>
  );

  async function unlock(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const next = await loginOwner(accessToken);
      setSession(next);
      setAccessToken("");
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  async function lock() {
    setError(null);
    try {
      setSession(await logoutOwner());
    } catch (reason: unknown) {
      setError(errorMessage(reason));
    }
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
