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
    label: "Instant",
    controlLabel: "Instant",
    description: "Direct answers; with Research on, run focused research for up to five minutes.",
  }),
  Object.freeze({
    id: "cooking",
    controlLabel: "Cooking",
    label: "Cooking",
    description: "Deliberate answers; with Research on, validate iteratively with a hard four-hour ceiling.",
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
]);

export function toggleComposerMenu(currentMenu, requestedMenu) {
  if (!COMPOSER_MENU_IDS.includes(requestedMenu)) return null;
  return currentMenu === requestedMenu ? null : requestedMenu;
}

export function normaliseCookingMode(value) {
  const candidate = String(value || "").trim().toLowerCase();
  if (COOKING_MODES.some((mode) => mode.id === candidate)) return candidate;
  if (candidate === "off") return "instant";
  if (candidate === "auto" || candidate === "deep") return "cooking";
  return "instant";
}

export function cookingModeLabel(value) {
  const selected = COOKING_MODES.find((mode) => mode.id === normaliseCookingMode(value));
  return selected?.controlLabel || "Instant";
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
  "reasoning_visibility",
  "web_search_enabled",
  "system_prompt",
  "stop_sequences",
  "computer_authority_mode",
]);

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
  return settings;
}

export function generationSettingsForRequest(value, defaults = {}) {
  return normaliseGenerationSettingsSnapshot(value, defaults);
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
