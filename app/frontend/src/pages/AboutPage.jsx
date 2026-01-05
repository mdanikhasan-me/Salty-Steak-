import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  Check,
  Copy,
  FolderOpen,
  ShieldCheck,
} from "lucide-react";
import { api } from "../api/client.js";
import { InlineNotice } from "../components/Primitives.jsx";
import { errorMessage } from "../workflows/formatters.js";

export function AboutPage({ onBack }) {
  const [about, setAbout] = useState(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.getAbout()
      .then((payload) => {
        if (!cancelled) setAbout(payload);
      })
      .catch((requestError) => {
        if (!cancelled) setError(errorMessage(requestError));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const diagnostics = useMemo(() => JSON.stringify(about, null, 2), [about]);
  const application = about?.application || {};
  const runtime = about?.runtime || {};
  const system = about?.system || {};
  const model = about?.model;
  const paths = about?.paths || {};

  async function copyDiagnostics() {
    try {
      await navigator.clipboard.writeText(diagnostics);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setError("Windows did not allow the diagnostic summary to be copied.");
    }
  }

  function openWorkspace() {
    api.openPath(paths.workspace).catch((requestError) => {
      setError(errorMessage(requestError));
    });
  }

  return (
    <article className="about-page about-page--compact" aria-labelledby="about-title">
      <button className="about-compact__back" type="button" onClick={onBack}>
        <ArrowLeft aria-hidden="true" />
        Back to chat
      </button>

      <header className="about-compact__hero">
        <img src="/assets/salty-potato-symbol.svg" alt="" width="56" height="56" />
        <div>
          <h1 id="about-title">Salty Steak</h1>
          <p>Private AI that runs on your computer.</p>
          <span>Version {application.app_version || "2.0.0"}</span>
        </div>
      </header>

      {error ? <InlineNotice kind="error" title="About details unavailable">{error}</InlineNotice> : null}

      <section className="about-compact__privacy">
        <ShieldCheck aria-hidden="true" />
        <div>
          <h2>Local by default</h2>
          <p>Conversations, files, and model data remain in your workspace unless you explicitly export or connect a network service.</p>
        </div>
      </section>

      <dl className="about-compact__facts">
        <div><dt>Local service</dt><dd>{about ? "Connected" : "Connecting"}</dd></div>
        <div><dt>Active model</dt><dd>{model?.display_name || "No model selected"}</dd></div>
        <div><dt>Workspace</dt><dd>{system.current_drive || "Local drive"}</dd></div>
        <div><dt>Runtime</dt><dd>{runtime.device ? `${runtime.device}${runtime.precision ? ` · ${runtime.precision}` : ""}` : "Idle"}</dd></div>
      </dl>

      <div className="about-compact__actions">
        <button className="button" type="button" disabled={!paths.workspace} onClick={openWorkspace}>
          <FolderOpen aria-hidden="true" />
          Open workspace
        </button>
        <button className="button" type="button" disabled={!about} onClick={copyDiagnostics}>
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied ? "Copied" : "Copy diagnostics"}
        </button>
      </div>
    </article>
  );
}
