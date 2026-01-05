import { operationMatches } from "../../workflows/operations.mjs";

export function latestMatchingOperation(operations, type, targetId) {
  return Object.values(operations)
    .filter((operation) => operationMatches(operation, type, targetId))
    .sort((a, b) => Date.parse(b.updated_at || 0) - Date.parse(a.updated_at || 0))[0];
}
