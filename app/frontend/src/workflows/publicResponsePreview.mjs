// The backend sends a bounded tail with an absolute codepoint offset, not a
// token delta. Merge only verified overlap from one operation and stream.
export function mergePublicPreview(previous, preview, operationId, limit = 64_000) {
  if (preview?.kind !== "output" || !preview.tail_text || !operationId) return null;
  const tail = Array.from(String(preview.tail_text));
  const end = Number(preview.stream_character_count ?? preview.character_count);
  if (!Number.isSafeInteger(end) || end < tail.length) return null;
  const key = `${operationId}:${preview.stream_id || "primary"}`;
  const start = end - tail.length;
  if (previous?.key === key && end < previous.end) return previous;
  let text = tail, first = start;
  if (previous?.key === key && start <= previous.end && start >= previous.start) {
    const prior = Array.from(previous.text);
    const overlap = previous.end - start;
    if (prior.slice(start - previous.start).join("") === tail.slice(0, overlap).join("")) {
      if (end === previous.end) return previous;
      text = prior.concat(tail.slice(overlap)); first = previous.start;
    }
  }
  if (text.length > limit) { first += text.length - limit; text = text.slice(-limit); }
  return { key, text: text.join(""), start: first, end, partial: first > 0 };
}
