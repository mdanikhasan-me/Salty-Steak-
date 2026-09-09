import { normaliseToken, operationState, phaseLabel } from "./operations.mjs";

const NAMES = {
  training: "Training", training_identity: "Identity training",
  dataset_validation: "Dataset validation", dataset_preparation: "Dataset preparation",
  evaluation: "Evaluation", project_verification: "Installation verification",
  chat_image_generation: "Image generation", chat_host_action_execution: "Action",
};

export function operationPresentation(operation) {
  const type = normaliseToken(operation?.type);
  const training = type === "training" || type === "training_identity";
  const name = NAMES[type] || "Operation";
  const state = operationState(operation);
  const completedMeasuredWork = training && state === "failed"
    && Number(operation.current_progress) > 0
    && operation.total_progress != null
    && Number(operation.current_progress) >= Number(operation.total_progress);
  const recovered = operation?.outcome === "recovered_and_completed" || Boolean(operation?.result?.recovered_after_restart);
  const needsAttention = operation?.outcome === "needs_attention" || operation?.result?.completion_outcome === "needs_attention";
  const outcomeTitle = recovered ? "Recovered and completed"
    : needsAttention ? (training ? "Saved version needs attention" : `${name} needs attention`)
    : completedMeasuredWork ? "Training steps completed; version finalisation failed"
    : state === "failed" ? `${name} did not complete`
    : state === "cancelled" ? `${name} cancelled`
    : state === "interrupted" ? `${name} interrupted`
    : state === "completed" ? `${name} completed`
    : phaseLabel(operation);
  const errorTitle = operation?.error?.failed_phase
    || (training ? (needsAttention ? "Post-training workflow needs attention" : "Version finalisation") : `${name} failed`);
  return { training, completedMeasuredWork, recovered, needsAttention, outcomeTitle, errorTitle };
}
