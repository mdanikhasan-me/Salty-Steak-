export const TERMINAL_STATES = new Set(["completed", "interrupted", "failed", "cancelled"]);
export const ACTIVE_STATES = new Set(["queued", "running", "stop_requested"]);

const PHASE_LABELS = {
  queued: "Queued",
  waiting: "Waiting",
  reading_data: "Reading data",
  creating_splits: "Creating splits",
  tokenising: "Tokenising",
  packing: "Packing",
  saving_prepared_data: "Saving prepared data",
  verifying: "Verifying",
  ready: "Ready",
  preparing_data: "Preparing data",
  loading_model: "Loading model",
  training: "Training",
  training_identity: "Identity post-training",
  validating: "Checking training loss",
  checking_training_loss: "Checking training loss",
  saving_version: "Saving version",
  verifying_version: "Verifying version",
  registering_version: "Registering version",
  activating_completed_version: "Activating evaluated version",
  preparing_evaluation: "Preparing evaluation",
  loading_saved_version_for_evaluation: "Loading saved version for evaluation",
  evaluating_validation_records: "Evaluating validation records",
  finalising_evaluation: "Finalising evaluation",
  saving_evaluation_report: "Saving evaluation report",
  confirming_evaluation_evidence: "Confirming evaluation evidence",
  evaluated: "Evaluation completed",
  selecting_saved_version: "Selecting saved version",
  activating_evaluated_version: "Activating evaluated version",
  applying_retention: "Applying retention",
  finalisation_requires_attention: "Finalisation requires attention",
  recovered_and_completed: "Recovered and completed",
  stop_requested: "Stop requested",
  finishing: "Finishing",
  completed: "Completed",
  interrupted: "Interrupted",
  failed: "Failed",
  preparing_evaluation_data: "Preparing evaluation data",
  loading_saved_version: "Loading saved version",
  evaluating_records: "Evaluating records",
  finalising: "Finalising",
  saving_report: "Saving report",
  preparing_saved_version: "Preparing saved version",
  creating_runtime: "Creating runtime",
  verifying_runtime: "Verifying runtime",
  loading_salty_potato: "Loading Salty Steak",
  testing_real_generation: "Testing real generation",
  switching_chat_version: "Switching chat version",
  validating_image_pipeline: "Checking image model",
  preparing_image_model: "Preparing image model",
  switching_from_chat: "Freeing memory for image generation",
  switching_from_chat_to_image_generation: "Freeing memory for image generation",
  verifying_bundle: "Verifying local image files",
  bundle_verified: "Local image files verified",
  loading_text_encoder: "Loading prompt encoder",
  encoding_prompt: "Understanding the image prompt",
  text_encoder_unloaded: "Prompt encoded",
  loading_image_model: "Loading image model",
  loading_transformer: "Loading image model",
  transformer_ready: "Image model ready",
  denoising: "Generating image",
  decoding_image: "Rendering image",
  saving_image: "Saving image",
  restoring_chat: "Restoring Chat",
  restoring_chat_after_image_generation: "Restoring Chat",
  checking_status: "Checking status",
};

export function normaliseToken(value = "") {
  return String(value)
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_")
    .replace(/[^a-z0-9_]/g, "");
}

export function operationState(operation) {
  return normaliseToken(operation?.state || "queued");
}

export function isTerminal(operation) {
  return TERMINAL_STATES.has(operationState(operation));
}

export function isActive(operation) {
  return Boolean(operation) && ACTIVE_STATES.has(operationState(operation));
}

export function phaseLabel(operation) {
  const token = normaliseToken(operation?.phase || operation?.state || "waiting");
  if (PHASE_LABELS[token]) return PHASE_LABELS[token];
  return token
    .split("_")
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}

export function measurableProgress(operation) {
  const current = Number(
    operation?.current_progress ?? operation?.progress?.current ?? operation?.current,
  );
  const total = Number(
    operation?.total_progress ?? operation?.progress?.total ?? operation?.total,
  );
  if (!Number.isFinite(current) || !Number.isFinite(total) || total <= 0) {
    return null;
  }
  return {
    current: Math.max(0, current),
    total,
    percentage: Math.min(100, Math.max(0, (current / total) * 100)),
  };
}

export function mergeOperations(previous, incoming) {
  const next = { ...previous };
  for (const operation of incoming || []) {
    if (!operation?.id) continue;
    const existing = next[operation.id];
    const existingTime = Date.parse(existing?.updated_at || existing?.updatedAt || 0);
    const incomingTime = Date.parse(operation.updated_at || operation.updatedAt || 0);
    if (!existing || !Number.isFinite(existingTime) || !Number.isFinite(incomingTime) || incomingTime >= existingTime) {
      next[operation.id] = { ...existing, ...operation };
    }
  }
  return next;
}

export function nextPollDelay(previousDelay, operations, options = {}) {
  const initial = options.initial ?? 750;
  const maximum = options.maximum ?? 5000;
  const hasActive = Object.values(operations || {}).some(isActive);
  if (hasActive) return initial;
  return Math.min(maximum, Math.max(initial, (previousDelay || initial) * 1.6));
}

export function notificationForTransition(previous, current) {
  if (!previous || !current?.id) return null;
  const from = operationState(previous);
  const to = operationState(current);
  if (from === to) return null;

  const explicit = current?.result?.notification || current?.notification;
  if (to === "completed") {
    return {
      id: `${current.id}:completed`,
      kind: "success",
      message: explicit || completedMessage(current.type),
      operationId: current.id,
      page: pageForOperation(current.type),
    };
  }
  if (to === "failed") {
    if (
      normaliseToken(current.type) === "training" &&
      current.outcome !== "needs_attention"
    ) return null;
    const chatFailure = normaliseToken(current.type) === "chat_generation";
    return {
      id: `${current.id}:failed`,
      kind: "error",
      message: current?.error?.message || current?.error || `${operationName(current.type)} failed.`,
      operationId: current.id,
      page: pageForOperation(current.type),
      persistent: !chatFailure,
    };
  }
  if (to === "interrupted") {
    return {
      id: `${current.id}:interrupted`,
      kind: "information",
      message: `${operationName(current.type)} stopped safely.`,
      operationId: current.id,
      page: pageForOperation(current.type),
    };
  }
  return null;
}

export function trainingHeaderState(operation) {
  if (!operation) {
    return { value: "ready", label: "Ready to set up" };
  }
  const state = operationState(operation);
  if (ACTIVE_STATES.has(state)) {
    return { value: state, label: "Training in progress" };
  }
  if (state === "failed") {
    return {
      value: operation?.outcome === "needs_attention" ? "warning" : "failed",
      label:
        operation?.outcome === "needs_attention"
          ? "Needs attention"
          : "Training failed",
    };
  }
  if (state === "interrupted") {
    return { value: "interrupted", label: "Training stopped" };
  }
  return { value: "ready", label: "Ready to set up" };
}

export function latestTrainingOperation(operations, persistedStatus) {
  return [...Object.values(operations || {}), persistedStatus]
    .filter(Boolean)
    .filter((operation) => normaliseToken(operation.type) === "training")
    .sort(
      (left, right) =>
        Date.parse(right.updated_at || right.updatedAt || 0) -
        Date.parse(left.updated_at || left.updatedAt || 0),
    )[0];
}

export function pageForOperation(type = "") {
  const token = normaliseToken(type);
  if (token.startsWith("dataset_")) return "data";
  if (token.startsWith("training")) return "train";
  if (token === "evaluation" || token === "evaluation_activation") return "evaluate";
  if (["runtime_creation", "activation", "version_activation"].includes(token)) {
    return "chat";
  }
  if (token === "chat_image_generation") return "chat";
  if (token === "chat_generation" || token === "chat_host_action_execution") return "chat";
  if (["deletion", "version_deletion"].includes(token)) return "versions";
  if (["project_verification", "cache_clear"].includes(token)) return "project";
  return null;
}

export function operationName(type = "operation") {
  const names = {
    dataset_validation: "Dataset validation",
    dataset_preparation: "Dataset preparation",
    training: "Training",
    training_finalisation_retry: "Finalisation retry",
    evaluation: "Evaluation",
    evaluation_activation: "Evaluation and activation",
    post_training_recovery: "Post-training recovery",
    runtime_creation: "Runtime preparation",
    activation: "Chat version switch",
    version_activation: "Chat version switch",
    chat_image_generation: "Image generation",
    deletion: "Version deletion",
    version_deletion: "Version deletion",
    project_verification: "File verification",
    cache_clear: "Cache clearing",
  };
  const token = normaliseToken(type);
  return names[token] || phaseLabel({ phase: token });
}

function completedMessage(type) {
  const messages = {
    dataset_validation: "Dataset validation completed.",
    dataset_preparation: "Dataset is ready for training.",
    training: "Training completed and the saved version was verified.",
    training_identity: "Learned identity passed its native acceptance gates.",
    training_finalisation_retry: "Finalisation was recovered and registered.",
    evaluation: "Evaluation completed and its report was saved.",
    evaluation_activation: "Evaluation passed and Chat was switched.",
    post_training_recovery: "Post-training workflow recovered.",
    runtime_creation: "Salty Steak is ready in chat.",
    activation: "Salty Steak is ready in chat.",
    version_activation: "Salty Steak is ready in chat.",
    chat_image_generation: "Image created.",
    deletion: "Saved version deleted.",
    version_deletion: "Saved version deleted.",
    project_verification: "Project files were verified.",
    cache_clear: "Disposable cache cleared.",
  };
  return messages[normaliseToken(type)] || `${operationName(type)} completed.`;
}

export function domainForOperation(type = "") {
  const token = normaliseToken(type);
  if (token.startsWith("dataset_")) return "datasets";
  if (token.startsWith("training")) return "training";
  if (token === "evaluation" || token === "evaluation_activation") return "evaluations";
  if (["runtime_creation", "activation", "version_activation"].includes(token)) {
    return "chat";
  }
  if (token === "chat_image_generation") return "chat";
  if (["deletion", "version_deletion"].includes(token)) return "versions";
  if (["project_verification", "cache_clear"].includes(token)) return "project";
  return null;
}

export function operationMatches(operation, type, targetId) {
  return (
    normaliseToken(operation?.type) === normaliseToken(type) &&
    (!targetId || String(operation?.target_id || operation?.targetId) === String(targetId))
  );
}
