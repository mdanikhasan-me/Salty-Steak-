// Bounded in-memory views make back-and-forth navigation immediate. Every read
// still revalidates from the service; another conversation is never a fallback.
export function createConversationCache(limit = 8) {
  const views = new Map();
  return {
    get(id) {
      if (!id) return null;
      const value = views.get(String(id));
      if (value) { views.delete(String(id)); views.set(String(id), value); }
      return value || null;
    },
    set(value) {
      if (!value?.id) return;
      const id = String(value.id); views.delete(id); views.set(id, value);
      while (views.size > limit) views.delete(views.keys().next().value);
    },
    delete(id) { views.delete(String(id)); },
  };
}
