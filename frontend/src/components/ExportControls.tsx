import { Download, FileArchive } from "lucide-react";
import { useState } from "react";
import type { ExportDownloadResult, ExportFormat } from "../api/types";

interface Props {
  disabled?: boolean;
  onExport: (format: ExportFormat) => Promise<ExportDownloadResult>;
  buttonLabel?: string;
}

const FORMATS: Array<{ value: ExportFormat; label: string }> = [
  { value: "json", label: "Open Playlist JSON (lossless)" },
  { value: "csv", label: "CSV spreadsheet" },
  { value: "txt", label: "TXT table" },
  { value: "m3u8", label: "M3U8 playlist" },
  { value: "xspf", label: "XSPF playlist" },
];

export default function ExportControls({
  disabled = false,
  onExport,
  buttonLabel = "Download export",
}: Props) {
  const [format, setFormat] = useState<ExportFormat>("json");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleExport() {
    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const result = await onExport(format);
      setMessage(
        result.warningCount > 0
          ? `${result.filename} downloaded with ${result.warningCount} warning${
              result.warningCount === 1 ? "" : "s"
            }. Review its manifest, comments, or warning fields.`
          : `${result.filename} downloaded.`,
      );
    } catch (exportError: unknown) {
      setError(exportError instanceof Error ? exportError.message : String(exportError));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="export-control-block">
      <div className="export-controls">
        <span className="export-local-mark" aria-hidden="true">
          <FileArchive />
          Local file
        </span>
        <label className="export-format-field">
          Format
          <select
            value={format}
            disabled={disabled || busy}
            onChange={(event) => setFormat(event.target.value as ExportFormat)}
          >
            {FORMATS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <button
          className="secondary export-download"
          type="button"
          disabled={disabled || busy}
          onClick={() => void handleExport()}
        >
          <Download aria-hidden="true" />
          {busy ? "Preparing..." : buttonLabel}
        </button>
      </div>
      {message ? (
        <p className="export-message" role="status">
          {message}
        </p>
      ) : null}
      {error ? (
        <p className="warn export-message" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
