import { FileCheck2, FileMusic, Trash2, Upload } from "lucide-react";
import type { LocalImportPreview } from "../api/types";

const ACCEPTED_FORMATS = ".txt,.csv,.m3u,.m3u8,.pls,.wpl,.xspf,.xml,.json";
const MAX_VISIBLE_ISSUES = 50;

interface Props {
  preview: LocalImportPreview | null;
  busy: boolean;
  onUpload: (file: File) => void;
  onDiscard: () => void;
}

export default function LocalFileImportPanel({ preview, busy, onUpload, onDiscard }: Props) {
  function chooseFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) onUpload(file);
  }

  return (
    <div className="account-panel local-import-panel">
      <div className="account-heading">
        <span className="provider-icon" data-provider="local_file" aria-hidden="true">
          <FileMusic />
        </span>
        <div>
          <span className="account-role">Source</span>
          <h3>Local playlist file</h3>
        </div>
      </div>

      {!preview ? (
        <>
          <label className={`local-import-dropzone ${busy ? "is-busy" : ""}`}>
            <input
              type="file"
              accept={ACCEPTED_FORMATS}
              disabled={busy}
              onChange={chooseFile}
            />
            <Upload aria-hidden="true" />
            <strong>{busy ? "Reading playlist…" : "Choose a playlist file"}</strong>
            <span>TXT, CSV, M3U, M3U8, PLS, WPL, XSPF, XML, or JSON</span>
          </label>
          <p className="local-import-privacy">
            Parsed here and stored only as an expiring local preview. Audio files are never read or
            uploaded.
          </p>
        </>
      ) : (
        <div className="import-manifest">
          <div className="import-manifest-rail" aria-hidden="true">
            LOCAL
          </div>
          <div className="import-manifest-body">
            <div className="import-manifest-heading">
              <span className="import-ready-icon" aria-hidden="true">
                <FileCheck2 />
              </span>
              <div>
                <strong>{preview.filename}</strong>
                <span>
                  {formatBytes(preview.file_size)} · {preview.encoding ?? "format encoding"}
                </span>
              </div>
              <span className="format-stamp">{preview.detected_format.toUpperCase()}</span>
            </div>
            <dl className="import-metrics">
              <div>
                <dt>Playlists</dt>
                <dd>{preview.playlist_count}</dd>
              </div>
              <div>
                <dt>Tracks</dt>
                <dd>{preview.track_count}</dd>
              </div>
              <div>
                <dt>Duplicates</dt>
                <dd>{preview.duplicate_count}</dd>
              </div>
              <div>
                <dt>Skipped</dt>
                <dd>{preview.unsupported_count}</dd>
              </div>
            </dl>
            <p className="import-expiry">
              Preview expires {formatExpiry(preview.expires_at)}. Duplicate ordering is preserved;
              unsupported entries stay visible below.
            </p>
            {preview.issues.length > 0 ? (
              <details className="import-issues" open={preview.malformed_count > 0}>
                <summary>
                  {preview.issues.length} validation finding
                  {preview.issues.length === 1 ? "" : "s"}
                </summary>
                <ul>
                  {preview.issues.slice(0, MAX_VISIBLE_ISSUES).map((issue, index) => (
                    <li key={`${issue.code}-${issue.line_or_item ?? index}`}>
                      <span className={`issue-marker issue-${issue.severity}`}>
                        {issue.severity}
                      </span>
                      <span>
                        {issue.line_or_item !== null ? `Item ${issue.line_or_item}: ` : ""}
                        {issue.message}
                      </span>
                    </li>
                  ))}
                </ul>
                {preview.issues.length > MAX_VISIBLE_ISSUES ? (
                  <p className="muted">
                    Showing the first {MAX_VISIBLE_ISSUES} findings. Fix those entries and upload
                    again for the remaining details.
                  </p>
                ) : null}
              </details>
            ) : (
              <p className="connected">File validated with no parse findings.</p>
            )}
            <div className="toolbar">
              <label className="button-label secondary">
                <Upload aria-hidden="true" />
                Replace file
                <input
                  type="file"
                  accept={ACCEPTED_FORMATS}
                  disabled={busy}
                  onChange={chooseFile}
                />
              </label>
              <button className="secondary compact" disabled={busy} onClick={onDiscard}>
                <Trash2 aria-hidden="true" />
                Discard preview
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatExpiry(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "soon";
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}
