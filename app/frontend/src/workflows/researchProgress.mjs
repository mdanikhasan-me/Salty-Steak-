














export function siteIcon(url) {
  try {
    const parsed = new URL(String(url));
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return "";
    return `${parsed.protocol}//${parsed.host}/favicon.ico`;
  } catch {
    return "";
  }
}

function niceHost(host) {
  return String(host || "").replace(/^www\./, "");
}









export function researchWaves(progress) {
  const waves = (progress?.waves || []).filter(Boolean);
  return waves.map((wave, index) => {
    const sites = (wave.sites || []).filter((site) => site && site.host);
    const opened = Number(wave.opened) || 0;
    const verified = Number(wave.verified) || 0;
    const done = String(wave.state || "") === "done";

    return {
      id: `wave-${index}`,

      label: done
        ? `Searched ${sites.length} ${sites.length === 1 ? "website" : "websites"}`
        : `Searching ${sites.length} ${sites.length === 1 ? "website" : "websites"}`,
      query: String(wave.query || ""),
      running: !done,
      opened,
      verified,
      detail: detailFor({ opened, verified, done }),
      sites: sites.map((site) => ({
        host: niceHost(site.host),
        url: String(site.url || ""),
        title: String(site.title || ""),
        state: String(site.state || "found"),
        icon: siteIcon(site.url),
      })),
    };
  });
}

function detailFor({ opened, verified, done }) {
  if (verified) {
    return `${verified} verified${opened ? ` of ${opened} read` : ""}`;
  }
  if (opened) return `${opened} read`;
  return done ? "nothing usable" : "";
}







export function researchProgress(details) {
  const progress = details?.research_progress;
  const waves = researchWaves(progress);
  if (!waves.length) return null;

  const sites = new Set();
  for (const wave of waves) {
    for (const site of wave.sites) sites.add(site.host);
  }
  const running = waves.some((wave) => wave.running);

  return {
    phase: String(progress?.phase || ""),
    running,
    waves,
    siteCount: sites.size,
    verified: waves.reduce((total, wave) => total + wave.verified, 0),

    summary: `Searched ${sites.size} ${sites.size === 1 ? "website" : "websites"}`,
  };
}

export default researchProgress;
