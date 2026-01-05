export function synchronizeConversationSelection(reference, commit, value) {
  const selectedId = value === null || value === undefined ? null : value;
  reference.current = selectedId;
  commit(selectedId);
  return selectedId;
}

export function conversationSelectionExists(conversations, selectedId) {
  if (selectedId === null || selectedId === undefined) return false;
  return (conversations || []).some(
    (conversation) => String(conversation?.id) === String(selectedId),
  );
}

export function conversationStateAfterDelete(conversations, deletedId, selectedId) {
  const source = conversations || [];
  const deletedIndex = source.findIndex(
    (conversation) => String(conversation?.id) === String(deletedId),
  );
  if (deletedIndex < 0) {
    return {
      conversations: source,
      selectedId,
      selectedWasDeleted: false,
    };
  }
  const remaining = source.filter(
    (conversation) => String(conversation?.id) !== String(deletedId),
  );
  const deletedWasSelected = String(selectedId) === String(deletedId);
  const nextIndex = Math.min(Math.max(deletedIndex, 0), Math.max(remaining.length - 1, 0));
  return {
    conversations: remaining,
    selectedId: deletedWasSelected ? remaining[nextIndex]?.id ?? null : selectedId,
    selectedWasDeleted: deletedWasSelected,
  };
}

export function validConversationTitle(value) {
  const title = String(value || "").trim();
  const containsControlCharacter = [...title].some((character) => {
    const codePoint = character.codePointAt(0);
    return codePoint < 32 || codePoint === 127;
  });
  return title.length > 0 && title.length <= 80 && !containsControlCharacter
    ? title
    : null;
}

export function groupConversationsByRecency(conversations, now = new Date()) {
  const current = now instanceof Date ? now : new Date(now);
  const today = new Date(
    current.getFullYear(),
    current.getMonth(),
    current.getDate(),
  ).getTime();
  const groups = {
    Today: [],
    Yesterday: [],
    Older: [],
  };
  for (const conversation of conversations || []) {
    const timestamp = new Date(
      conversation?.updated_at || conversation?.created_at || 0,
    ).getTime();
    const age = today - timestamp;
    const label = Number.isFinite(timestamp) && age < 24 * 60 * 60 * 1000
      ? "Today"
      : Number.isFinite(timestamp) && age < 48 * 60 * 60 * 1000
        ? "Yesterday"
        : "Older";
    groups[label].push(conversation);
  }
  return ["Today", "Yesterday", "Older"]
    .filter((label) => groups[label].length)
    .map((label) => ({ label, items: groups[label] }));
}
