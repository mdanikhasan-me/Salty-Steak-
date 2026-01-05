const DEFAULT_TTL = 10_000;

export function addNotification(items, notification, now = Date.now(), ttl = DEFAULT_TTL) {
  if (!notification?.id || !notification?.message) return items;
  if (items.some((item) => item.id === notification.id)) return items;
  return [
    ...items,
    {
      ...notification,
      createdAt: now,
      expiresAt: notification.persistent ? null : now + ttl,
    },
  ];
}

export function removeNotification(items, id) {
  return items.filter((item) => item.id !== id);
}

export function expireNotifications(items, now = Date.now()) {
  return items.filter((item) => item.expiresAt === null || item.pausedAt || item.expiresAt > now);
}

export function pauseNotification(items, id, now = Date.now()) {
  return items.map((item) => item.id === id && item.expiresAt !== null && !item.pausedAt
    ? { ...item, pausedAt: now }
    : item);
}

export function resumeNotification(items, id, now = Date.now()) {
  return items.map((item) => {
    if (item.id !== id || !item.pausedAt || item.expiresAt === null) return item;
    return { ...item, expiresAt: item.expiresAt + (now - item.pausedAt), pausedAt: null };
  });
}

export function notificationVisibleOnPage(notification, page) {
  return !notification?.page || notification.page === page;
}

export function notificationReducer(state, action) {
  switch (action.type) {
    case "add":
      return addNotification(state, action.notification, action.now, action.ttl);
    case "remove":
      return removeNotification(state, action.id);
    case "expire":
      return expireNotifications(state, action.now);
    case "pause":
      return pauseNotification(state, action.id, action.now);
    case "resume":
      return resumeNotification(state, action.id, action.now);
    case "clear":
      return [];
    default:
      return state;
  }
}
