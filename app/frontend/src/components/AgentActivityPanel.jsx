import { useState } from "react";
import {
  AppWindow,
  Check,
  ChevronRight,
  CircleAlert,
  Globe,
  Image as ImageIcon,
  Mail,
  Monitor,
  MousePointerClick,
  Search,
  ShieldAlert,
  Square,
  Terminal,
} from "lucide-react";

const ACTION_ICONS = {
  "screen.capture": Monitor,
  "input.control": MousePointerClick,
  "application.launch": AppWindow,
  "terminal.execute": Terminal,
  "browser.control": Globe,
  "ui.automation": MousePointerClick,
  "window.control": AppWindow,
  "image.generate": ImageIcon,
  research: Search,
  connector: Mail,
  respond: Check,
};


const ACTION_LABELS = {
  "screen.capture": "Looked at the screen",
  "input.control": "Used mouse or keyboard",
  "application.launch": "Opened an application",
  "terminal.execute": "Ran a command",
  "browser.control": "Read a web page",
  "ui.automation": "Used a window control",
  "window.control": "Switched window",
  "image.generate": "Created an image",
  respond: "Finished",
};

const STATE_COPY = {
  queued: "Getting ready",
  planning: "Working out how",
  executing: "Working on your request",
  observing: "Checking the result",
  verifying: "Confirming it worked",
  waiting: "Waiting for you",
  running: "Working on your request",
  stopping: "Stopping",
  stopped: "Stopped",
  completed: "Done",
  cancelled: "Stopped",
  failed: "Could not finish",
  exhausted: "Reached the step limit",
  needs_review: "Waiting for your review",
};


const WAITING_COPY = {
  user_sign_in: "Sign in to continue",
  user_approval: "Waiting for your approval",
  user_answer: "Waiting for your answer",
  user_2fa: "Enter your verification code",
  ambiguous_destructive_choice: "Choose before anything is deleted",
};

function elapsedLabel(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value < 60) return `${value.toFixed(1)}s`;
  const minutes = Math.floor(value / 60);
  return `${minutes}m ${Math.round(value % 60)}s`;
}

function stepSummary(task, visibleCount, running) {


  const planned = Number(task?.planned_step_count);
  if (Number.isFinite(planned) && planned > 0) {
    return `Step ${Math.min(visibleCount, planned)} of ${planned}`;
  }
  if (!visibleCount) return running ? "Starting" : "";
  return `Step ${visibleCount}`;
}

export function AgentActivityPanel({
  task,
  running = false,
  onStop,
  onClose,
  onApprove,
  onReject,
}) {
  const [showDetail, setShowDetail] = useState(false);
  if (!task) return null;

  const steps = Array.isArray(task.steps) ? task.steps : [];
  const state = String(task.state || (running ? "running" : "completed"));
  const stopping = state === "stopping";
  const current =
    running && task.action && !stopping
      ? {
        step: Number(task.step) || steps.length + 1,
        action: String(task.action),
        reason: String(task.reason || ""),
        status: "running",
      }
      : null;
  const visible = current ? [...steps, current] : steps;
  const elapsed = elapsedLabel(task.elapsed_seconds);
  const summary = stepSummary(task, visible.length, running);
  const waiting = task.waiting_for ? WAITING_COPY[task.waiting_for] : null;
  const approval = task.approval || null;
  const metrics = task.metrics || {};

  return (
    <aside className="agent-activity" aria-label="Activity">
      <header className="agent-activity__header">
        <div className="agent-activity__heading">
          <strong>{task.state_label || STATE_COPY[state] || "Working"}</strong>
          <span className="agent-activity__meta">
            {[summary, elapsed].filter(Boolean).join(" · ")}
          </span>
        </div>
        {running || stopping ? (
          <button
            type="button"
            className="agent-activity__stop"
            onClick={onStop}


            disabled={stopping}
            aria-live="polite"
          >
            <Square aria-hidden="true" />
            {stopping ? "Stopping…" : "Stop"}
          </button>
        ) : onClose ? (
          <button type="button" className="agent-activity__stop" onClick={onClose}>
            Close
          </button>
        ) : null}
      </header>

      {waiting ? (
        <div className="agent-activity__waiting" role="status">
          <ShieldAlert aria-hidden="true" />
          <div>
            <strong>{waiting}</strong>
            {task.waiting_detail?.url ? <p>{task.waiting_detail.url}</p> : null}
          </div>
        </div>
      ) : null}

      {approval ? (
        <ApprovalCard approval={approval} onApprove={onApprove} onReject={onReject} />
      ) : null}

      <ol className="agent-activity__steps">
        {visible.map((step, index) => {
          const Icon = ACTION_ICONS[step.action] || CircleAlert;
          const failed = step.status === "failed";
          const blocked = step.status === "blocked";
          const active = step.status === "running";
          const tone = failed
            ? "failed"
            : blocked
              ? "blocked"
              : active
                ? "active"
                : "done";
          return (
            <li
              key={`${step.step}-${step.action}-${index}`}
              className={`agent-step agent-step--${tone}`}
              style={{ "--agent-step-index": Math.min(index, 6) }}
            >
              <span className="agent-step__marker" aria-hidden="true">
                {blocked ? <ShieldAlert /> : <Icon />}
              </span>
              <div className="agent-step__body">
                <div className="agent-step__title">
                  <strong>{ACTION_LABELS[step.action] || step.action}</strong>
                  {step.verified === true ? (
                    <small className="agent-step__verified">Confirmed</small>
                  ) : null}
                </div>
                {step.reason ? <p>{step.reason}</p> : null}
                <StepDetail step={step} />
              </div>
            </li>
          );
        })}
      </ol>

      <div className="agent-activity__disclosure">
        <button
          type="button"
          className="agent-activity__toggle"
          onClick={() => setShowDetail((value) => !value)}
          aria-expanded={showDetail}
        >
          <ChevronRight aria-hidden="true" data-open={showDetail || undefined} />
          Technical details
        </button>
        {showDetail ? (
          <dl className="agent-activity__facts">
            <TechnicalFact label="Task" value={task.task_id} mono />
            <TechnicalFact label="State" value={state} />
            <TechnicalFact label="Model calls" value={metrics.model_calls} />
            <TechnicalFact label="Planning calls" value={metrics.planning_model_calls} />
            <TechnicalFact label="Image calls" value={metrics.image_model_calls} />
            <TechnicalFact label="Tool calls" value={metrics.tool_calls} />
            <TechnicalFact label="Retries" value={metrics.retries} />
            <TechnicalFact label="Replans" value={metrics.replans} />
            <TechnicalFact label="Screenshots" value={metrics.screenshots} />
            {task.failure ? <TechnicalFact label="Failure" value={task.failure} /> : null}
          </dl>
        ) : null}
      </div>
    </aside>
  );
}

function TechnicalFact({ label, value, mono = false }) {
  if (value === undefined || value === null || value === "") return null;
  return (
    <div className="agent-activity__fact">
      <dt>{label}</dt>
      <dd className={mono ? "is-mono" : undefined}>{String(value)}</dd>
    </div>
  );
}

function ApprovalCard({ approval, onApprove, onReject }) {


  const count = Number(approval.item_count);
  return (
    <div className="agent-approval" role="group" aria-label="Approval required">
      <div className="agent-approval__body">
        <strong>{approval.summary || "This action needs your approval"}</strong>
        {approval.resource ? <p className="agent-approval__where">{approval.resource}</p> : null}
        {Number.isFinite(count) && count > 0 ? (
          <p className="agent-approval__count">{count} items affected</p>
        ) : null}
        {approval.reason ? <p className="agent-approval__why">{approval.reason}</p> : null}
      </div>
      <div className="agent-approval__actions">
        <button
          type="button"
          className="agent-approval__reject"
          onClick={() => onReject?.(approval)}
        >
          Reject
        </button>
        <button
          type="button"
          className="agent-approval__approve"
          onClick={() => onApprove?.(approval)}
        >
          Approve
        </button>
      </div>
    </div>
  );
}

function StepDetail({ step }) {
  const observation = step.observation || {};
  if (step.status === "failed" && observation.error) {
    return <p className="agent-step__detail agent-step__detail--failed">{observation.error}</p>;
  }
  if (step.action === "screen.capture" && observation.screenshot_path) {
    return (
      <p className="agent-step__detail">
        {`Captured ${observation.image_width || "?"}×${observation.image_height || "?"}`}
        {Number(observation.scale_divisor) > 1
          ? ` from a ${observation.screen_width}×${observation.screen_height} screen`
          : ""}
      </p>
    );
  }
  if (step.action === "terminal.execute") {
    const output = String(observation.stdout || observation.stderr || "").trim();
    if (!output) return null;
    return (
      <pre className="agent-step__output">
        {output.length > 600 ? `${output.slice(0, 600)}…` : output}
      </pre>
    );
  }
  if (step.action === "application.launch" && observation.target) {
    return <p className="agent-step__detail">{observation.target}</p>;
  }
  return null;
}
