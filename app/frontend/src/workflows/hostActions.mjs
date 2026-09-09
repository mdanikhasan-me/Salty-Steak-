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

export function hostSafeAssistantContent(message, proposal = null, operation = null) {
  const content = String(message?.content || "");
  // Cleanup has one live status surface; stale planning text must not contradict it.
  if (proposal?.kind === "system.clean_temp") return "";
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
    result: value.result && typeof value.result === "object" ? structuredCloneSafe(value.result) : null,
    generation_settings: value.generation_settings && typeof value.generation_settings === "object" ? structuredCloneSafe(value.generation_settings) : undefined,
    image_settings: value.image_settings && typeof value.image_settings === "object" ? structuredCloneSafe(value.image_settings) : undefined,
    requiresConfirmation: value.requires_confirmation !== false,
    executionAllowed: value.execution_allowed === true,
    legacy,
  });
}

export function cleanupPresentation(proposal, operation = null) {
  const state = String(operation?.state || proposal?.state || "pending_review").toLowerCase();
  const result = operation?.result?.result || proposal?.result || null;
  if (["queued", "running", "stop_requested"].includes(state)) return {
    label: state === "stop_requested" ? "Stopping cleanup" : "Cleaning temporary files",
    summary: state === "stop_requested" ? "Stop requested. Waiting for the cleanup worker to finish." : "Working through the reviewed folders. Files in use and inaccessible entries are skipped.",
    roots: [],
  };
  if (state === "completed" && result && Number.isFinite(result.deleted_files)) {
    const skipped = Number(result.skipped_entries) || 0;
    const count = new Intl.NumberFormat();
    const bytes = Number(result.reclaimed_bytes) || 0;
    const size = bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GiB` : `${(bytes / 1048576).toFixed(1)} MiB`;
    return {
      label: skipped ? "Cleanup finished with skipped items" : "Cleanup finished",
      summary: `${count.format(result.deleted_files)} files removed · ${size} reclaimed${skipped ? ` · ${count.format(skipped)} entries skipped` : ""}.`,
      roots: Array.isArray(result.roots) ? result.roots : [],
    };
  }
  if (state === "completed") return {label:"Cleanup result unavailable",summary:"The operation ended, but its removal counts could not be verified.",roots:[]};
  if (["failed", "interrupted", "cancelled"].includes(state)) {
    const count = Number.isFinite(result?.deleted_files) ? `${new Intl.NumberFormat().format(result.deleted_files)} files were recorded as removed before the operation ended.` : "Removal counts were not recorded. Some files may have been removed.";
    const error = typeof operation?.error === "string" ? operation.error : operation?.error?.message;
    return { label: state === "failed" ? "Cleanup failed" : "Cleanup stopped", summary: [error, count].filter(Boolean).join(" "), roots: Array.isArray(result?.roots) ? result.roots : [] };
  }
  return {label:"Temporary file cleanup",summary:"Review the folders before permanently removing their contents.",roots:[]};
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
