export function generationFailureForMessage(message) {
  if (!message || String(message.role || "").toLowerCase() === "assistant") return null;
  const details = message.technical_details || message.details || {};
  if (String(details.generation_state || "").toLowerCase() !== "failed") return null;
  const error = details.generation_error;
  const messageText = typeof error === "object" && error
    ? String(error.message || "").trim()
    : "";
  return {
    type: typeof error === "object" && error
      ? String(error.type || "GenerationError")
      : "GenerationError",
    message: messageText || "Salty Steak could not complete this response.",
  };
}
