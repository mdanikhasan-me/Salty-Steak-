const IMAGE_SCHEMA = "salty-steak-generated-image-v1";
const SHA256 = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[a-zA-Z0-9][a-zA-Z0-9_-]{7,127}$/;

export function createFreshImageSeed(randomWords = null) {
  const words = randomWords || new Uint32Array(2);
  if (!randomWords) {
    if (globalThis.crypto?.getRandomValues) {
      globalThis.crypto.getRandomValues(words);
    } else {
      words[0] = Math.floor(Math.random() * 0x1_0000_0000);
      words[1] = Date.now() >>> 0;
    }
  }
  if (!words || words.length < 2) throw new TypeError("Two random words are required");
  const high = Number(words[0]) >>> 0;
  const low = Number(words[1]) >>> 0;
  return (high * 0x20_0000) + (low & 0x1f_ffff);
}

export function generatedImageForMessage(message) {
  if (!message || message.role !== "assistant") return null;
  const details = message.technical_details || message.details || {};
  const value = details.generated_image;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const id = String(value.id || "");
  const sha256 = String(value.sha256 || "").toLowerCase();
  const mediaType = String(value.media_type || "");
  const width = Number(value.width);
  const height = Number(value.height);
  const sizeBytes = Number(value.size_bytes);
  if (
    value.schema !== IMAGE_SCHEMA
    || !IDENTIFIER.test(id)
    || !SHA256.test(sha256)
    || mediaType !== "image/png"
    || !Number.isInteger(width)
    || !Number.isInteger(height)
    || width < 64
    || height < 64
    || width > 4096
    || height > 4096
    || !Number.isInteger(sizeBytes)
    || sizeBytes <= 0
  ) return null;
  const prompt = String(value.prompt || "").trim().slice(0, 4_000);
  return Object.freeze({
    id,
    sha256,
    mediaType,
    width,
    height,
    sizeBytes,
    prompt,
    modelName: String(value.model_name || "Steak gen 1 ScaledFP8"),
    url: `/api/chat/image-artifacts/${encodeURIComponent(id)}`,
  });
}
