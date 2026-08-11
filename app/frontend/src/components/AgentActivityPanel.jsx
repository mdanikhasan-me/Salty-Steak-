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
  X,
} from "lucide-react";

const ACTION_ICONS = {
  "screen.capture": Monitor,
  "input.control": MousePointerClick,
  "application.launch": AppWindow,
  "terminal.execute": Terminal,
  "browser.control": Globe,
  "ui.automation": MousePointerClick,
  "discord.inspect": Search,
  "window.control": AppWindow,
  "image.generate": ImageIcon,
  research: Search,
  connector: Mail,
  respond: Check,
};

const ACTION_LABELS = {
  "screen.capture": "Checked the screen",
  "input.control": "Worked in the application",
  "application.launch": "Opened an application",
  "terminal.execute": "Ran a local command",
  "browser.control": "Reviewed web content",
  "ui.automation": "Worked in the application",
  "discord.inspect": "Reviewed Discord",
  "window.control": "Worked in the application",
  "image.generate": "Created an image",
  research: "Reviewed sources",
  connector: "Used a connected service",
  respond: "Prepared the answer",
};

const PHASE_LABELS = {
  discord: "Worked in Discord",
  computer: "Worked in the application",
  screen: "Checked the screen",
  terminal: "Ran local commands",
  research: "Reviewed sources",
  image: "Created an image",
  connector: "Used a connected service",
  finish: "Prepared the answer",
};

const STATE_COPY = {
  queued: "Getting ready",
  planning: "Planning the next step",
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
  exhausted: "Reached the decision guard",
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
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  }
  return `${minutes}m ${Math.round(value % 60)}s`;
}

function stepSummary(task, visibleCount, running) {
  const planned = Number(task?.planned_step_count);
  if (Number.isFinite(planned) && planned > 0) {
    return `Step ${Math.min(visibleCount, planned)} of ${planned}`;
  }
  const measured = Number(task?.step_count);
  const completed = Number.isFinite(measured) ? Math.max(measured, visibleCount) : visibleCount;
  if (!completed) return running ? "Starting" : "";
  return `Step ${completed}`;
}

function stepTone(step) {
  if (step.status === "failed") return "failed";
  if (step.status === "blocked") return "blocked";
  if (step.status === "running") return "active";
  return "done";
}

function stepPhase(step) {
  const action = String(step.action || "");
  const reason = String(step.reason || "").toLowerCase();
  if (
    action === "discord.inspect"
    || /discord|giveaway|server list|channel|quick switcher/.test(reason)
  ) return "discord";
  if (action === "screen.capture") return "screen";
  if (action === "terminal.execute") return "terminal";
  if (action === "browser.control" || action === "research") return "research";
  if (action === "image.generate") return "image";
  if (action === "connector") return "connector";
  if (action === "respond") return "finish";
  if (["input.control", "application.launch", "ui.automation", "window.control"].includes(action)) {
    return "computer";
  }
  return action || "computer";
}

function readableReason(value) {
  return String(value || "")
    .replaceAll("UIA", "window")
    .replaceAll("UI tree", "visible controls")
    .replaceAll("control tree", "visible controls")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 260);
}

function groupActivitySteps(steps) {
  return steps.reduce((groups, step) => {
    const phase = stepPhase(step);
    const tone = stepTone(step);
    const previous = groups.at(-1);
    if (previous && previous.phase === phase && previous.tone === tone) {
      previous.steps.push(step);
      return groups;
    }
    groups.push({ phase, tone, steps: [step] });
    return groups;
  }, []);
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
  const groups = groupActivitySteps(steps);
  const elapsed = elapsedLabel(task.elapsed_seconds);
  const summary = stepSummary(task, steps.length + (current ? 1 : 0), running);
  const waiting = task.waiting_for ? WAITING_COPY[task.waiting_for] : null;
  const approval = task.approval || null;
  const metrics = task.metrics || {};
  const preview = task.generation_preview || {};
  const budget = task.mission_budget || {};
  const remaining = elapsedLabel(budget.remaining_seconds);
  const liveSummary = String(
    current?.reason || preview.summary || STATE_COPY[state] || "Working on your request",
  );
  const totalOutputTokens = Number(metrics.planning_output_tokens);
  const liveCounts = [
    Number.isFinite(totalOutputTokens)
      ? { label: "Total output tokens", value: totalOutputTokens.toLocaleString() }
      : null,
    remaining ? { label: "Time remaining", value: remaining } : null,
  ].filter(Boolean);

  return (
    <aside className="agent-activity" aria-label="Activity">
      <header className="agent-activity__header">
        <div className="agent-activity__heading">
          <span className="agent-activity__eyebrow">Activity</span>
          <strong>{task.state_label || STATE_COPY[state] || "Working"}</strong>
          <span className="agent-activity__meta">
            {[summary, elapsed].filter(Boolean).join(" / ")}
          </span>
        </div>
        <div className="agent-activity__header-actions">
          {running || stopping ? (
            <button
              type="button"
              className="agent-activity__stop"
              onClick={onStop}
              disabled={stopping}
              aria-live="polite"
            >
              <Square aria-hidden="true" />
              {stopping ? "Stopping..." : "Stop"}
            </button>
          ) : null}
          {onClose ? (
            <button
              type="button"
              className="agent-activity__close"
              onClick={onClose}
              aria-label="Close activity"
            >
              <X aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </header>

      <div className="agent-activity__body">
        {waiting ? (
          <div className="agent-activity__waiting" role="status">
            <ShieldAlert aria-hidden="true" />
            <div>
              <strong>{waiting}</strong>
              {task.waiting_detail?.url ? <p>{task.waiting_detail.url}</p> : null}
            </div>
          </div>
        ) : null}

        {running && !waiting ? (
          <section className="agent-activity__live" role="status" aria-live="polite">
            <span className="agent-activity__section-label">Now</span>
            <strong>{readableReason(liveSummary)}</strong>
            {liveCounts.length ? (
              <dl className="agent-activity__live-metrics" aria-label="Live task telemetry">
                {liveCounts.map((item) => (
                  <div key={item.label}>
                    <dt>{item.label}</dt>
                    <dd>{item.value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </section>
        ) : null}

        {approval ? (
          <ApprovalCard approval={approval} onApprove={onApprove} onReject={onReject} />
        ) : null}

        {groups.length ? (
          <section className="agent-activity__timeline" aria-label="Progress summary">
            <header>
              <strong>{running ? "Progress" : "What happened"}</strong>
              <span>{`${steps.length} action${steps.length === 1 ? "" : "s"}`}</span>
            </header>
            <ol className="agent-activity__groups">
              {groups.map((group, index) => (
                <ActivityGroup
                  key={`${group.phase}-${group.steps[0]?.step}-${index}`}
                  group={group}
                />
              ))}
            </ol>
          </section>
        ) : null}

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
              <TechnicalFact label="Total output tokens" value={metrics.planning_output_tokens} />
              <TechnicalFact label="Current decision tokens" value={preview.token_count} />
              <TechnicalFact label="Output characters" value={metrics.planning_output_characters} />
              <TechnicalFact label="Planning calls" value={metrics.planning_model_calls} />
              <TechnicalFact label="Image calls" value={metrics.image_model_calls} />
              <TechnicalFact label="Tool calls" value={metrics.tool_calls} />
              <TechnicalFact label="Retries" value={metrics.retries} />
              <TechnicalFact label="Replans" value={metrics.replans} />
              <TechnicalFact label="Screenshots" value={metrics.screenshots} />
              <TechnicalFact label="Context rollovers" value={metrics.context_compactions} />
              <TechnicalFact
                label="Mission time limit"
                value={elapsedLabel(budget.duration_limit_seconds)}
              />
              <TechnicalFact label="Decision guard" value={budget.step_limit} />
              {task.failure ? <TechnicalFact label="Failure" value={task.failure} /> : null}
            </dl>
          ) : null}
        </div>
      </div>
    </aside>
  );
}

function ActivityGroup({ group }) {
  const latest = group.steps.at(-1) || {};
  const Icon = ACTION_ICONS[latest.action] || CircleAlert;
  const reasons = [...new Set(group.steps.map((step) => readableReason(step.reason)).filter(Boolean))];
  const latestReason = reasons.at(-1) || "Completed this part of the task.";
  const count = group.steps.length;
  const hasDetails = count > 1 || latest.status === "failed" || latest.action === "terminal.execute";
  return (
    <li className={`agent-activity__group agent-activity__group--${group.tone}`}>
      <span className="agent-activity__group-marker" aria-hidden="true">
        {group.tone === "blocked" ? <ShieldAlert /> : <Icon />}
      </span>
      <div className="agent-activity__group-body">
        <div className="agent-activity__group-title">
          <strong>{PHASE_LABELS[group.phase] || ACTION_LABELS[latest.action] || "Worked on the task"}</strong>
          <small>
            {group.tone === "failed"
              ? "Failed"
              : group.tone === "blocked"
                ? "Blocked"
                : `${count} action${count === 1 ? "" : "s"}`}
          </small>
        </div>
        <p>{latestReason}</p>
        {hasDetails ? (
          <details className="agent-activity__group-details">
            <summary>{count > 1 ? `Show ${count} steps` : "Show details"}</summary>
            {reasons.length ? (
              <ol>
                {reasons.map((reason) => <li key={reason}>{reason}</li>)}
              </ol>
            ) : null}
            <StepDetail step={latest} />
          </details>
        ) : null}
        {latest.verified === true ? <small className="agent-step__verified">Confirmed</small> : null}
      </div>
    </li>
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
        <button type="button" className="agent-approval__reject" onClick={() => onReject?.(approval)}>
          Reject
        </button>
        <button type="button" className="agent-approval__approve" onClick={() => onApprove?.(approval)}>
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
        {`Captured ${observation.image_width || "?"}x${observation.image_height || "?"}`}
        {Number(observation.scale_divisor) > 1
          ? ` from a ${observation.screen_width}x${observation.screen_height} screen`
          : ""}
      </p>
    );
  }
  if (step.action === "terminal.execute") {
    const output = String(observation.stdout || observation.stderr || "").trim();
    if (!output) return null;
    return <pre className="agent-step__output">{output.length > 600 ? `${output.slice(0, 600)}...` : output}</pre>;
  }
  if (step.action === "application.launch" && observation.target) {
    return <p className="agent-step__detail">{observation.target}</p>;
  }
  return null;
}
