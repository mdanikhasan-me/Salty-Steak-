
















import { groupConversationsByRecency } from "./conversations.mjs";


export function folderOf(conversation) {
  const first = (conversation?.labels || [])[0];
  return first?.id ? { id: String(first.id), name: String(first.name || "") } : null;
}


export function matchesQuery(conversation, query) {
  const needle = String(query || "").trim().toLowerCase();
  if (!needle) return true;
  if (String(conversation?.title || "").toLowerCase().includes(needle)) return true;



  return String(folderOf(conversation)?.name || "")
    .toLowerCase()
    .includes(needle);
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
  folders = [],
  query = "",
  openFolderId = null,
  now = new Date(),
} = {}) {
  const folderId = openFolderId ? String(openFolderId) : null;
  const visible = conversations.filter((conversation) => {
    if (!matchesQuery(conversation, query)) return false;
    if (!folderId) return true;
    return folderOf(conversation)?.id === folderId;
  });

  const pinned = visible
    .filter(isPinned)
    .sort((left, right) => pinnedOrder(right) - pinnedOrder(left));
  const recents = visible.filter((conversation) => !isPinned(conversation));

  return {
    pinned,
    groups: groupConversationsByRecency(recents, now),
    folders: folders
      .map((item) => ({
        id: String(item?.id || ""),
        name: String(item?.name || ""),
        count: Number(item?.conversation_count) || 0,
      }))
      .filter((item) => item.id && item.name),
    openFolderId: folderId,
    total: conversations.length,
    visibleCount: visible.length,


    filtered: Boolean(folderId) || Boolean(String(query || "").trim()),
  };
}


export function openFolder(organised) {
  return (
    (organised?.folders || []).find(
      (item) => item.id === organised?.openFolderId,
    ) || null
  );
}





export function moveChoicesFor(conversation, folders) {
  const current = folderOf(conversation)?.id || null;
  return (folders || [])
    .map((item) => ({
      id: String(item?.id || ""),
      name: String(item?.name || ""),
      current: String(item?.id) === current,
    }))
    .filter((item) => item.id && item.name);
}


export function conversationTitle(conversation) {
  return String(conversation?.title || "").trim() || "New chat";
}
