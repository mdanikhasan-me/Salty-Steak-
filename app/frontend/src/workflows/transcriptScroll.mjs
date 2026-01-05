export const TRANSCRIPT_BOTTOM_THRESHOLD = 56;

export function isNearTranscriptBottom(metrics, threshold = TRANSCRIPT_BOTTOM_THRESHOLD) {
  if (!metrics) return true;
  const remaining = metrics.scrollHeight - metrics.scrollTop - metrics.clientHeight;
  return remaining <= threshold;
}

export function shouldFollowTranscript({ wasNearBottom, contentChanged }) {
  return Boolean(wasNearBottom && contentChanged);
}
