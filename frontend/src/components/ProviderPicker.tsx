import { Check, LockKeyhole } from "lucide-react";
import type { ProviderView } from "../api/types";
import ProviderIcon from "./ProviderIcon";

interface Props {
  title: string;
  role: "source" | "target";
  providers: ProviderView[];
  selected: string | null;
  onSelect: (name: string) => void;
}

export default function ProviderPicker({ title, role, providers, selected, onSelect }: Props) {
  return (
    <section className={`card provider-picker provider-picker-${role}`}>
      <div className="provider-picker-heading">
        <span className="step-number">{role === "source" ? "1" : "2"}</span>
        <div>
          <h2>{title}</h2>
          <p className="muted">
            {role === "source" ? "Choose where your playlists live." : "Choose their new home."}
          </p>
        </div>
      </div>
      <div className="provider-options">
        {providers.map((p) => {
          const eligible = role === "source" ? p.can_source : p.can_target;
          const isSelected = selected === p.name;
          return (
            <button
              key={p.name}
              className={`provider provider-${p.name}`}
              aria-pressed={isSelected}
              disabled={!eligible}
              onClick={() => onSelect(p.name)}
            >
              <ProviderIcon provider={p.name} />
              <span className="provider-copy">
                <strong>{p.display_name}</strong>
                <span className="provider-meta">
                  {p.official ? "Official integration" : "Community integration"}
                </span>
                {p.warning ? <span className="warn">{p.warning}</span> : null}
              </span>
              <span className="provider-state" aria-hidden="true">
                {!eligible ? <LockKeyhole /> : isSelected ? <Check /> : null}
              </span>
              <span className="badge">{p.stability}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
