import { useState } from "react";
import { Check, ChevronDown, CircleAlert } from "lucide-react";

import { researchProgress } from "../workflows/researchProgress.mjs";
import { SiteIcon } from "./SiteIcon.jsx";



export function ResearchProgress({ details, compact = false }) {
  const [expanded, setExpanded] = useState(false);
  const account = researchProgress(details);
  if (!account) return null;
  const sources = account.sources;

  return (
    <section
      className={`research-progress ${compact ? "research-progress--compact" : ""}`}
      aria-label="Research in progress"
      aria-live="polite"
      aria-busy={account.running}
    >
      <button type="button" className="research-source-summary" aria-expanded={expanded} onClick={()=>setExpanded(value=>!value)}>
        <span className="research-source-icons">{sources.slice(0,3).map(site=><SiteIcon key={site.host} url={site.url} host={site.host} />)}</span>
        {account.failure ? <CircleAlert className="research-source-error-icon" aria-hidden="true"/> : null}
        <span>{account.failure ? "Search interrupted" : account.running ? "Reading sources" : `${account.siteCount} websites explored`}</span>
        {account.verified > 0 ? <small>{account.verified} validated</small> : null}
        <ChevronDown className="research-source-toggle" data-open={expanded || undefined} aria-hidden="true"/>
      </button>
      {account.failure ? <p className="research-source-error" role="status">{account.failure}</p> : null}
      {expanded ? <div className="research-source-list">{sources.map(site=><a key={site.host} href={site.url} target="_blank" rel="noreferrer"><SiteIcon url={site.url} host={site.host}/><span><strong>{site.title || site.host}</strong><small>{site.host}</small></span>{["validated","verified"].includes(site.state)?<Check aria-label="Read"/>:null}</a>)}</div> : null}
    </section>
  );
}

export default ResearchProgress;
