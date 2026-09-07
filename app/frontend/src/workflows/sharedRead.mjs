// Concurrent consumers share one read. Brief operation snapshots prevent the
// global status observer and active transcript from polling the same ID twice.
export function createSharedRead(read, { freshFor = 0, maximum = 64, now = Date.now } = {}) {
  const cache = new Map();
  function get(key) {
    key = String(key);
    const existing = cache.get(key);
    if (existing && (existing.pending || now() - existing.time < freshFor)) return existing.promise;
    const entry = { pending: true, time: 0 };
    entry.promise = Promise.resolve().then(() => read(key)).then(value => {
      entry.pending = false; entry.time = now();
      if (!freshFor && cache.get(key) === entry) cache.delete(key);
      return value;
    }, error => {
      if (cache.get(key) === entry) cache.delete(key);
      throw error;
    });
    cache.delete(key); cache.set(key, entry);
    while (cache.size > maximum) cache.delete(cache.keys().next().value);
    return entry.promise;
  }
  get.invalidate = key => cache.delete(String(key));
  return get;
}
