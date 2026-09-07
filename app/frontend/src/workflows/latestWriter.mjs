// Serialize durable settings updates and coalesce edits waiting behind a slow
// write. The newest value wins per field without losing unrelated preferences.
export function createLatestWriter(write) {
  let running = false, pending = null, waiters = [];
  async function flush() {
    if (running || !pending) return;
    running = true;
    while (pending) {
      const value = pending, listeners = waiters;
      pending = null; waiters = [];
      try { const result = await write(value); listeners.forEach(item => item.resolve(result)); }
      catch (error) { listeners.forEach(item => item.reject(error)); }
    }
    running = false;
  }
  return value => new Promise((resolve, reject) => {
    pending = { ...pending, ...value }; waiters.push({ resolve, reject });
    void flush();
  });
}
