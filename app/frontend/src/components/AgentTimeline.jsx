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






export function AgentTimeline({ events, running = false, artifactSource = null }) {
  const blocks = executionEvents(events);
  if (!blocks.length) return null;

  return (
    <section className="agent-timeline" aria-label="Agent execution">
      <ol className="agent-timeline__list">
        {blocks.map((event, index) => (
          <AgentBlock
            key={`${event.step ?? index}-${event.action}`}
            event={event}
            artifactSource={artifactSource}
          />
        ))}
        {running ? (
          <li className="agent-block agent-block--running agent-block--pending">
            <span className="agent-block__marker" aria-hidden="true" />
            <div className="agent-block__main">
              <span className="agent-block__head agent-block__head--static">
                <span className="agent-block__chevron" aria-hidden="true" />
                <Circle className="agent-block__icon" aria-hidden="true" />
                <span className="agent-block__tool">Working</span>
              </span>
            </div>
          </li>
        ) : null}
      </ol>
    </section>
  );
}
