// A delayed response must never replace text written after Send, or a draft
// belonging to a different conversation.
export function recoverConversationDraft({ drafts, ownerId, selectedId, current, content, attachments = [] }) {
  const key = ownerId || "new";
  const selected = String(ownerId || "") === String(selectedId || "");
  const previous = (selected ? current : drafts.get(key)) || {};
  if (previous.draft?.trim() || previous.attachments?.length) return null;
  const restored = { ...previous, draft: content, attachments };
  drafts.set(key, restored);
  return selected ? restored : null;
}
