export function drawerReducer(open, action) {
  switch (action?.type) {
    case "open":
      return true;
    case "toggle":
      return !open;
    case "close":
      return false;
    case "navigate":
    case "page_changed":
      return open;
    default:
      return open;
  }
}
