import { AppWindow, Layers, Monitor, MousePointerClick, Terminal, Folder, Globe, MessageCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import "../styles/computer-tools.css";
import {
  COMPUTER_CONTROL_CAPABILITIES,
  automationResultSummary,
  grantRequest,
  hasComputerControlAdapter,
  normaliseAutomationAudit,
  normaliseAutomationStatus,
  prepareApplicationLaunchInvocation,
  prepareInputControlInvocation,
  prepareWindowControlInvocation,
  prepareScreenCaptureInvocation,
  prepareTerminalInvocation,
  revokeRequest,
  unavailableAutomationStatus,
} from "../workflows/computerControlPermissions.mjs";

const ICONS = {
  terminal: Terminal,
  files: Folder,
  browser_control: Globe,
  ui_automation: AppWindow,
  discord_inspect: MessageCircle,
  screen_capture: Monitor,
  input_control: MousePointerClick,
  application_launch: AppWindow,
  window_control: Layers,
};
const MANUAL_CONTROLS = new Set(["terminal.execute", "screen.capture", "input.control", "application.launch", "window.control"]);

export function ComputerControlPermissions({
  onOpenBrowser,
  adapter = null,
  onAnalyzeCapture = null,
  proposedInvocation = null,
}) {
  const adapterReady = hasComputerControlAdapter(adapter);
  const [status, setStatus] = useState(() => unavailableAutomationStatus(
    adapterReady ? "Automation status has not loaded yet." : "This app build has no automation adapter.",
  ));
  const [auditRecords, setAuditRecords] = useState([]);
  const [loading, setLoading] = useState(adapterReady);
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [grantReview, setGrantReview] = useState(null);
  const [terminalDraft, setTerminalDraft] = useState(null);
  const [invocationReview, setInvocationReview] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const queuedProposalRef = useRef(null);
  const handledProposalRef = useRef("");
  const sectionRef = useRef(null);

  const refresh = useCallback(async ({ quiet = false } = {}) => {
    if (!hasComputerControlAdapter(adapter)) {
      setStatus(unavailableAutomationStatus("This app build has no automation adapter."));
      setAuditRecords([]);
      setError("Computer access is unavailable because the broker adapter is missing.");
      setLoading(false);
      return;
    }
    if (!quiet) setLoading(true);
    setError("");
    try {
      const [nextStatus, nextAudit] = await Promise.all([
        adapter.getStatus(),
        adapter.getAudit(8),
      ]);
      setStatus(normaliseAutomationStatus(nextStatus));
      setAuditRecords(normaliseAutomationAudit(nextAudit));
    } catch (reason) {
      const message = errorMessage(reason);
      setStatus(unavailableAutomationStatus(message));
      setAuditRecords([]);
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [adapter]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (
      !proposedInvocation
      || proposedInvocation.kind !== "terminal.execute"
      || !proposedInvocation.id
      || handledProposalRef.current === proposedInvocation.id
      || loading
    ) return;


    if (status.schema === "unavailable") return;
    const item = status.capabilities.find((capability) => capability.id === "terminal.execute");
    const args = proposedInvocation.arguments;
    if (!item || !args || !Array.isArray(args.argv)) {
      handledProposalRef.current = proposedInvocation.id;
      setError("The proposed terminal action is incomplete and cannot be reviewed.");
      return;
    }
    handledProposalRef.current = proposedInvocation.id;
    const draft = {
      capability: item,
      argvText: JSON.stringify(args.argv),
      workingDirectory: String(
        args.working_directory || item.constraints.working_directory_root || "",
      ),
      timeoutSeconds: Number(args.timeout_seconds || Math.min(10, status.limits.maxTimeoutSeconds)),
    };
    queuedProposalRef.current = draft;
    setError("");
    setLastResult(null);
    sectionRef.current?.scrollIntoView({ block: "start" });
    if (item.effectiveEnabled) {
      try {
        setInvocationReview(prepareTerminalInvocation(draft, status.limits));
      } catch (reason) {
        setError(errorMessage(reason));
      }
      return;
    }
    if (item.available && !item.granted) {
      setGrantReview({
        capability: item,
        workingDirectoryRoot: String(item.constraints.working_directory_root || ""),
      });
      return;
    }
    setError(item.detail || "Terminal access is not currently available.");
  }, [loading, proposedInvocation, status]);

  function updateDraft(field, value) {
    setTerminalDraft(current => ({ ...current, [field]: value }));
  }

  function beginGrant(item) {
    const refreshingStaleGrant = item.granted && item.actionLabel === "Refresh access";
    if (!item.available || (item.granted && !refreshingStaleGrant)) return;
    setError("");
    setInvocationReview(null);
    setTerminalDraft(null);
    setGrantReview({
      capability: item,
      workingDirectoryRoot: item.id === "terminal.execute"
        ? String(item.constraints.working_directory_root || "")
        : "",
    });
  }

  async function confirmGrant() {
    if (!grantReview) return;
    const item = grantReview.capability;
    setBusyId(item.id);
    setError("");
    try {
      const request = grantRequest(item, grantReview.workingDirectoryRoot);
      const nextStatus = normaliseAutomationStatus(await adapter.grant(request));
      setStatus(nextStatus);
      setGrantReview(null);
      const queued = queuedProposalRef.current;
      const grantedCapability = nextStatus.capabilities.find(
        (capability) => capability.id === "terminal.execute",
      );
      if (queued && grantedCapability?.effectiveEnabled) {
        queuedProposalRef.current = null;
        setInvocationReview(prepareTerminalInvocation(
          { ...queued, capability: grantedCapability },
          nextStatus.limits,
        ));
      }
      await refreshAudit();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusyId("");
    }
  }

  async function revoke(item) {
    if (!item.granted) return;
    setBusyId(item.id);
    setError("");
    try {
      const nextStatus = await adapter.revoke(revokeRequest(item));
      setStatus(normaliseAutomationStatus(nextStatus));
      setGrantReview(null);
      setTerminalDraft(null);
      setInvocationReview(null);
      await refreshAudit();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusyId("");
    }
  }

  function beginInvocation(item) {
    if (!item.effectiveEnabled) return;
    setError("");
    setGrantReview(null);
    setInvocationReview(null);
    if (item.id === "terminal.execute") {
      setTerminalDraft({
        kind: "terminal",
        capability: item,
        argvText: "",
        workingDirectory: String(item.constraints.working_directory_root || ""),
        timeoutSeconds: Math.min(10, status.limits.maxTimeoutSeconds),
      });
      return;
    }
    if (item.id === "input.control") {
      setTerminalDraft({
        kind: "input",
        capability: item,
        action: "mouse_click",
        x: 0,
        y: 0,
        button: "left",
        text: "",
        key: "Enter",
        combo: "Ctrl+C",
        clicks: -3,
      });
      return;
    }
    if (item.id === "application.launch") {
      setTerminalDraft({ kind: "launch", capability: item, target: "" });
      return;
    }
    if (item.id === "window.control") {
      setTerminalDraft({ kind: "window", capability: item, action: "list", title: "" });
      return;
    }
    try {
      setInvocationReview(prepareScreenCaptureInvocation(item));
      setTerminalDraft(null);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  function reviewTerminalInvocation() {
    if (!terminalDraft) return;
    setError("");
    try {
      const prepared = terminalDraft.kind === "input"
        ? prepareInputControlInvocation(terminalDraft)
        : terminalDraft.kind === "launch"
          ? prepareApplicationLaunchInvocation(terminalDraft)
          : terminalDraft.kind === "window"
            ? prepareWindowControlInvocation(terminalDraft)
            : prepareTerminalInvocation(terminalDraft, status.limits);
      setInvocationReview(prepared);
      setTerminalDraft(null);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  async function confirmInvocation() {
    if (!invocationReview) return;
    setBusyId(invocationReview.capability);
    setError("");
    try {
      const result = await adapter.invoke({
        capability: invocationReview.capability,
        arguments: invocationReview.arguments,
        user_confirmed: true,
      });
      setLastResult(result);
      setInvocationReview(null);
      await refresh({ quiet: true });
    } catch (reason) {
      setError(errorMessage(reason));
      await refreshAudit().catch(() => {});
    } finally {
      setBusyId("");
    }
  }

  async function refreshAudit() {
    const nextAudit = await adapter.getAudit(8);
    setAuditRecords(normaliseAutomationAudit(nextAudit));
  }

  const activeReviewCapability = grantReview?.capability.id || terminalDraft?.capability.id || invocationReview?.capability;
  const activeReview = <>
      {grantReview ? (
        <PermissionReview
          title={`Grant ${grantReview.capability.name} access?`}
          summary={grantScopeSummary(grantReview)}
          note="This grant stays on this computer until you revoke it. Actions follow the Ask or Full access mode selected beside the composer."
          confirmLabel="Grant access"
          busy={busyId === grantReview.capability.id}
          onCancel={() => setGrantReview(null)}
          onConfirm={confirmGrant}
        >
          {grantReview.capability.id === "terminal.execute" ? (
            <label className="automation-permission-field">
              <span>Working-directory root</span>
              <input
                type="text"
                value={grantReview.workingDirectoryRoot}
                onChange={(event) => {
                  const value = event.currentTarget.value;
                  setGrantReview(current => ({ ...current, workingDirectoryRoot: value }));
                }}
              />
              <small>Must be an existing folder inside the project. This does not sandbox the executable.</small>
            </label>
          ) : null}
        </PermissionReview>
      ) : null}

      {terminalDraft ? (
        <section className="automation-permission-review automation-invocation-form" aria-label={draftTitle(terminalDraft)}>
          <div>
            <strong>{draftTitle(terminalDraft)}</strong>
            {terminalDraft.kind === "input" ? (
              <>
                <p>The action is sent to whichever window currently has focus.</p>
                <label className="automation-permission-field automation-permission-field--compact">
                  <span>Action</span>
                  <select
                    value={terminalDraft.action}
                    onChange={(event) => updateDraft("action", event.currentTarget.value)}
                  >
                    <option value="mouse_move">Move pointer</option>
                    <option value="mouse_click">Click</option>
                    <option value="mouse_scroll">Scroll</option>
                    <option value="key_press">Press a key</option>
                    <option value="type_text">Type text</option>
                    <option value="key_combo">Key combination</option>
                  </select>
                </label>
                {["mouse_move", "mouse_click", "mouse_scroll"].includes(terminalDraft.action) ? (
                  <>
                    <label className="automation-permission-field automation-permission-field--compact">
                      <span>Screen x</span>
                      <input
                        type="number"
                        value={terminalDraft.x}
                        onChange={(event) => updateDraft("x", event.currentTarget.value)}
                      />
                    </label>
                    <label className="automation-permission-field automation-permission-field--compact">
                      <span>Screen y</span>
                      <input
                        type="number"
                        value={terminalDraft.y}
                        onChange={(event) => updateDraft("y", event.currentTarget.value)}
                      />
                    </label>
                  </>
                ) : null}
                {terminalDraft.action === "mouse_click" ? (
                  <label className="automation-permission-field automation-permission-field--compact">
                    <span>Button</span>
                    <select
                      value={terminalDraft.button}
                      onChange={(event) => updateDraft("button", event.currentTarget.value)}
                    >
                      <option value="left">Left</option>
                      <option value="right">Right</option>
                      <option value="middle">Middle</option>
                    </select>
                  </label>
                ) : null}
                {terminalDraft.action === "mouse_scroll" ? (
                  <label className="automation-permission-field automation-permission-field--compact">
                    <span>Scroll clicks (negative scrolls down)</span>
                    <input
                      type="number"
                      value={terminalDraft.clicks}
                      onChange={(event) => updateDraft("clicks", event.currentTarget.value)}
                    />
                  </label>
                ) : null}
                {terminalDraft.action === "key_press" ? (
                  <label className="automation-permission-field">
                    <span>Key name</span>
                    <input
                      type="text"
                      value={terminalDraft.key}
                      placeholder="Enter, Tab, Escape, F5, Left"
                      onChange={(event) => updateDraft("key", event.currentTarget.value)}
                    />
                  </label>
                ) : null}
                {terminalDraft.action === "type_text" ? (
                  <label className="automation-permission-field">
                    <span>Text to type</span>
                    <textarea
                      rows="3"
                      value={terminalDraft.text}
                      onChange={(event) => updateDraft("text", event.currentTarget.value)}
                    />
                  </label>
                ) : null}
                {terminalDraft.action === "key_combo" ? (
                  <label className="automation-permission-field">
                    <span>Combination</span>
                    <input
                      type="text"
                      value={terminalDraft.combo}
                      placeholder="Ctrl+C, Alt+F4, Win+D"
                      onChange={(event) => updateDraft("combo", event.currentTarget.value)}
                    />
                  </label>
                ) : null}
              </>
            ) : terminalDraft.kind === "window" ? (
              <>
                <p>Windows reports its own open window titles, so nothing is read from the screen.</p>
                <label className="automation-permission-field automation-permission-field--compact">
                  <span>Action</span>
                  <select
                    value={terminalDraft.action}
                    onChange={(event) => updateDraft("action", event.currentTarget.value)}
                  >
                    <option value="list">List open windows</option>
                    <option value="focus">Bring a window to the front</option>
                    <option value="close">Close a window</option>
                  </select>
                </label>
                {terminalDraft.action === "list" ? null : (
                  <label className="automation-permission-field">
                    <span>Window title</span>
                    <input
                      type="text"
                      value={terminalDraft.title}
                      placeholder="part of the window title"
                      onChange={(event) => updateDraft("title", event.currentTarget.value)}
                    />
                  </label>
                )}
              </>
            ) : terminalDraft.kind === "launch" ? (
              <>
                <p>Enter an installed application name, an https link, or an absolute file path.</p>
                <label className="automation-permission-field">
                  <span>Application, link, or file</span>
                  <input
                    type="text"
                    value={terminalDraft.target}
                    placeholder="msedge, https://example.com, C:\\Users\\me\\notes.txt"
                    onChange={(event) => updateDraft("target", event.currentTarget.value)}
                  />
                </label>
              </>
            ) : (
              <>
                <p>Enter argv as a JSON array. No shell command string is accepted.</p>
                <label className="automation-permission-field">
                  <span>Command argv</span>
                  <textarea
                    rows="3"
                    value={terminalDraft.argvText}
                    placeholder={'["git", "status", "--short"]'}
                    onChange={(event) => updateDraft("argvText", event.currentTarget.value)}
                  />
                </label>
                <label className="automation-permission-field">
                  <span>Working directory</span>
                  <input
                    type="text"
                    value={terminalDraft.workingDirectory}
                    onChange={(event) => updateDraft("workingDirectory", event.currentTarget.value)}
                  />
                </label>
                <label className="automation-permission-field automation-permission-field--compact">
                  <span>Timeout in seconds</span>
                  <input
                    type="number"
                    min="0.05"
                    max={status.limits.maxTimeoutSeconds}
                    step="0.05"
                    value={terminalDraft.timeoutSeconds}
                    onChange={(event) => updateDraft("timeoutSeconds", event.currentTarget.value)}
                  />
                </label>
              </>
            )}
          </div>
          <div className="automation-permission-review__actions">
            <button type="button" className="plugin-action plugin-action--quiet" onClick={() => setTerminalDraft(null)}>Cancel</button>
            <button type="button" className="plugin-action" onClick={reviewTerminalInvocation}>Review action</button>
          </div>
        </section>
      ) : null}

      {invocationReview ? (
        <PermissionReview
          title={`Confirm ${capabilityName(invocationReview.capability)} action`}
          summary={invocationReview.summary}
          note="Only the exact arguments shown above will be sent to the local broker."
          confirmLabel={invocationReview.capability === "window.control"
            ? ({ list: "List windows", focus: "Focus window", close: "Close window" }[invocationReview.arguments?.action] || "Confirm action")
            : capabilityInvokeLabel(invocationReview.capability)}
          busy={busyId === invocationReview.capability}
          onCancel={() => setInvocationReview(null)}
          onConfirm={confirmInvocation}
        />
      ) : null}

  </>;

  return (
    <section ref={sectionRef} className="computer-control-permissions" aria-labelledby="computer-control-title">
      <header className="plugin-section-label">
        <strong id="computer-control-title">Computer access</strong>
        <span>Choose what Sawlper can use. Local actions follow your task; Ask requests approval for destructive changes.</span>
      </header>
      {loading ? <p className="automation-permission-notice" role="status">Checking the local broker...</p> : null}
      {[
        { id: "browser", title: "Browser" },
        { id: "computer", title: "Computer" },
        { id: "terminal_files", title: "Terminal and files" },
      ].map((group) => {
        const capabilities = status.capabilities.filter((item) => item.group === group.id);
        if (!capabilities.length) return null;
        return <section className="computer-tool-group" key={group.id} aria-label={group.title}>
          <h3>{group.title}</h3>
          {group.id === "browser" && onOpenBrowser ? <button type="button" className="plugin-action" onClick={() => onOpenBrowser()}>Open browser</button> : null}
          <div className="plugins-list" role="list">
        {capabilities.map((item) => {
          const Icon = ICONS[item.uiId] || AppWindow;
          const busy = busyId === item.id;
          const refreshingStaleGrant = item.granted && item.actionLabel === "Refresh access";
          const actionDisabled = busy || !item.available || (item.granted && !item.effectiveEnabled && !refreshingStaleGrant);
          return (
            <div className="plugin-row" role="listitem" key={item.id} aria-busy={busy || undefined}>
              <span className={`plugin-row__icon plugin-row__icon--${item.uiId}`} aria-hidden="true">
                <Icon />
              </span>
              <div className="plugin-row__copy">
                <div className="plugin-row__title-line">
                  <strong>{item.name}</strong>
                  <span className={`plugin-state plugin-state--${item.stateTone}`}>
                    <i aria-hidden="true" />
                    {item.stateLabel}
                  </span>
                </div>
                <p>{item.description}</p>
                <small>{item.detail}</small>
              </div>
              <div className="plugin-row__action automation-actions">
                {item.granted ? (
                  <button
                    type="button"
                    className="plugin-action plugin-action--quiet"
                    disabled={busy}
                    onClick={() => revoke(item)}
                  >
                    Revoke
                  </button>
                ) : null}
                {item.granted && item.effectiveEnabled && !MANUAL_CONTROLS.has(item.id) ? <span className="automation-agent-availability">Available in Sawlper</span> : <button
                  type="button"
                  className="plugin-action"
                  disabled={actionDisabled}
                  title={actionDisabled ? item.detail : undefined}
                  onClick={() => (item.granted && item.effectiveEnabled ? beginInvocation(item) : beginGrant(item))}
                >
                  {busy ? (
                    <span className="activity-phrase activity-phrase--compact" role="status" aria-live="polite" aria-atomic="true">
                      Working…
                    </span>
                  ) : item.actionLabel}
                </button>}
              </div>
              {activeReviewCapability === item.id ? <div className="automation-inline-review">{activeReview}</div> : null}
            </div>
          );
        })}
          </div>
        </section>;
      })}
      {status.capabilities.some((item) => item.group === "app_integrations") ? (
        <details className="computer-tool-integrations">
          <summary>App integrations</summary>
          <p>Optional tools for specific apps.</p>
          <div className="plugins-list" role="list">
            {status.capabilities.filter((item) => item.group === "app_integrations").map((item) => {
              const Icon = ICONS[item.uiId] || AppWindow;
              const busy = busyId === item.id;
              const refreshingStaleGrant = item.granted && item.actionLabel === "Refresh access";
              const actionDisabled = busy || !item.available || (item.granted && !item.effectiveEnabled && !refreshingStaleGrant);
              return (
                <div className="plugin-row" role="listitem" key={item.id} aria-busy={busy || undefined}>
                  <span className={`plugin-row__icon plugin-row__icon--${item.uiId}`} aria-hidden="true"><Icon /></span>
                  <div className="plugin-row__copy">
                    <div className="plugin-row__title-line">
                      <strong>{item.name}</strong>
                      <span className={`plugin-state plugin-state--${item.stateTone}`}><i aria-hidden="true" />{item.stateLabel}</span>
                    </div>
                    <p>{item.description}</p>
                    <small>{item.detail}</small>
                  </div>
                  <div className="plugin-row__action automation-actions">
                    {item.granted ? <button type="button" className="plugin-action plugin-action--quiet" disabled={busy} onClick={() => revoke(item)}>Revoke</button> : null}
                    {item.granted && item.effectiveEnabled && !MANUAL_CONTROLS.has(item.id) ? <span className="automation-agent-availability">Available in Sawlper</span> : <button type="button" className="plugin-action" disabled={actionDisabled} title={actionDisabled ? item.detail : undefined} onClick={() => (item.granted && item.effectiveEnabled ? beginInvocation(item) : beginGrant(item))}>
                      {busy ? <span className="activity-phrase activity-phrase--compact" role="status" aria-live="polite" aria-atomic="true">Working…</span> : item.actionLabel}
                    </button>}
                  </div>
                  {activeReviewCapability === item.id ? <div className="automation-inline-review">{activeReview}</div> : null}
                </div>
              );
            })}
          </div>
        </details>
      ) : null}

      {lastResult ? (
        <div>
          <p className={`automation-permission-result automation-permission-result--${String(lastResult.status || "unknown")}`} role="status">
            {automationResultSummary(lastResult)}
          </p>
          {lastResult.capability === "screen.capture"
            && lastResult.status === "succeeded"
            && lastResult.audit_record_id
            && typeof onAnalyzeCapture === "function" ? (
            <button
              type="button"
              className="plugin-action plugin-action--quiet"
              onClick={() => onAnalyzeCapture(lastResult)}
            >
              Analyze this capture
            </button>
          ) : null}
        </div>
      ) : null}
      {error ? <p className="automation-permission-error" role="alert">{error}</p> : null}

      <details className="automation-audit">
        <summary>Recent computer access activity ({status.auditRecordCount})</summary>
        {auditRecords.length ? (
          <ol>
            {auditRecords.map((record) => (
              <li key={record.id}>
                <span>{auditCapabilityName(record.capability)} {record.event}</span>
                <strong>{record.outcome}</strong>
                <time dateTime={record.createdAt}>{record.createdAt || "Time not reported"}</time>
              </li>
            ))}
          </ol>
        ) : <p>No automation activity has been recorded.</p>}
      </details>
    </section>
  );
}

function PermissionReview({ title, summary, note, confirmLabel, busy, onCancel, onConfirm, children }) {
  const reviewRef = useRef(null);
  useEffect(() => {
    reviewRef.current?.scrollIntoView({ block: "nearest" });
    reviewRef.current?.querySelector("button")?.focus({ preventScroll: true });
  }, [title]);
  return (
    <section ref={reviewRef} className="automation-permission-review" aria-label={title} aria-busy={busy || undefined}>
      <div>
        <strong>{title}</strong>
        <p>{summary}</p>
        {children}
        <small>{note}</small>
      </div>
      <div className="automation-permission-review__actions">
        <button type="button" className="plugin-action plugin-action--quiet" disabled={busy} onClick={onCancel}>Cancel</button>
        <button type="button" className="plugin-action" disabled={busy} onClick={onConfirm}>
          {busy ? (
            <span className="activity-phrase activity-phrase--compact" role="status" aria-live="polite" aria-atomic="true">
              Working…
            </span>
          ) : confirmLabel}
        </button>
      </div>
    </section>
  );
}

function draftTitle(draft) {
  if (draft.kind === "input") return "Prepare mouse or keyboard action";
  if (draft.kind === "launch") return "Prepare application or link";
  if (draft.kind === "window") return "Prepare window action";
  return "Prepare terminal command";
}

function grantScopeSummary(review) {
  if (review.capability.id === "terminal.execute") {
    return `Allow terminal commands whose working directory stays under ${review.workingDirectoryRoot || "the selected project folder"}.`;
  }
  if (review.capability.id === "input.control") {
    return (
      "Allow Salty Steak to move the pointer, click, and send keystrokes to any "
      + "window on this computer. This is the same reach you have at the keyboard, "
      + "so it is not limited to this application."
    );
  }
  if (review.capability.id === "window.control") {
    return (
      "Allow Salty Steak to see the titles of your open windows and to bring one "
      + "to the front or close it. Closing asks the application to close, so it "
      + "still prompts you to save unsaved work."
    );
  }
  if (review.capability.id === "application.launch") {
    return (
      "Allow Salty Steak to start installed applications and open https links "
      + "with their default Windows handler. Launched apps keep running after the "
      + "action finishes."
    );
  }
  return `Allow primary-screen captures saved under ${String(review.capability.constraints.output_root || "the app-owned screenshot folder")}.`;
}

function capabilityName(id) {
  return COMPUTER_CONTROL_CAPABILITIES.find((item) => item.id === id)?.name || "computer";
}

function capabilityInvokeLabel(id) {
  return COMPUTER_CONTROL_CAPABILITIES.find((item) => item.id === id)?.invokeLabel || "Confirm action";
}

function auditCapabilityName(id) {
  return COMPUTER_CONTROL_CAPABILITIES.find((item) => item.id === id)?.name || id;
}

function errorMessage(error) {
  return String(error?.message || error || "Computer access could not be updated.");
}
