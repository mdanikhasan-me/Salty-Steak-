import { useState } from "react";
import { Check } from "lucide-react";

import { researchProgress } from "../workflows/researchProgress.mjs";
import { SiteIcon } from "./SiteIcon.jsx";



export function ResearchProgress({ details, compact = false }) {
  const [expanded, setExpanded] = useState(false);
  const account = researchProgress(details);
  if (!account) return null;
  const sources = [...new Map(account.waves.flatMap(wave=>wave.sites).map(site=>[site.host,site])).values()];

  return (
    <section
      className={`research-progress ${compact ? "research-progress--compact" : ""}`}
      aria-label="Research in progress"
      aria-live="polite"
      aria-busy={account.running}
    >
      <button type="button" className="research-source-summary" aria-expanded={expanded} onClick={()=>setExpanded(value=>!value)}>
        <span className="research-source-icons">{sources.slice(0,3).map(site=><SiteIcon key={site.host} url={site.url} host={site.host} />)}</span>
        <span>{account.running ? "Reading sources" : `${account.siteCount} websites explored`}</span>
        {account.verified > 0 ? <small>{account.verified} pages read</small> : null}
        <span aria-hidden="true">{expanded ? "⌃" : "⌄"}</span>
      </button>
      {expanded ? <div className="research-source-list">{sources.map(site=><a key={site.host} href={site.url} target="_blank" rel="noreferrer"><SiteIcon url={site.url} host={site.host}/><span><strong>{site.title || site.host}</strong><small>{site.host}</small></span>{["validated","verified"].includes(site.state)?<Check aria-label="Read"/>:null}</a>)}</div> : null}
    </section>
  );
}

export default ResearchProgress;
