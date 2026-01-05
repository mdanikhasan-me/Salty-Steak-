export function sameSelectionId(left, right) {
  if (left === null || left === undefined || right === null || right === undefined) {
    return false;
  }
  return String(left) === String(right);
}

export function selectedItemOrFirst(items, selectedId) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return items.find((item) => sameSelectionId(item?.id, selectedId)) || items[0];
}
