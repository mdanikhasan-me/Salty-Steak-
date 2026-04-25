

















const PHASE_OF_ACTION = {
  "browser.control": "web",
  research: "web",
  "screen.capture": "vision",
  "ui.automation": "windows",
  "window.control": "windows",
  "application.launch": "windows",
  "input.control": "windows",
  "terminal.execute": "terminal",
  "files.read": "files",
  "files.write": "files",
  "image.generate": "image",
  plan: "planning",
};



const PHASE = {
  web: { label: "Web searched", unit: "page" },
  vision: { label: "Looked at the screen", unit: "capture" },
  windows: { label: "Used this computer", unit: "action" },
  terminal: { label: "Ran commands", unit: "command" },
  files: { label: "Read files", unit: "file" },
  image: { label: "Generated an image", unit: "image" },
  planning: { label: "Planned the work", unit: "step" },
  other: { label: "Worked", unit: "step" },
};

function phaseOf(event) {
  const action = String(event?.action || "");
  if (PHASE_OF_ACTION[action]) return PHASE_OF_ACTION[action];

  if (action.includes(".")) return "connected";
  return "other";
}

function serviceLabel(action) {
  const service = String(action || "").split(".")[0];
  if (!service) return "a connected app";
  return service.charAt(0).toUpperCase() + service.slice(1);
}

function plural(count, unit) {
  return `${count} ${unit}${count === 1 ? "" : "s"}`;
}








export function activityPhases(events, { sites = 0, pages = 0 } = {}) {
  const runs = [];
  for (const event of events || []) {
    const phase = phaseOf(event);
    const previous = runs[runs.length - 1];
    if (previous && previous.phase === phase) {
      previous.events.push(event);
      continue;
    }
    runs.push({ phase, events: [event] });
  }

  return runs.map((run, index) => {
    const failed = run.events.filter(
      (event) => String(event?.status || "") === "failed",
    ).length;
    const milliseconds = run.events.reduce(
      (total, event) => total + (Number(event?.duration_ms) || 0),
      0,
    );
    const descriptor =
      run.phase === "connected"
        ? { label: `Used ${serviceLabel(run.events[0]?.action)}`, unit: "operation" }
        : PHASE[run.phase] || PHASE.other;




    let counted = plural(run.events.length, descriptor.unit);
    if (run.phase === "web" && pages) {
      counted = sites
        ? `${plural(sites, "site")}, ${plural(pages, "page")}`
        : plural(pages, "page");
    }

    return {
      id: `${run.phase}-${index}`,
      phase: run.phase,
      label: descriptor.label,
      detail: counted,
      milliseconds,
      failed,
      steps: run.events.length,
      events: run.events,
    };
  });
}







export function outcomeLabel(state) {
  switch (String(state || "")) {
    case "stopped":
      return "Stopped";
    case "failed":
      return "Couldn't finish";
    case "partial":
      return "Incomplete";
    case "waiting":
      return "Waiting for you";
    case "rendering":
      return "Creating the image";
    default:
      return "";
  }
}





export function activityAccount(events, options = {}) {
  const phases = activityPhases(events, options);
  const total = phases.reduce((sum, phase) => sum + phase.steps, 0);
  return {
    phases,
    steps: total,

    expandable: total > 0,
    outcome: outcomeLabel(options.state),
  };
}

export default activityAccount;
