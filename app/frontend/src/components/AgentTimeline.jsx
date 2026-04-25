import { useState } from "react";
import {
  AppWindow,
  Camera,
  Check,
  ChevronRight,
  Circle,
  Globe,
  Keyboard,
  ListTree,
  MousePointer2,
  Search,
  SquareTerminal,
  Layers,
} from "lucide-react";
import {
  eventArtifact,
  eventDetail,
  eventState,
  eventSummary,
  executionEvents,
  formatDuration,
  toolPresentation,
} from "../workflows/agentTimeline.mjs";
import { activityAccount } from "../workflows/activitySummary.mjs";

const ICONS = {
  terminal: SquareTerminal,
  globe: Globe,
  camera: Camera,
  app: AppWindow,
  window: Layers,
  pointer: MousePointer2,
  keyboard: Keyboard,
  search: Search,
  list: ListTree,
  check: Check,
  dot: Circle,
};






function AgentBlock({ event, artifactSource }) {
  const [open, setOpen] = useState(false);
  const presentation = toolPresentation(event.action);
  const Icon = ICONS[presentation.icon] || Circle;
  const state = eventState(event);
  const { input, output } = eventDetail(event);
  const artifact = eventArtifact(event);
  const duration = formatDuration(event.duration_ms);
  const expandable = Boolean(input || output);

  return (
    <li className={`agent-block agent-block--${state}`}>
      <span className="agent-block__marker" aria-hidden="true" />
      <div className="agent-block__main">
        <button
          type="button"
          className="agent-block__head"
          aria-expanded={expandable ? open : undefined}
          disabled={!expandable}
          onClick={() => expandable && setOpen((value) => !value)}
        >
          {expandable ? (
            <ChevronRight
              className={`agent-block__chevron ${open ? "agent-block__chevron--open" : ""}`}
              aria-hidden="true"
            />
          ) : (
            <span className="agent-block__chevron" aria-hidden="true" />
          )}
          <Icon className="agent-block__icon" aria-hidden="true" />
          <span className="agent-block__tool">{presentation.label}</span>
          <span className="agent-block__summary">{eventSummary(event)}</span>
          {duration ? <span className="agent-block__duration">{duration}</span> : null}
        </button>

        {open && expandable ? (
          <div className="agent-block__detail">
            {input ? (
              <div className="agent-block__stream">
                <span className="agent-block__stream-label">In</span>
                <pre>{input}</pre>
              </div>
            ) : null}
            {output ? (
              <div className="agent-block__stream">
                <span className="agent-block__stream-label">Out</span>
                <pre>{output}</pre>
              </div>
            ) : null}
          </div>
        ) : null}

        {artifact && artifactSource ? (
          <figure className="agent-block__artifact">
            <img src={artifactSource(artifact)} alt="Captured screen" loading="lazy" />
          </figure>
        ) : null}
      </div>
    </li>
  );
}













function ActivityPhase({ phase, artifactSource, startOpen }) {
  const [open, setOpen] = useState(startOpen);
  const Icon = ICONS[PHASE_ICONS[phase.phase] || "dot"] || Circle;
  const duration = formatDuration(phase.milliseconds);
  const detail = [phase.detail, duration].filter(Boolean).join(" · ");

  return (
    <li className="activity-phase">
      <button
        type="button"
        className="activity-phase__head"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronRight
          className={`activity-phase__chevron ${open ? "activity-phase__chevron--open" : ""}`}
          aria-hidden="true"
        />
        <Icon className="activity-phase__icon" aria-hidden="true" />
        <span className="activity-phase__label">{phase.label}</span>
        <span className="activity-phase__detail">{detail}</span>
      </button>
      {open ? (
        <ol className="agent-timeline__list activity-phase__steps">
          {phase.events.map((event, index) => (
            <AgentBlock
              key={`${event.step ?? index}-${event.action}`}
              event={event}
              artifactSource={artifactSource}
            />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

const PHASE_ICONS = {
  web: "globe",
  vision: "camera",
  windows: "app",
  terminal: "terminal",
  files: "list",
  image: "camera",
  planning: "list",
  connected: "layers",
  other: "dot",
};












export function AgentTimeline({
  events,
  running = false,
  artifactSource = null,
  sites = 0,
  pages = 0,
  state = "",
}) {
  const blocks = executionEvents(events);
  if (!blocks.length && !running) return null;
  const account = activityAccount(blocks, { sites, pages, state });

  return (
    <section className="agent-timeline" aria-label="What this turn did">
      <ol className="agent-timeline__phases">
        {account.phases.map((phase) => (
          <ActivityPhase
            key={phase.id}
            phase={phase}
            artifactSource={artifactSource}


            startOpen={running}
          />
        ))}
        {running ? (
          <li className="activity-phase activity-phase--running">
            <span className="activity-phase__head activity-phase__head--static">
              <span className="activity-phase__chevron" aria-hidden="true" />
              <Circle className="activity-phase__icon" aria-hidden="true" />
              <span className="activity-phase__label">Working</span>
            </span>
          </li>
        ) : null}
      </ol>
      {account.outcome ? (
        <p className="activity-phase__outcome">{account.outcome}</p>
      ) : null}
    </section>
  );
}
