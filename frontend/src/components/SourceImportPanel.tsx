import { Check, FileCheck2, Link2, RotateCcw, Trash2, Wifi } from "lucide-react";
import type { AccountView, SourceImportPreview } from "../api/types";
import ProviderIcon from "./ProviderIcon";
import { providerLabel } from "../utils/providers";

const MAX_VISIBLE_ISSUES = 20;

interface Props {
  provider: "public_url" | "pasted_text";
  preview: SourceImportPreview | null;
  busy: boolean;
  url: string;
  text: string;
  name: string;
  onUrlChange: (value: string) => void;
  onTextChange: (value: string) => void;
  onNameChange: (value: string) => void;
  onPreview: () => void;
  onDiscard: () => void;
  requiredProvider: string | null;
  requiredAccount: AccountView | null;
  onConnect: (provider: string) => void;
  onTestConnection: (account: AccountView) => void;
}

export default function SourceImportPanel({
  provider,
  preview,
  busy,
  url,
  text,
  name,
  onUrlChange,
  onTextChange,
  onNameChange,
  onPreview,
  onDiscard,
  requiredProvider,
  requiredAccount,
  onConnect,
  onTestConnection,
}: Props) {
  const isUrl = provider === "public_url";
  const title = isUrl ? "Public playlist URL" : "Pasted playlist text";
  const previewDisabled = busy || (isUrl ? !url.trim() : !text.trim());

  return (
    <div className="account-panel source-import-panel">
      <div className="account-heading">
        <ProviderIcon provider={provider} />
        <div>
          <span className="account-role">Source</span>
          <h3>{title}</h3>
        </div>
      </div>

      {isUrl ? (
        <div className="import-fields">
          <label htmlFor="sourceImportUrl">Public playlist URL</label>
          <input
            id="sourceImportUrl"
            type="url"
            value={url}
            disabled={busy}
            onChange={(event) => onUrlChange(event.target.value)}
            placeholder="https://music.youtube.com/playlist?list=..."
          />
          <p className="muted import-security-note">
            HTTPS only. Only links from supported playlist hosts are accepted; redirects,
            response size, and requests to private or internal network destinations are
            strictly limited.
          </p>
        </div>
      ) : (
        <div className="import-fields">
          <label htmlFor="sourceImportName">Playlist name (optional)</label>
          <input
            id="sourceImportName"
            value={name}
            disabled={busy}
            onChange={(event) => onNameChange(event.target.value)}
            placeholder="Imported track list"
          />
          <label htmlFor="sourceImportText">Tracks</label>
          <textarea
            id="sourceImportText"
            value={text}
            disabled={busy}
            onChange={(event) => onTextChange(event.target.value)}
            placeholder={"# Comments start with #\nBjörk - Jóga\nMassive Attack\tTeardrop\tMezzanine"}
          />
          <p className="muted import-security-note">
            One track per line. Use "Artist - Title", tab-separated columns, or "Artist -
            Title (Album)". Duplicate lines and Unicode text are preserved.
          </p>
        </div>
      )}

      <button
        className="primary import-preview-button"
        disabled={previewDisabled}
        onClick={onPreview}
      >
        {busy ? "Previewing…" : preview ? "Refresh preview" : "Preview import"}
      </button>

      {requiredProvider ? (
        <div className="source-connection-required">
          <p className="eyebrow">
            <Link2 aria-hidden="true" />
            Source access required
          </p>
          <p className="muted">
            Connect {providerLabel(requiredProvider)} to read this playlist, then preview
            again.
          </p>
          {requiredAccount ? (
            <>
              <p className="connected">
                <Check aria-hidden="true" />
                Connected as{" "}
                {requiredAccount.display_name ?? requiredAccount.provider_user_id ?? requiredAccount.id}
              </p>
              <button
                className="secondary compact"
                disabled={busy}
                onClick={() => onTestConnection(requiredAccount)}
              >
                <Wifi aria-hidden="true" />
                Test connection
              </button>
              <button
                className="secondary compact"
                disabled={busy}
                onClick={() => onConnect(requiredProvider)}
              >
                <RotateCcw aria-hidden="true" />
                Reconnect
              </button>
            </>
          ) : (
            <button className="secondary compact" disabled={busy} onClick={() => onConnect(requiredProvider)}>
              <ProviderIcon provider={requiredProvider} className="provider-icon-inline" />
              Connect {providerLabel(requiredProvider)}
            </button>
          )}
        </div>
      ) : null}

      {preview ? (
        <div className="import-manifest">
          <div className="import-manifest-rail" aria-hidden="true">
            {isUrl ? "URL" : "TEXT"}
          </div>
          <div className="import-manifest-body">
            <div className="import-manifest-heading">
              <span className="import-ready-icon" aria-hidden="true">
                <FileCheck2 />
              </span>
              <div>
                <strong>{preview.playlist.name}</strong>
                <span>{preview.source_label}</span>
              </div>
              <span className="format-stamp">{preview.track_count} tracks</span>
            </div>
            <p className="import-expiry">
              Preview expires {formatExpiry(preview.expires_at)}. The migration worker uses
              this exact snapshot from {preview.source_locator}, even if the source changes
              later. {preview.unsupported_count} unsupported item
              {preview.unsupported_count === 1 ? "" : "s"} stay visible but unselected below.
            </p>
            {preview.issues.length > 0 ? (
              <details className="import-issues" open={false}>
                <summary>
                  {preview.issues.length} parsing or compatibility warning
                  {preview.issues.length === 1 ? "" : "s"}
                </summary>
                <ul>
                  {preview.issues.slice(0, MAX_VISIBLE_ISSUES).map((issue, index) => (
                    <li key={`${issue.code}-${issue.line ?? index}`}>
                      <span className={`issue-marker issue-${issue.severity}`}>
                        {issue.severity}
                      </span>
                      <span>
                        {issue.line ? `Line ${issue.line}: ` : ""}
                        {issue.message}
                      </span>
                    </li>
                  ))}
                </ul>
                {preview.issues.length > MAX_VISIBLE_ISSUES ? (
                  <p className="muted">
                    {preview.issues.length - MAX_VISIBLE_ISSUES} more warnings are not shown.
                  </p>
                ) : null}
              </details>
            ) : (
              <p className="connected">Previewed with no parsing findings.</p>
            )}
            <div className="toolbar">
              <button className="secondary compact" disabled={busy} onClick={onDiscard}>
                <Trash2 aria-hidden="true" />
                Discard preview
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function formatExpiry(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "soon";
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}
