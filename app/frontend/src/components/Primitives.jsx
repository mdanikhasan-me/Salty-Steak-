import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Circle,
  Info,
  LoaderCircle,
  XCircle,
} from "lucide-react";
import { normaliseToken } from "../workflows/operations.mjs";

export function PageHeader({ title, actions, children }) {
  return (
    <header className="page-header">
      <div className="page-header__copy">
        <h1>{title}</h1>
        {children}
      </div>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </header>
  );
}

export function Status({ value, label, detail, size = "normal" }) {
  const token = normaliseToken(value || label || "unknown");
  const kind = statusKind(token);
  const Icon = {
    success: CheckCircle2,
    warning: AlertTriangle,
    error: XCircle,
    progress: LoaderCircle,
    neutral: Circle,
  }[kind];
  return (
    <span
      className={`status status--${kind} ${size === "small" ? "status--small" : ""}`}
      title={detail || undefined}
    >
      <Icon className={kind === "progress" ? "spin" : ""} aria-hidden="true" />
      <span>{label || readableStatus(token)}</span>
    </span>
  );
}

function statusKind(token) {
  if (
    [
      "ready",
      "completed",
      "valid",
      "verified",
      "passed",
      "active",
      "prepared",
      "training_ready",
    ].includes(token)
  ) {
    return "success";
  }
  if (
    [
      "failed",
      "invalid",
      "error",
      "unavailable",
      "blocked",
      "source_changed",
    ].includes(token)
  ) {
    return "error";
  }
  if (
    ["warning", "warnings", "needs_validation", "not_prepared", "interrupted"].includes(token)
  ) {
    return "warning";
  }
  if (
    [
      "queued",
      "running",
      "preparing",
      "preparing_salty_potato",
      "stop_requested",
      "checking_status",
    ].includes(token)
  ) {
    return "progress";
  }
  return "neutral";
}

function readableStatus(token) {
  return token
    .split("_")
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}

export function InlineNotice({ kind = "information", title, children, actions }) {
  const Icon =
    kind === "error" ? XCircle : kind === "warning" ? AlertTriangle : kind === "success" ? CheckCircle2 : Info;
  return (
    <div className={`inline-notice inline-notice--${kind}`} role={kind === "error" ? "alert" : "status"}>
      <Icon aria-hidden="true" />
      <div>
        {title ? <strong>{title}</strong> : null}
        <div>{children}</div>
        {actions ? <div className="inline-notice__actions">{actions}</div> : null}
      </div>
    </div>
  );
}

export function EmptyState({ icon: Icon, title, description, action }) {
  return (
    <div className={`empty-state ${Icon ? "empty-state--with-icon" : "empty-state--without-icon"}`}>
      {Icon ? <Icon aria-hidden="true" /> : null}
      <div className="empty-state__copy">
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  );
}

export function Button({
  children,
  variant = "secondary",
  icon: Icon,
  busy = false,
  type = "button",
  className = "",
  ...props
}) {
  return (
    <button
      type={type}
      className={`button button--${variant} ${className}`}
      disabled={busy || props.disabled}
      {...props}
    >
      {busy ? <LoaderCircle className="spin" aria-hidden="true" /> : Icon ? <Icon aria-hidden="true" /> : null}
      <span>{children}</span>
    </button>
  );
}

export function Disclosure({ summary, children, open = false, className = "" }) {
  return (
    <details className={`disclosure ${className}`} open={open}>
      <summary>
        <span>{summary}</span>
        <ChevronDown aria-hidden="true" />
      </summary>
      <div className="disclosure__content">{children}</div>
    </details>
  );
}

export function DefinitionList({ items, compact = false }) {
  return (
    <dl className={`definition-list ${compact ? "definition-list--compact" : ""}`}>
      {items
        .filter((item) => item && item.value !== undefined && item.value !== null)
        .map((item) => (
          <div key={item.label}>
            <dt>{item.label}</dt>
            <dd
              className={[item.mono ? "mono" : "", item.truncate ? "truncate" : ""]
                .filter(Boolean)
                .join(" ")}
              title={item.truncate ? String(item.value) : undefined}
            >
              {item.value}
            </dd>
          </div>
        ))}
    </dl>
  );
}

export function SegmentedControl({ label, value, options, onChange }) {
  return (
    <fieldset className="segmented-field">
      <legend>{label}</legend>
      <div className="segmented-control">
        {options.map((option) => (
          <label key={option.value}>
            <input
              type="radio"
              name={label}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
