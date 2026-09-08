import { useEffect, useState } from "react";

const icons = new Map();
function loadIcon(url) {
  let origin;
  try { const parsed = new URL(url);if (!/^https?:$/.test(parsed.protocol)) return Promise.resolve(null);origin = parsed.origin; }
  catch { return Promise.resolve(null); }
  if (!icons.has(origin)) {
    icons.set(origin, fetch(`/api/web/favicon?url=${encodeURIComponent(origin)}`).then(r=>r.ok?r.json():null).then(r=>r?.data?.icon||r?.icon||null).catch(()=>null));
    if (icons.size > 128) icons.delete(icons.keys().next().value);
  }
  return icons.get(origin);
}

export function SiteIcon({ url, host = "" }) {
  const [icon, setIcon] = useState(null);
  useEffect(() => { let active=true;setIcon(null);loadIcon(url).then(value=>{if(active)setIcon(value);});return ()=>{active=false;}; }, [url]);
  return <span className="site-icon" aria-hidden="true">{icon ? <img src={icon} alt="" width="16" height="16" /> : <span>{host.replace(/^www\./, "").charAt(0).toUpperCase() || "↗"}</span>}</span>;
}
