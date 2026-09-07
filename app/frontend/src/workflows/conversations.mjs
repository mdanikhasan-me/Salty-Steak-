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
  const dayKey = (date) => Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
  const today = dayKey(current);
  const groups = new Map();
  const dateLabels = new Map();
  for (const conversation of conversations || []) {
    const stamp = conversation?.updated_at || conversation?.created_at;
    const date = stamp ? new Date(stamp) : new Date(NaN);
    const valid = Number.isFinite(date.getTime());
    const day = valid ? dayKey(date) : -Infinity;
    const age = today - day;
    let label = dateLabels.get(day);
    if (!label) {
      label = !valid ? "Date unavailable" : age === 0 ? "Today" : age === 86400000 ? "Yesterday"
        : date.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(date.getFullYear() !== current.getFullYear() ? { year: "numeric" } : {}) });
      dateLabels.set(day, label);
    }
    if (!groups.has(day)) groups.set(day, { label, items: [] });
    groups.get(day).items.push(conversation);
  }
  return [...groups.entries()].sort(([left],[right]) => right-left).map(([,group]) => ({
    ...group, items: group.items.sort((left,right) => new Date(right.updated_at || right.created_at).getTime() - new Date(left.updated_at || left.created_at).getTime()),
  }));
}
