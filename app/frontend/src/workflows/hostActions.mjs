export const HOST_ACTION_SCHEMA = "salty-steak-host-action-proposal-v1";
export const TEMP_CLEANUP_CONFIRMATION = "CLEAN_WINDOWS_TEMPORARY_FILES";

const ACTION_KINDS = new Set([
  "image.generate",
  "terminal.execute",
  "filesystem.trash_file",
  "system.clean_temp",
]);

export function hostActionProposalForMessage(message) {
  if (!message || message.role !== "assistant") return null;
  const details = message.technical_details || message.details || {};
  const durable = normaliseProposal(details.host_action_proposal, false);
  if (durable) return durable;
  return legacyImageProposal(message);
}

export function hostSafeAssistantContent(message, proposal = null) {
  const content = String(message?.content || "");
  if (proposal?.legacy === true) return "";
  if (looksLikeHistoricalUnixDeletionAdvice(content)) {
    return (
      "This historical response contained Unix deletion commands, so Salty Steak "
      + "withheld them in the Windows app. Prepare a reviewed Windows action instead."
    );
  }
  return content;
}

function normaliseProposal(value, legacy) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (value.schema !== HOST_ACTION_SCHEMA || !ACTION_KINDS.has(value.kind)) return null;
  const state = String(value.state || "");
  const summary = String(value.summary || "").trim();
  if (!state || !summary || summary.length > 4_000) return null;
  const argumentsValue = value.arguments;
  if (
    argumentsValue !== null
    && (typeof argumentsValue !== "object" || Array.isArray(argumentsValue))
  ) return null;
  return Object.freeze({
    schema: HOST_ACTION_SCHEMA,
    id: String(value.id || ""),
    kind: value.kind,
    title: String(value.title || defaultTitle(value.kind)),
    summary,
    state,
    risk: String(value.risk || "standard"),
    arguments: argumentsValue ? structuredCloneSafe(argumentsValue) : null,
    runtimeReason: value.runtime_reason ? String(value.runtime_reason) : "",
    requiresConfirmation: value.requires_confirmation !== false,
    executionAllowed: value.execution_allowed === true,
    legacy,
  });
}

function legacyImageProposal(message) {
  const raw = String(message.content || "").trim();
  if (!raw.startsWith("{") || !raw.endsWith("}")) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const keys = Object.keys(parsed).sort();
  if (keys.join(",") !== "action,prompt" || parsed.action !== "generate_image") return null;
  const prompt = String(parsed.prompt || "").trim();
  if (!prompt || prompt.length > 4_000) return null;
  return normaliseProposal({
    schema: HOST_ACTION_SCHEMA,
    id: `legacy-${String(message.id || "image")}`,
    kind: "image.generate",
    title: "Create an image",
    summary: prompt,
    arguments: { prompt },
    state: "blocked_runtime_unavailable",
    runtime_reason: (
      "Steak gen 1 ScaledFP8 is registered, but its complete local generation "
      + "pipeline has not passed validation."
    ),
    requires_confirmation: true,
    execution_allowed: false,
  }, true);
}

function looksLikeHistoricalUnixDeletionAdvice(content) {
  const lowered = content.toLowerCase();
  return (
    (lowered.includes("sudo rm -rf /tmp") || lowered.includes("rm /path/to/"))
    && lowered.includes("/tmp")
  );
}

function defaultTitle(kind) {
  if (kind === "image.generate") return "Create an image";
  if (kind === "filesystem.trash_file") return "Move a file to Recycle Bin";
  if (kind === "system.clean_temp") return "Clean Windows temporary files";
  return "Review terminal command";
}

function structuredCloneSafe(value) {
  return JSON.parse(JSON.stringify(value));
}
