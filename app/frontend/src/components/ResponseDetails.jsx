import { useEffect, useRef } from "react";
import { Check, ExternalLink, Search, Sparkles, Wrench, X } from "lucide-react";

import {
  formatElapsed,
  responseDetails,
  supportFor,
} from "../workflows/responseProvenance.mjs";













const ACTIVITY_ICONS = {
  search: Search,
  read: ExternalLink,
  compare: Check,
  conflict: Sparkles,
  note: Sparkles,
  tool: Wrench,
  finished: Check,
};

function SourceChip({ source }) {
  return (
    <a
      className="source-chip"
      href={source.url}
      target="_blank"
      rel="noreferrer noopener"
      title={source.title}
    >
      {source.host || source.title}
    </a>
  );
}

export function ResponseDetails({ details, onClose }) {
  const panel = useRef(null);
  const account = responseDetails(details);

  useEffect(() => {
    panel.current?.focus();
    function onKey(event) {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      onClose?.();
    }
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return (
    <aside
      className="response-details"
      role="dialog"
      aria-label="Response details"
      tabIndex={-1}
      ref={panel}
    >
      <header className="response-details__head">
        <strong>{account.completion || "Response details"}</strong>
        <button type="button" onClick={onClose} aria-label="Close response details">
          <X aria-hidden="true" />
        </button>
      </header>

      <div className="response-details__body">
        {account.activity.length ? (
          <section className="response-section">
            <h3>Activity</h3>
            <ol className="response-activity">
              {account.activity.map((event, index) => {
                const Icon = ACTIVITY_ICONS[event.kind] || Wrench;
                return (
                  <li key={`${event.kind}-${index}`} className={`response-step response-step--${event.kind}`}>
                    <span className="response-step__marker" aria-hidden="true">
                      <Icon />
                    </span>
                    <div className="response-step__body">
                      <p>{event.text}</p>
                      {event.sources?.length ? (
                        <div className="response-step__chips">
                          {event.sources.slice(0, 8).map((source) => (
                            <SourceChip key={source.id} source={source} />
                          ))}
                        </div>
                      ) : null}
                    </div>
                    {event.durationMs ? (
                      <span className="response-step__time">
                        {formatElapsed(event.durationMs)}
                      </span>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </section>
        ) : null}

        {account.memories.length ? (
          <section className="response-section">
            <h3>
              Memory <small>{account.memories.length}</small>
            </h3>
            <ul className="response-memories">
              {account.memories.map((memory) => (
                <li key={memory}>{memory}</li>
              ))}
            </ul>
          </section>
        ) : null}

        {account.sources.length ? (
          <section className="response-section">
            <h3>
              Sources <small>{account.sources.length}</small>
            </h3>
            <ul className="response-sources">
              {account.sources.map((source) => {
                const support = supportFor(details, source.id);
                return (
                  <li key={source.id}>
                    <a
                      href={source.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="response-source"
                    >
                      <span className="response-source__host">{source.host}</span>
                      <span className="response-source__title">{source.title}</span>
                      {support.length ? (
                        <span className="response-source__support">{support[0]}</span>
                      ) : null}
                    </a>
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
