export const COMPOSER_MENU_IDS = Object.freeze([
  "attachments",
  "authority",
  "plugins",
  "model",
  "cooking",
]);

export const COMPUTER_AUTHORITY_MODES = Object.freeze([
  Object.freeze({
    id: "ask_every_time",
    label: "Ask every time",
    controlLabel: "Ask",
    description: "Review each computer action before Salty Steak runs it.",
  }),
  Object.freeze({
    id: "full_access",
    label: "Full access",
    controlLabel: "Full access",
    description: "Run computer actions immediately, including administrator actions through Windows UAC, with a local audit trail.",
  }),
]);

export function normaliseComputerAuthorityMode(value) {
  return String(value || "").trim().toLowerCase() === "full_access"
    ? "full_access"
    : "ask_every_time";
}

export function computerAuthorityLabel(value) {
  const selected = COMPUTER_AUTHORITY_MODES.find(
    (mode) => mode.id === normaliseComputerAuthorityMode(value),
  );
  return selected?.controlLabel || "Ask";
}

export const COOKING_MODES = Object.freeze([
  Object.freeze({
    id: "instant",
    label: "Blink",
    controlLabel: "Blink",
    description: "Direct answers; with Research on, run focused research for up to five minutes.",
  }),
  Object.freeze({
    id: "cooking",
    controlLabel: "Cook",
    label: "Cook",
    description: "Deliberate answers; with Research on, validate iteratively with a hard four-hour ceiling.",
  }),
  Object.freeze({
    id: "lock_in", controlLabel: "Lock In", label: "Lock In",
    description: "Maximum thinking, local builds and tests using installed tools, and up to two repairs. Runs generated code on this computer.",
  }),
]);

export const CONTEXT_WINDOW_PRESETS = Object.freeze([
  Object.freeze({ tokens: 16_384, label: "16K" }),
  Object.freeze({ tokens: 24_576, label: "24K" }),
  Object.freeze({ tokens: 32_768, label: "32K" }),
  Object.freeze({ tokens: 49_152, label: "48K" }),
  Object.freeze({ tokens: 65_536, label: "64K" }),
  Object.freeze({ tokens: 98_304, label: "96K" }),
  Object.freeze({ tokens: 131_072, label: "128K" }),
  Object.freeze({ tokens: 196_608, label: "192K" }),
  Object.freeze({ tokens: 262_144, label: "262K" }),
  Object.freeze({ tokens: 393_216, label: "384K" }),
  Object.freeze({ tokens: 524_288, label: "512K" }),
  Object.freeze({ tokens: 655_360, label: "640K" }),
  Object.freeze({ tokens: 800_000, label: "800K" }),
]);

export const MAXIMUM_OUTPUT_MODES = Object.freeze([
  Object.freeze({ id: "automatic", label: "Automatic" }),
  Object.freeze({ id: "manual", label: "Manual" }),
]);

export const MAXIMUM_OUTPUT_TOKEN_PRESETS = Object.freeze([
  256,
  512,
  1_024,
  2_048,
  4_096,
  8_192,
  16_384,
  32_768,
]);

export function toggleComposerMenu(currentMenu, requestedMenu) {
  if (!COMPOSER_MENU_IDS.includes(requestedMenu)) return null;
  return currentMenu === requestedMenu ? null : requestedMenu;
}

export function normaliseCookingMode(value) {
  const candidate = String(value || "").trim().toLowerCase();
  if (COOKING_MODES.some((mode) => mode.id === candidate)) return candidate;
  if (candidate === "off" || candidate === "blink") return "instant";
  if (candidate === "cook") return "cooking";
  if (candidate === "lock in") return "lock_in";
  if (candidate === "auto" || candidate === "deep") return "cooking";
  return "instant";
}

export function cookingModeLabel(value) {
  const selected = COOKING_MODES.find((mode) => mode.id === normaliseCookingMode(value));
  return selected?.controlLabel || "Blink";
}

export function normaliseReasoningVisibility(value) {
  return String(value || "").trim().toLowerCase() === "raw_local"
    ? "raw_local"
    : "summaries";
}

export function normaliseContextWindowTokens(value, fallback = 32_768) {
  const candidate = Number(value);
  if (CONTEXT_WINDOW_PRESETS.some((preset) => preset.tokens === candidate)) return candidate;
  const fallbackValue = Number(fallback);
  return CONTEXT_WINDOW_PRESETS.some((preset) => preset.tokens === fallbackValue)
    ? fallbackValue
    : 32_768;
}

export function normaliseMaximumOutputMode(value) {
  const candidate = String(value || "").trim().toLowerCase();
  return MAXIMUM_OUTPUT_MODES.some((mode) => mode.id === candidate) ? candidate : "automatic";
}

export function normaliseMaximumOutputTokens(value, fallback = 512) {
  const candidate = Number(value);
  if (MAXIMUM_OUTPUT_TOKEN_PRESETS.includes(candidate)) return candidate;
  const fallbackValue = Number(fallback);
  return MAXIMUM_OUTPUT_TOKEN_PRESETS.includes(fallbackValue) ? fallbackValue : 512;
}

export function contextWindowForMaximumOutput(value, currentContext = 32_768) {
  const outputTokens = normaliseMaximumOutputTokens(value);
  const selectedContext = normaliseContextWindowTokens(currentContext);
  if (outputTokens < selectedContext) return selectedContext;
  const preferredContext = Math.min(800_000, outputTokens * 2);
  return CONTEXT_WINDOW_PRESETS.find((preset) => preset.tokens >= preferredContext)?.tokens
    ?? CONTEXT_WINDOW_PRESETS.at(-1).tokens;
}

const GENERATION_SETTING_KEYS = Object.freeze([
  "context_window_tokens",
  "maximum_output_mode",
  "maximum_output_tokens",
  "temperature",
  "top_p",
  "top_k",
  "repetition_penalty",
  "seed",
  "reasoning_mode",
  "cooking_reasoning_tokens",
  "resource_mode",
  "reasoning_visibility",
  "web_search_enabled",
  "system_prompt",
  "stop_sequences",
  "computer_authority_mode",
  "image_model_id",
  "image_aspect_ratio",
  "image_resolution",
  "image_steps",
]);

export const IMAGE_RESOLUTION_PRESETS = Object.freeze([512, 768, 1024]);
export const IMAGE_ASPECT_RATIO_PRESETS = Object.freeze(["1:1", "4:3", "3:4", "16:9", "9:16"]);
export const IMAGE_STEP_PRESETS = Object.freeze([4, 8, 12, 20]);

export function imageCanvasForSettings(resolution, aspectRatio) {
  const edge = IMAGE_RESOLUTION_PRESETS.includes(Number(resolution))
    ? Number(resolution)
    : 512;
  const ratio = IMAGE_ASPECT_RATIO_PRESETS.includes(String(aspectRatio))
    ? String(aspectRatio)
    : "1:1";
  const [x, y] = ratio.split(":").map(Number);
  if (x === y) return { width: edge, height: edge };
  const landscape = x > y;
  const shortEdge = Math.max(
    256,
    Math.round((edge * (landscape ? y / x : x / y)) / 16) * 16,
  );
  return landscape
    ? { width: edge, height: shortEdge }
    : { width: shortEdge, height: edge };
}

export function normaliseGenerationSettingsSnapshot(value, defaults = {}) {
  const supplied = value && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
  const merged = { ...defaults, ...supplied };
  const settings = {};
  for (const key of GENERATION_SETTING_KEYS) {
    if (Object.hasOwn(merged, key)) settings[key] = merged[key];
  }
  settings.context_window_tokens = normaliseContextWindowTokens(
    merged.context_window_tokens,
    defaults.context_window_tokens,
  );
  settings.maximum_output_mode = normaliseMaximumOutputMode(
    merged.maximum_output_mode ?? merged.max_output_mode,
  );
  if (settings.maximum_output_mode === "manual") {
    settings.maximum_output_tokens = normaliseMaximumOutputTokens(
      merged.maximum_output_tokens ?? merged.max_output_tokens,
      defaults.maximum_output_tokens,
    );
  } else {
    const automaticLimit = Number(
      merged.maximum_output_tokens ?? merged.max_output_tokens ?? defaults.maximum_output_tokens,
    );
    settings.maximum_output_tokens = Number.isFinite(automaticLimit) && automaticLimit > 0
      ? Math.round(automaticLimit)
      : 8_192;
  }
  settings.reasoning_mode = normaliseCookingMode(merged.reasoning_mode);
  const reasoningBudget = Number(merged.cooking_reasoning_tokens ?? 1024);
  settings.cooking_reasoning_tokens = Number.isInteger(reasoningBudget) && reasoningBudget >= 256 && reasoningBudget <= 8192 ? reasoningBudget : 1024;
  settings.resource_mode = merged.resource_mode === "turtle" ? "turtle" : "normal";
  settings.reasoning_visibility = normaliseReasoningVisibility(
    merged.reasoning_visibility,
  );
  settings.web_search_enabled = Boolean(merged.web_search_enabled);
  settings.system_prompt = String(merged.system_prompt || "");
  settings.stop_sequences = Array.isArray(merged.stop_sequences)
    ? [...merged.stop_sequences]
    : [];
  settings.computer_authority_mode = normaliseComputerAuthorityMode(
    merged.computer_authority_mode,
  );
  settings.image_model_id = String(
    merged.image_model_id || defaults.image_model_id || "steak-gen-1-scaledfp8",
  );
  settings.image_aspect_ratio = IMAGE_ASPECT_RATIO_PRESETS.includes(
    String(merged.image_aspect_ratio),
  )
    ? String(merged.image_aspect_ratio)
    : "1:1";
  settings.image_resolution = IMAGE_RESOLUTION_PRESETS.includes(
    Number(merged.image_resolution),
  )
    ? Number(merged.image_resolution)
    : 512;
  const imageSteps = Number(merged.image_steps);
  settings.image_steps = Number.isInteger(imageSteps) && imageSteps >= 1 && imageSteps <= 50
    ? imageSteps
    : 8;
  return settings;
}

export function generationSettingsForRequest(value, defaults = {}) {
  const settings = normaliseGenerationSettingsSnapshot(value, defaults);
  return { ...settings, stop_sequences: settings.stop_sequences.filter((sequence) => sequence !== "") };
}

export function generationTurnSettingsForRequest(
  value,
  defaults = {},
  {
    agentMode = false,
    codeMode = false,
    workspaceMode,
    researchMode = false,
    researchCommand = false,
    imageCommand = false,
  } = {},
) {
  const researchEnabled = Boolean(researchMode) || Boolean(researchCommand);
  return {
    ...generationSettingsForRequest(value, defaults),
    agent_mode: Boolean(agentMode),
    code_mode: Boolean(codeMode),
    ...(workspaceMode ? { workspace_mode: workspaceMode } : {}),
    research_available: researchEnabled,
    research_command: researchEnabled,
    image_mode: Boolean(imageCommand),
    web_search_enabled: researchEnabled,
  };
}

export function nextEnabledMenuIndex(currentIndex, direction, enabledItems) {
  const indexes = enabledItems
    .map((enabled, index) => (enabled ? index : -1))
    .filter((index) => index >= 0);
  if (!indexes.length) return -1;
  const currentPosition = indexes.indexOf(currentIndex);
  if (direction === "first") return indexes[0];
  if (direction === "last") return indexes.at(-1);
  if (direction === "previous") {
    return indexes[(currentPosition <= 0 ? indexes.length : currentPosition) - 1];
  }
  return indexes[(currentPosition + 1 + indexes.length) % indexes.length];
}
