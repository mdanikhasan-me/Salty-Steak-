










import { groupConversationsByRecency } from "./conversations.mjs";


export function matchesQuery(conversation, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return true;
  const title = String(conversation?.title || "").toLowerCase();
  if (title.includes(needle)) return true;



  return (conversation?.labels || []).some((label) =>
    String(label?.name || "").toLowerCase().includes(needle),
  );
}

export function isPinned(conversation) {
  return Boolean(conversation?.pinned ?? conversation?.pinned_at);
}

function pinnedOrder(conversation) {
  const stamp = Date.parse(
    conversation?.pinned_at || conversation?.updated_at || 0,
  );
  return Number.isFinite(stamp) ? stamp : 0;
}







export function organiseConversations({
  conversations = [],
  labels = [],
  query = "",
  activeLabelId = null,
  now = new Date(),
} = {}) {
  const label = activeLabelId ? String(activeLabelId) : null;
  const visible = conversations.filter((conversation) => {
    if (!matchesQuery(conversation, query)) return false;
    if (!label) return true;
    return (conversation?.labels || []).some(
      (applied) => String(applied?.id) === label,
    );
  });

  const pinned = visible
    .filter(isPinned)
    .sort((left, right) => pinnedOrder(right) - pinnedOrder(left));
  const recents = visible.filter((conversation) => !isPinned(conversation));

  return {
    pinned,
    groups: groupConversationsByRecency(recents, now),
    labels: labels
      .map((item) => ({
        id: String(item?.id || ""),
        name: String(item?.name || ""),
        tone: String(item?.tone || "neutral"),
        count: Number(item?.conversation_count) || 0,
      }))
      .filter((item) => item.id && item.name),
    activeLabelId: label,
    total: conversations.length,
    visibleCount: visible.length,


    filtered: Boolean(label) || Boolean(String(query || "").trim()),
  };
}


export function activeLabel(organised) {
  return (
    (organised?.labels || []).find(
      (item) => item.id === organised?.activeLabelId,
    ) || null
  );
}





export function labelChoicesFor(conversation, labels) {
  const applied = new Set(
    (conversation?.labels || []).map((item) => String(item?.id)),
  );
  return (labels || [])
    .map((item) => ({
      id: String(item?.id || ""),
      name: String(item?.name || ""),
      tone: String(item?.tone || "neutral"),
      applied: applied.has(String(item?.id)),
    }))
    .filter((item) => item.id && item.name);
}


export function conversationTitle(conversation) {
  return String(conversation?.title || "").trim() || "New chat";
}
