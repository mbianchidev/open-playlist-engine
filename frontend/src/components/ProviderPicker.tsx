import type { ProviderView } from "../api/types";

interface Props {
  title: string;
  role: "source" | "target";
  providers: ProviderView[];
  selected: string | null;
  onSelect: (name: string) => void;
}

export default function ProviderPicker({ title, role, providers, selected, onSelect }: Props) {
  return (
    <section className="card">
      <h2>{title}</h2>
      {providers.map((p) => {
        const eligible = role === "source" ? p.can_source : p.can_target;
        return (
          <button
            key={p.name}
            className="provider"
            aria-pressed={selected === p.name}
            disabled={!eligible}
            onClick={() => onSelect(p.name)}
          >
            <span>
              {p.display_name}
              {p.warning ? <div className="warn">{p.warning}</div> : null}
            </span>
            <span className="badge">{p.stability}</span>
          </button>
        );
      })}
    </section>
  );
}
