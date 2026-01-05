export function latestRetryableUserMessageId(messages) {
  const list = Array.isArray(messages) ? messages : [];
  let latestUserIndex = -1;
  for (let index = list.length - 1; index >= 0; index -= 1) {
    if (list[index]?.role === "user") {
      latestUserIndex = index;
      break;
    }
  }
  if (latestUserIndex < 0) return null;
  const latestUser = list[latestUserIndex];
  if (latestUser?.technical_details?.host_action) return null;
  const hasResponse = list
    .slice(latestUserIndex + 1)
    .some((message) => message?.role === "assistant" && !message?.pending);
  return hasResponse ? latestUser.id || null : null;
}

export function createSerialGenerationExecutor() {
  let tail = Promise.resolve();
  return (task) => {
    const result = tail.then(task, task);
    tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  };
}

export function ownsGenerationTask(currentTaskId, candidateTaskId) {
  return Number(currentTaskId) === Number(candidateTaskId);
}

const CHAT_GENERATION_TYPES = new Set([
  "chat_generation",
  "chat_image_generation",
]);

export function isImageGenerationOperation(operation) {
  return String(operation?.type || "").trim().toLowerCase() === "chat_image_generation";
}

export function isChatGenerationOperation(operation) {
  return CHAT_GENERATION_TYPES.has(
    String(operation?.type || "").trim().toLowerCase(),
  );
}

export function chatGenerationConversationId(operation) {
  if (!isChatGenerationOperation(operation)) {
    return null;
  }
  const value = operation?.result?.conversation_id
    ?? operation?.details?.conversation_id
    ?? operation?.conversation_id
    ?? operation?.target_id;
  return value === null || value === undefined || String(value).trim() === ""
    ? null
    : String(value);
}

export function ownsConversationGeneration(operation, conversationId) {
  const ownerId = chatGenerationConversationId(operation);
  return ownerId !== null
    && conversationId !== null
    && conversationId !== undefined
    && ownerId === String(conversationId);
}

export function imageGenerationProposalId(operation) {
  if (!isImageGenerationOperation(operation)) return null;
  const value = operation?.result?.proposal_id
    ?? operation?.details?.proposal_id
    ?? operation?.proposal_id;
  return value === null || value === undefined || String(value).trim() === ""
    ? null
    : String(value);
}

export function imageGenerationMessageId(operation) {
  if (!isImageGenerationOperation(operation)) return null;
  const value = operation?.result?.assistant_message_id
    ?? operation?.details?.assistant_message_id
    ?? operation?.assistant_message_id;
  return value === null || value === undefined || String(value).trim() === ""
    ? null
    : String(value);
}

export function ownsImageProposalGeneration(
  operation,
  conversationId,
  proposalId,
  assistantMessageId = null,
) {
  if (!isImageGenerationOperation(operation)) return false;
  if (!ownsConversationGeneration(operation, conversationId)) return false;
  const operationProposalId = imageGenerationProposalId(operation);
  const operationMessageId = imageGenerationMessageId(operation);
  if (operationProposalId && proposalId) {
    return operationProposalId === String(proposalId);
  }
  return Boolean(
    operationMessageId
      && assistantMessageId
      && operationMessageId === String(assistantMessageId),
  );
}

const ACTIVE_GENERATION_STATES = new Set([
  "queued",
  "running",
  "stop_requested",
]);

export function shouldRenderConversationGeneration(
  operation,
  conversationId,
  sending,
  messages = [],
) {


  if (isImageGenerationOperation(operation)) return false;
  if (!sending || !ownsConversationGeneration(operation, conversationId)) {
    return false;
  }
  if (!ACTIVE_GENERATION_STATES.has(
    String(operation?.state || "").trim().toLowerCase(),
  )) {
    return false;
  }
  const durableAssistantId = operation?.result?.assistant_message_id;
  return !durableAssistantId || !(Array.isArray(messages) ? messages : []).some(
    (message) =>
      message?.role === "assistant" &&
      String(message?.id) === String(durableAssistantId),
  );
}

export function visibleConversationForSelection(conversation, conversationId) {
  if (
    !conversation ||
    conversationId === null ||
    conversationId === undefined ||
    String(conversation?.id) !== String(conversationId)
  ) {
    return null;
  }
  return conversation;
}

export function mergeFetchedConversationWithPending(
  fetchedConversation,
  currentConversation,
) {
  if (!fetchedConversation) return fetchedConversation;
  if (
    !currentConversation ||
    String(currentConversation?.id) !== String(fetchedConversation?.id)
  ) {
    return fetchedConversation;
  }
  const fetchedMessages = Array.isArray(fetchedConversation.messages)
    ? fetchedConversation.messages
    : [];
  const pendingMessages = (
    Array.isArray(currentConversation.messages) ? currentConversation.messages : []
  ).filter(
    (pending) =>
      pending?.pending === true &&
      !fetchedMessages.some(
        (message) =>
          message?.role === pending?.role &&
          String(message?.content || "") === String(pending?.content || ""),
      ),
  );
  return pendingMessages.length
    ? { ...fetchedConversation, messages: [...fetchedMessages, ...pendingMessages] }
    : fetchedConversation;
}

export function canStartChatSubmission({ sending, content }) {
  return !sending && Boolean(String(content || "").trim());
}
