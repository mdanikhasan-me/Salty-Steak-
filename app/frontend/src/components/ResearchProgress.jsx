import { useState } from "react";
import { Globe, Check } from "lucide-react";

import { researchProgress } from "../workflows/researchProgress.mjs";



function SiteChips({ sites }) {
  const [all, setAll] = useState(false);
  if (!sites.length) return null;
  const shown = all ? sites : sites.slice(0, 6);
  const hidden = sites.length - shown.length;

  return (
    <div className="research-wave__sites">
      {shown.map((site) => (
        <a
          key={site.host}
          className={`site-chips__chip research-wave__site research-wave__site--${site.state}`}
          href={site.url}
          title={site.title || site.host}
        >
          <span className="site-chips__icon" aria-hidden="true">
            {site.icon ? (
              <img
                src={site.icon}
                alt=""
                loading="lazy"
                onError={(event) => {
                  event.currentTarget.style.visibility = "hidden";
                }}
              />
            ) : null}
          </span>
          <span className="site-chips__host">{site.host}</span>
          {site.state === "validated" || site.state === "verified" ? (
            <Check className="research-wave__verified" aria-hidden="true" />
          ) : null}
        </a>
      ))}
      {hidden > 0 || all ? (
        <button
          type="button"
          className="site-chips__chip site-chips__chip--more"
          onClick={() => setAll((value) => !value)}
        >
          {all ? "Show less" : `${hidden} more`}
        </button>
      ) : null}
    </div>
  );
}










export function ResearchProgress({ details, compact = false }) {
  const account = researchProgress(details);
  if (!account) return null;

  return (
    <section
      className={`research-progress ${compact ? "research-progress--compact" : ""}`}
      aria-label="Research in progress"
      aria-live="polite"
      aria-busy={account.running}
    >
      {account.waves.map((wave) => (
        <div className="research-wave" key={wave.id}>
          <p className="research-wave__head">
            <Globe className="research-wave__icon" aria-hidden="true" />
            <span className="research-wave__label">{wave.label}</span>
            {wave.detail ? (
              <span className="research-wave__detail">{wave.detail}</span>
            ) : null}
          </p>
          <SiteChips sites={wave.sites} />
        </div>
      ))}
    </section>
  );
}

export default ResearchProgress;
