import { isActive } from "./operations.mjs";

// A failed lookup is not evidence that an operation is still healthy.
// A missing persisted ID is different from a temporary service outage.
export async function pollOperations(current, client) {
  const active = Object.values(current || {}).filter(isActive);
  if (!active.length) {
    const payload = await client.listOperations({ active: true });
    return Array.isArray(payload) ? payload : payload?.operations || payload?.active_operations || [];
  }
  const results = await Promise.allSettled(active.map(operation => client.getOperation(operation.id)));
  const incoming = [];
  const failures = [];
  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      const operation = result.value?.operation || result.value;
      if (operation?.id) incoming.push(operation);
    } else if (Number(result.reason?.status) === 404) {
      incoming.push({ ...active[index], state: "interrupted", phase: "interrupted",
        updated_at: new Date().toISOString(),
        error: { message: "The service no longer has this operation. Reopen the conversation to check its last saved response." },
      });
    } else failures.push(result.reason);
  });
  if (!incoming.length && failures.length) throw failures[0];
  return incoming;
}
