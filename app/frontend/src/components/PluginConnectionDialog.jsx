import { useMemo, useState } from "react";
import { CheckCircle2, Server, ShieldCheck, X } from "lucide-react";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap.js";
import "../styles/plugin-connection-dialog.css";

const LABELS = {
  gmail: "Gmail",
  google_calendar: "Google Calendar",
  icloud_calendar: "iCloud Calendar",
  mcp: "MCP server",
};

export function PluginConnectionDialog({
  connectorId,
  connector = {},
  busy = false,
  error = "",
  onConnect,
  onRecheck,
  onDisconnect,
  onClose,
}) {
  const configuration = connector.configuration || {};
  const [endpoint, setEndpoint] = useState(configuration.streamable_http_endpoint || "");
  const [bearerToken, setBearerToken] = useState("");
  const [allowedTools, setAllowedTools] = useState(
    Array.isArray(configuration.allowed_tools) ? configuration.allowed_tools.join(", ") : "",
  );
  const [validationError, setValidationError] = useState("");
  const configured = ["connected", "configured", "degraded", "error"].includes(connector.status);
  const verified = connector.status === "connected";
  const dialogRef = useModalFocusTrap({ onClose, canClose: !busy });
  const label = LABELS[connectorId] || "Plugin";
  const discoveredTools = useMemo(
    () => Array.isArray(configuration.discovered_tool_names) ? configuration.discovered_tool_names : [],
    [configuration.discovered_tool_names],
  );

  function submit(event) {
    event.preventDefault();
    if (busy) return;
    const toolNames = allowedTools.split(",").map((item) => item.trim()).filter(Boolean);
    if (toolNames.some((name) => /\s/.test(name))) {
      setValidationError("Tool names cannot contain spaces. Separate each tool with a comma.");
      return;
    }
    setValidationError("");
    const request = {
      endpoint: endpoint.trim(),
      allowed_tools: toolNames,
    };
    if (bearerToken.trim()) request.bearer_token = bearerToken.trim();
    onConnect?.(request);
  }

  return (
    <div className="plugin-connect-layer" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose?.();
    }}>
      <section ref={dialogRef} className="plugin-connect-dialog" role="dialog" aria-modal="true" aria-labelledby="plugin-connect-title" tabIndex="-1">
        <header>
          <div>
            <span>{configured ? "Manage connection" : "Connect plugin"}</span>
            <h2 id="plugin-connect-title">{label}</h2>
          </div>
          <button type="button" aria-label="Close connection setup" disabled={busy} onClick={onClose}>
            <X aria-hidden="true" />
          </button>
        </header>

        <div className="plugin-connect-dialog__body">
          <div className="plugin-connect-dialog__intro">
            <Server aria-hidden="true" />
            <div>
              <strong>Streamable HTTP</strong>
              <p>Connect a compatible MCP service. Salty Steak stores the endpoint locally and protects the bearer token for this Windows account.</p>
            </div>
          </div>

          <form onSubmit={submit}>
            <label>
              <span>Server endpoint</span>
              <input
                type="url"
                disabled={busy}
                required
                spellCheck="false"
                autoComplete="off"
                value={endpoint}
                placeholder="https://example.com/mcp"
                onChange={(event) => setEndpoint(event.currentTarget.value)}
              />
              <small>HTTPS is required. Loopback HTTP is allowed for a server running on this computer.</small>
            </label>
            <label>
              <span>Bearer token <em>optional</em></span>
              <input
                type="password"
                disabled={busy}
                autoComplete="off"
                value={bearerToken}
                placeholder={connector.credentials_present ? "Stored - leave blank to keep it for this endpoint" : "Paste token"}
                onChange={(event) => setBearerToken(event.currentTarget.value)}
              />
            </label>
            <label>
              <span>Allowed tools <em>optional</em></span>
              <input
                type="text"
                disabled={busy}
                spellCheck="false"
                value={allowedTools}
                placeholder="search_mail, create_event"
                onChange={(event) => setAllowedTools(event.currentTarget.value)}
              />
              <small>Comma-separated allowlist. Leave empty to discover tools, then approve them before use.</small>
            </label>

            {discoveredTools.length ? (
              <div className="plugin-connect-dialog__tools">
                <span>Discovered tools</span>
                <div>{discoveredTools.map((tool) => <code key={tool}>{tool}</code>)}</div>
              </div>
            ) : null}
            {validationError || error ? <p className="plugin-connect-dialog__error" role="alert">{validationError || error}</p> : null}

            <div className="plugin-connect-dialog__assurance">
              <ShieldCheck aria-hidden="true" />
              <span>Connecting discovers capabilities only. Actions remain blocked until their tool names are explicitly allowed.</span>
            </div>

            <footer>
              {configured && onDisconnect ? (
                <button type="button" className="plugin-connect-dialog__disconnect" disabled={busy} onClick={onDisconnect}>Disconnect</button>
              ) : <span />}
              <div>
                {configured && onRecheck ? (
                  <button type="button" disabled={busy} onClick={onRecheck}>Recheck</button>
                ) : null}
                <button type="submit" className="plugin-connect-dialog__primary" disabled={busy || !endpoint.trim()}>
                  {busy ? "Checking..." : configured ? "Save changes" : "Connect"}
                </button>
              </div>
            </footer>
          </form>

          {verified ? (
            <p className="plugin-connect-dialog__connected"><CheckCircle2 aria-hidden="true" /> Connected and verified</p>
          ) : configured ? (
            <p className="plugin-connect-dialog__configured">Configured; run a successful check before tools can be used.</p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
