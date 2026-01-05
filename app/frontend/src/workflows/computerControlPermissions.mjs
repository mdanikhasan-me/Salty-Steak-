export const AUTOMATION_SCHEMA = "salty-steak-windows-automation-v1";

export const COMPUTER_CONTROL_CAPABILITIES = Object.freeze([
  Object.freeze({
    id: "terminal.execute",
    uiId: "terminal",
    name: "Terminal",
    description: "Run one reviewed argv command inside the granted project directory.",
    invokeLabel: "Run command",
  }),
  Object.freeze({
    id: "screen.capture",
    uiId: "screen_capture",
    name: "Screen capture",
    description: "Capture the primary screen to an app-owned local artifact.",
    invokeLabel: "Capture now",
  }),
  Object.freeze({
    id: "input.control",
    uiId: "input_control",
    name: "Mouse and keyboard",
    description: "Move and click the pointer and send keystrokes to whichever window has focus.",
    invokeLabel: "Send input",
  }),
  Object.freeze({
    id: "application.launch",
    uiId: "application_launch",
    name: "Open apps and links",
    description: "Start an installed application or open a web link with its default handler.",
    invokeLabel: "Open now",
  }),
  Object.freeze({
    id: "window.control",
    uiId: "window_control",
    name: "Windows and focus",
    description: "List open windows and bring one to the front or close it by its title.",
    invokeLabel: "List windows",
  }),
]);

const ADAPTER_METHODS = Object.freeze([
  "getStatus",
  "grant",
  "revoke",
  "invoke",
  "getAudit",
]);

export function createComputerControlAdapter(methods) {
  if (!methods || typeof methods !== "object") {
    throw new TypeError("Computer-control adapter methods are required");
  }
  const adapter = {};
  for (const method of ADAPTER_METHODS) {
    if (typeof methods[method] !== "function") {
      throw new TypeError(`Computer-control adapter requires ${method}()`);
    }
    adapter[method] = methods[method];
  }
  return Object.freeze(adapter);
}

export function hasComputerControlAdapter(value) {
  return Boolean(
    value && ADAPTER_METHODS.every((method) => typeof value[method] === "function"),
  );
}

export function normaliseAutomationStatus(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("Automation status is not an object");
  }
  if (value.schema !== AUTOMATION_SCHEMA) {
    throw new TypeError("Automation status schema is unsupported");
  }
  if (
    value.local_only !== true ||
    value.external_service_required !== false ||
    value.starts_network_service !== false ||
    value.default_enabled !== false ||
    value.explicit_persisted_user_grant_required !== true
  ) {
    throw new TypeError("Automation status did not preserve the fail-closed contract");
  }
  const rawCapabilities = Array.isArray(value.capabilities) ? value.capabilities : [];
  const capabilityMap = new Map(
    rawCapabilities.map((item) => [String(item?.capability || ""), item]),
  );
  return Object.freeze({
    schema: AUTOMATION_SCHEMA,
    platform: String(value.platform || "unknown"),
    limits: normaliseLimits(value.limits),
    riskBoundaries: value.risk_boundaries && typeof value.risk_boundaries === "object"
      ? { ...value.risk_boundaries }
      : {},
    capabilities: COMPUTER_CONTROL_CAPABILITIES.map((definition) => (
      normaliseCapability(capabilityMap.get(definition.id), definition)
    )),
    activeInvocations: Array.isArray(value.active_invocations)
      ? value.active_invocations.map((item) => ({ ...item }))
      : [],
    auditRecordCount: finiteNonNegative(value.audit_record_count) ?? 0,
  });
}

export function unavailableAutomationStatus(reason) {
  const detail = String(reason || "Automation status is unavailable.");
  return Object.freeze({
    schema: "unavailable",
    platform: "unknown",
    limits: normaliseLimits({}),
    riskBoundaries: {},
    capabilities: COMPUTER_CONTROL_CAPABILITIES.map((definition) => Object.freeze({
      ...definition,
      available: false,
      granted: false,
      effectiveEnabled: false,
      constraints: {},
      detail,
      stateLabel: "Unavailable",
      stateTone: "unavailable",
      actionLabel: "Unavailable",
    })),
    activeInvocations: [],
    auditRecordCount: 0,
  });
}

export function grantRequest(capability, workingDirectoryRoot) {
  const definition = capabilityDefinition(capability);
  const request = {
    capabilities: [definition.id],
    user_confirmed: true,
  };
  if (definition.id === "terminal.execute") {
    const root = requiredText(workingDirectoryRoot, "working directory root", 32_768);
    request.working_directory_root = root;
  }
  return Object.freeze(request);
}

export function revokeRequest(capability) {
  return Object.freeze({ capabilities: [capabilityDefinition(capability).id] });
}

export function prepareTerminalInvocation(value, limits = {}) {
  const source = value && typeof value === "object" ? value : {};
  let argv;
  try {
    argv = JSON.parse(String(source.argvText || ""));
  } catch {
    throw new TypeError("Command arguments must be a valid JSON array");
  }
  const maximumArguments = finitePositiveInteger(limits.maxArguments) ?? 128;
  const maximumCharacters = finitePositiveInteger(limits.maxArgumentCharacters) ?? 32_768;
  if (!Array.isArray(argv) || argv.length < 1 || argv.length > maximumArguments) {
    throw new TypeError(`Command arguments must contain 1 to ${maximumArguments} items`);
  }
  if (argv.some((item) => typeof item !== "string" || !item || item.includes("\0"))) {
    throw new TypeError("Every command argument must be non-empty text without NUL characters");
  }
  if (argv.reduce((total, item) => total + item.length, 0) > maximumCharacters) {
    throw new TypeError(`Command arguments exceed ${maximumCharacters} characters`);
  }
  const workingDirectory = requiredText(source.workingDirectory, "working directory", 32_768);
  const maximumTimeout = finitePositive(limits.maxTimeoutSeconds) ?? 30;
  const timeoutSeconds = Number(source.timeoutSeconds);
  if (!Number.isFinite(timeoutSeconds) || timeoutSeconds < 0.05 || timeoutSeconds > maximumTimeout) {
    throw new TypeError(`Command timeout must be between 0.05 and ${maximumTimeout} seconds`);
  }
  const argumentsPayload = Object.freeze({
    argv: [...argv],
    working_directory: workingDirectory,
    timeout_seconds: timeoutSeconds,
  });
  return Object.freeze({
    capability: "terminal.execute",
    arguments: argumentsPayload,
    summary: `argv ${JSON.stringify(argv)}; working directory ${workingDirectory}; timeout ${timeoutSeconds} seconds`,
  });
}

export function prepareScreenCaptureInvocation(capability) {
  const definition = capabilityDefinition(capability);
  if (definition.id !== "screen.capture") {
    throw new TypeError("Screen-capture invocation requires screen.capture");
  }
  const outputRoot = requiredText(
    capability?.constraints?.output_root,
    "screen-capture output root",
    32_768,
  );
  return Object.freeze({
    capability: "screen.capture",
    arguments: Object.freeze({ screen: "primary" }),
    summary: `Capture the primary screen and save a BMP artifact under ${outputRoot}`,
  });
}

export function prepareInputControlInvocation(value) {
  const source = value && typeof value === "object" ? value : {};
  const action = String(source.action || "").trim();
  if (!action) throw new TypeError("Choose an input action");
  const payload = { action };
  if (action === "mouse_move" || action === "mouse_click" || action === "mouse_scroll") {
    for (const axis of ["x", "y"]) {
      const coordinate = Number(source[axis]);
      if (!Number.isSafeInteger(coordinate)) {
        throw new TypeError(`Screen coordinate ${axis} must be a whole number`);
      }
      payload[axis] = coordinate;
    }
  }
  if (action === "mouse_click") {
    const button = String(source.button || "left");
    if (!["left", "right", "middle"].includes(button)) {
      throw new TypeError("Mouse button must be left, right, or middle");
    }
    payload.button = button;
    payload.double = Boolean(source.double);
  }
  if (action === "mouse_scroll") {
    const clicks = Number(source.clicks);
    if (!Number.isSafeInteger(clicks) || clicks === 0) {
      throw new TypeError("Scroll clicks must be a non-zero whole number");
    }
    payload.clicks = clicks;
  }
  if (action === "key_press") {
    payload.key = requiredText(source.key, "key name", 40);
  }
  if (action === "type_text") {
    payload.text = requiredText(source.text, "text to type", 4_096);
  }
  if (action === "key_combo") {
    payload.combo = requiredText(source.combo, "key combination", 80);
  }
  return Object.freeze({
    capability: "input.control",
    arguments: Object.freeze(payload),
    summary: `Send ${action} to the focused window: ${JSON.stringify(payload)}`,
  });
}

export function prepareWindowControlInvocation(value) {
  const source = value && typeof value === "object" ? value : {};
  const action = String(source.action || "list").trim();
  if (!["list", "focus", "close"].includes(action)) {
    throw new TypeError("Window action must be list, focus, or close");
  }
  if (action === "list") {
    return Object.freeze({
      capability: "window.control",
      arguments: Object.freeze({ action }),
      summary: "List every visible window with its title",
    });
  }
  const title = requiredText(source.title, "window title", 512);
  return Object.freeze({
    capability: "window.control",
    arguments: Object.freeze({ action, title }),
    summary: `${action === "focus" ? "Bring to the front" : "Close"} the window matching ${title}`,
  });
}

export function prepareApplicationLaunchInvocation(value) {
  const source = value && typeof value === "object" ? value : {};
  const target = requiredText(source.target, "application, link, or file", 2_048);
  return Object.freeze({
    capability: "application.launch",
    arguments: Object.freeze({ target }),
    summary: `Open ${target} with its default Windows handler`,
  });
}

export function normaliseAutomationAudit(value) {
  if (!Array.isArray(value)) throw new TypeError("Automation audit response is not an array");
  return value.map((item) => ({
    id: String(item?.id || ""),
    event: String(item?.event || "unknown"),
    capability: String(item?.capability || "unknown"),
    outcome: String(item?.outcome || "unknown"),
    createdAt: String(item?.created_at || ""),
    completedAt: item?.completed_at ? String(item.completed_at) : "",
  })).filter((item) => item.id);
}

export function automationResultSummary(result) {
  if (!result || typeof result !== "object") return "The broker returned no result details.";
  const status = String(result.status || "unknown");
  if (result.capability === "terminal.execute") {
    const exit = result.exit_code === null || result.exit_code === undefined
      ? "not reported"
      : String(result.exit_code);
    return `Terminal ${status}; exit code ${exit}; audit ${String(result.audit_record_id || "not reported")}.`;
  }
  if (result.capability === "screen.capture") {
    const path = String(result.artifact?.path || "no artifact retained");
    return `Screen capture ${status}; ${path}; audit ${String(result.audit_record_id || "not reported")}.`;
  }
  if (result.capability === "input.control") {
    return `Input ${status}; ${String(result.action || "action")}; audit ${String(result.audit_record_id || "not reported")}.`;
  }
  if (result.capability === "application.launch") {
    return `Launch ${status}; ${String(result.target || "target")}; audit ${String(result.audit_record_id || "not reported")}.`;
  }
  if (result.capability === "window.control") {
    const detail = result.action === "list"
      ? `${Number(result.window_count) || 0} windows`
      : String(result.window?.title || "window");
    return `Window ${status}; ${detail}; audit ${String(result.audit_record_id || "not reported")}.`;
  }
  return `Automation ${status}.`;
}

function normaliseCapability(value, definition) {
  if (!value || typeof value !== "object") {
    return Object.freeze({
      ...definition,
      available: false,
      granted: false,
      effectiveEnabled: false,
      constraints: {},
      detail: "The broker did not report this capability.",
      stateLabel: "Unavailable",
      stateTone: "unavailable",
      actionLabel: "Unavailable",
    });
  }
  const platformSupported = value.platform_supported === true;
  const constraintValid = value.constraint_valid === true;
  const granted = value.granted === true;
  const effectiveEnabled = Boolean(
    value.effective_enabled === true && granted && platformSupported && constraintValid,
  );
  const available = platformSupported && constraintValid;
  let stateLabel = "Not granted";
  let stateTone = "quiet";
  let actionLabel = "Review access";
  let detail = scopeSummary(definition.id, value.constraints);
  if (!platformSupported) {
    stateLabel = "Unavailable";
    stateTone = "unavailable";
    actionLabel = "Unavailable";
    detail = "The Windows automation broker is unavailable on this platform.";
  } else if (!constraintValid) {
    stateLabel = "Needs attention";
    stateTone = "error";
    actionLabel = "Unavailable";
    detail = String(value.constraint_error || "Persisted scope is invalid.");
  } else if (granted && !effectiveEnabled) {
    stateLabel = "Needs attention";
    stateTone = "error";
    actionLabel = "Unavailable";
    detail = "The persisted grant is not currently effective.";
  } else if (effectiveEnabled) {
    stateLabel = "Granted";
    stateTone = "connected";
    actionLabel = definition.invokeLabel;
  }
  return Object.freeze({
    ...definition,
    available,
    granted,
    effectiveEnabled,
    constraints: value.constraints && typeof value.constraints === "object"
      ? { ...value.constraints }
      : {},
    grantId: String(value.id || definition.id),
    grantedAt: value.granted_at ? String(value.granted_at) : "",
    revokedAt: value.revoked_at ? String(value.revoked_at) : "",
    detail,
    stateLabel,
    stateTone,
    actionLabel,
  });
}

function scopeSummary(capability, constraints) {
  if (capability === "terminal.execute") {
    return `Working-directory root: ${String(constraints?.working_directory_root || "not reported")}. This is not a filesystem or network sandbox.`;
  }
  return `Primary screen only. Artifacts: ${String(constraints?.output_root || "not reported")}. No visible capture indicator is provided.`;
}

function normaliseLimits(value) {
  const limits = value && typeof value === "object" ? value : {};
  return Object.freeze({
    maxTimeoutSeconds: finitePositive(limits.max_timeout_seconds) ?? 30,
    maxArguments: finitePositiveInteger(limits.max_arguments) ?? 128,
    maxArgumentCharacters: finitePositiveInteger(limits.max_argument_characters) ?? 32_768,
  });
}

function capabilityDefinition(value) {
  const id = typeof value === "string" ? value : String(value?.id || "");
  const definition = COMPUTER_CONTROL_CAPABILITIES.find((item) => item.id === id);
  if (!definition) throw new TypeError("Unknown computer-control capability");
  return definition;
}

function requiredText(value, label, maximum) {
  const text = String(value || "").trim();
  if (!text || text.length > maximum) throw new TypeError(`Computer-control ${label} is invalid`);
  return text;
}

function finiteNonNegative(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function finitePositive(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function finitePositiveInteger(value) {
  const number = finitePositive(value);
  return number !== null && Number.isInteger(number) ? number : null;
}
