export const DEFAULT_POST_TRAINING_POLICY = "evaluate_and_activate";

export const POST_TRAINING_POLICIES = Object.freeze([
  Object.freeze({
    value: "evaluate_and_activate",
    label: "Evaluate and use in Chat",
    description:
      "Save, run validation evaluation, and switch Chat only after exact evidence passes.",
    recommended: true,
  }),
  Object.freeze({
    value: "evaluate",
    label: "Evaluate after training",
    description: "Save and evaluate; keep the current Chat runtime.",
    recommended: false,
  }),
  Object.freeze({
    value: "save_only",
    label: "Save version only",
    description: "Save the verified version without evaluation or activation.",
    recommended: false,
  }),
]);

export const SAVE_PHASES = Object.freeze([
  { value: "queued", label: "Queued" },
  { value: "preparing_data", label: "Preparing data" },
  { value: "loading_model", label: "Loading starting version" },
  { value: "training", label: "Training" },
  { value: "checking_training_loss", label: "Checking training loss" },
  { value: "saving_version", label: "Saving checkpoint" },
  { value: "verifying_version", label: "Verifying checkpoint" },
  { value: "registering_version", label: "Registering version" },
  { value: "applying_retention", label: "Applying retention" },
  { value: "completed", label: "Completed" },
]);

export const EVALUATION_PHASES = Object.freeze([
  ...SAVE_PHASES.slice(0, -2),
  { value: "preparing_evaluation", label: "Preparing evaluation" },
  {
    value: "loading_saved_version_for_evaluation",
    label: "Loading saved version for evaluation",
  },
  {
    value: "evaluating_validation_records",
    label: "Evaluating validation records",
  },
  { value: "finalising_evaluation", label: "Finalising evaluation" },
  { value: "saving_evaluation_report", label: "Saving evaluation report" },
  {
    value: "confirming_evaluation_evidence",
    label: "Confirming evaluation evidence",
  },
  { value: "applying_retention", label: "Applying retention" },
  { value: "completed", label: "Completed" },
]);

export const ACTIVATION_PHASES = Object.freeze([
  ...EVALUATION_PHASES.slice(0, -2),
  { value: "selecting_saved_version", label: "Selecting saved version" },
  {
    value: "activating_evaluated_version",
    label: "Activating evaluated version",
  },
  { value: "preparing_saved_version", label: "Preparing saved version" },
  { value: "creating_runtime", label: "Creating runtime" },
  { value: "verifying_runtime", label: "Verifying runtime" },
  { value: "loading_salty_potato", label: "Loading Chat runtime" },
  { value: "testing_real_generation", label: "Confirming runtime identity" },
  { value: "switching_chat_version", label: "Switching Chat version" },
  { value: "applying_retention", label: "Applying retention" },
  { value: "completed", label: "Completed" },
]);

export function phasesForPolicy(policy) {
  if (policy === "save_only") return SAVE_PHASES;
  if (policy === "evaluate") return EVALUATION_PHASES;
  return ACTIVATION_PHASES;
}

export function trainingCompletionEvidence(operation) {
  const result = operation?.result || {};
  const policy =
    result.post_training_policy ||
    operation?.post_training_policy ||
    DEFAULT_POST_TRAINING_POLICY;
  const completed = new Set([
    "queued",
    "preparing_data",
    "loading_model",
    "training",
    "validating",
    "checking_training_loss",
    "saving_version",
    "verifying_version",
    "registering_version",
    "completed",
  ]);
  if (policy !== "save_only") {
    completed.add("preparing_evaluation");
    completed.add("loading_saved_version_for_evaluation");
    completed.add("evaluating_validation_records");
    completed.add("finalising_evaluation");
    completed.add("saving_evaluation_report");
    completed.add("confirming_evaluation_evidence");
  }
  if (
    policy === "evaluate_and_activate" &&
    result.active_in_chat &&
    result.runtime_identity
  ) {
    completed.add("selecting_saved_version");
    completed.add("activating_evaluated_version");
    completed.add("preparing_saved_version");
    completed.add("creating_runtime");
    completed.add("verifying_runtime");
    completed.add("loading_salty_potato");
    completed.add("testing_real_generation");
    completed.add("switching_chat_version");
  }
  if (result.retention_completed || result.retention?.completed) {
    completed.add("applying_retention");
  }
  return completed;
}
