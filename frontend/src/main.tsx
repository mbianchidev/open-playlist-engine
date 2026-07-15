import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import OwnerGate from "./components/OwnerGate";
import PublicSharePage from "./components/PublicSharePage";
import "./index.css";
import "./theme.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("missing #root element");
}

const shareToken = publicShareToken(window.location.pathname);

createRoot(root).render(
  <StrictMode>{shareToken ? <PublicSharePage token={shareToken} /> : <OwnerGate />}</StrictMode>,
);

function publicShareToken(pathname: string): string | null {
  const match = pathname.match(/^\/(?:share|shared)\/([A-Za-z0-9_-]{32,})\/?$/);
  return match?.[1] ?? null;
}
