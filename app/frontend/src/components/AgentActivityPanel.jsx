import {
  AppWindow,
  Check,
  CircleAlert,
  Monitor,
  MousePointerClick,
  ShieldAlert,
  Square,
  Terminal,
} from "lucide-react";

const ACTION_ICONS = {
  "screen.capture": Monitor,
  "input.control": MousePointerClick,
  "application.launch": AppWindow,
  "terminal.execute": Terminal,
  respond: Check,
};

const ACTION_LABELS = {
  "screen.capture": "Looked at the screen",
  "input.control": "Used mouse or keyboard",
  "application.launch": "Opened an application",
  "terminal.execute": "Ran a command",
  respond: "Finished",
};



const STATE_COPY = {
  running: "Working",
  completed: "Completed",
  cancelled: "Stopped",
  failed: "Failed",
  exhausted: "Reached the step limit",
  needs_review: "Waiting for your review",
};

function stepSummary(task, visibleCount, running) {


  const planned = Number(task?.planned_step_count);
  if (Number.isFinite(planned) && planned > 0) {
    return `Plan step ${Math.min(visibleCount, planned)} of ${planned}`;
  }
  if (!visibleCount) return running ? "Starting" : "No steps";
  return `Step ${visibleCount}`;
}

export function AgentActivityPanel({ task, running = false, onStop, onClose }) {
  if (!task) return null;
  const steps = Array.isArray(task.steps) ? task.steps : [];
  const state = String(task.state || (running ? "running" : "completed"));
  const current = running && task.action
    ? {
      step: Number(task.step) || steps.length + 1,
      action: String(task.action),
      reason: String(task.reason || ""),
      status: "running",
    }
    : null;
  const visible = current ? [...steps, current] : steps;
  const seconds = Number(task.elapsed_seconds);
  const elapsed = Number.isFinite(seconds) && seconds > 0
    ? (seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds)}s`)
    : "";

  return (
    <aside className="agent-activity" aria-label="Automation activity">
      <header className="agent-activity__header">
        <div>
          <strong>{task.state_label || STATE_COPY[state] || "Automation"}</strong>
          <span>
            {stepSummary(task, visible.length, running)}
            {elapsed ? ` · ${elapsed}` : ""}
          </span>
        </div>
        {running ? (
          <button type="button" className="agent-activity__stop" onClick={onStop}>
            <Square aria-hidden="true" />
            Stop
          </button>
        ) : onClose ? (
          <button type="button" className="agent-activity__stop" onClick={onClose}>
            Close
          </button>
        ) : null}
      </header>

      <ol className="agent-activity__steps">
        {visible.map((step, index) => {
          const Icon = ACTION_ICONS[step.action] || CircleAlert;
          const failed = step.status === "failed";
          const blocked = step.status === "blocked";
          const active = step.status === "running";
          const tone = failed ? "failed" : blocked ? "blocked" : active ? "active" : "done";
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
                  <small>Step {step.step}</small>
                </div>
                {step.reason ? <p>{step.reason}</p> : null}
                <StepDetail step={step} />
              </div>
            </li>
          );
        })}
      </ol>
    </aside>
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
