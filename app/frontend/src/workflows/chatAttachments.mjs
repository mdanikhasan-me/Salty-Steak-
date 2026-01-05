const MAX_ATTACHMENT_BYTES = 256 * 1024;
const MAX_MESSAGE_ATTACHMENT_BYTES = 512 * 1024;

const TEXT_EXTENSIONS = new Set([
  ".csv", ".js", ".json", ".jsonl", ".jsx", ".md", ".py", ".toml",
  ".ts", ".tsv", ".tsx", ".txt", ".xml", ".yaml", ".yml",
]);

export function attachmentKind(file) {
  const type = String(file?.type || "").trim().toLowerCase();
  const name = String(file?.name || "").trim().toLowerCase();
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("text/")) return "text";
  const extensionIndex = name.lastIndexOf(".");
  const extension = extensionIndex >= 0 ? name.slice(extensionIndex) : "";
  return TEXT_EXTENSIONS.has(extension) ? "text" : "unsupported";
}

export function validateAttachment(file, currentTotalBytes = 0) {
  const name = safeAttachmentName(file?.name);
  const size = Number(file?.size || 0);
  const kind = attachmentKind(file);
  if (kind === "image") {
    return {
      accepted: false,
      code: "vision_required",
      message: `${name} needs a vision-capable model. The current text model cannot analyze pictures.`,
    };
  }
  if (kind !== "text") {
    return {
      accepted: false,
      code: "unsupported_type",
      message: `${name} is not a supported text or code file.`,
    };
  }
  if (!Number.isFinite(size) || size < 0 || size > MAX_ATTACHMENT_BYTES) {
    return {
      accepted: false,
      code: "file_too_large",
      message: `${name} is larger than the 256 KB per-file limit.`,
    };
  }
  if (Number(currentTotalBytes) + size > MAX_MESSAGE_ATTACHMENT_BYTES) {
    return {
      accepted: false,
      code: "message_too_large",
      message: "Attached text is limited to 512 KB per message.",
    };
  }
  return { accepted: true, kind, name, size };
}

const VISION_MEDIA_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/bmp",
]);

export function validateVisionAttachment(
  file,
  visionStatus,
  currentImageCount = 0,
  currentTextCount = 0,
) {
  const name = safeAttachmentName(file?.name);
  const type = String(file?.type || "").trim().toLowerCase();
  const size = Number(file?.size || 0);
  if (!visionStatus?.application_available) {
    return {
      accepted: false,
      code: "vision_unavailable",
      message: String(visionStatus?.reason || "Image analysis is unavailable."),
    };
  }
  if (currentImageCount > 0) {
    return {
      accepted: false,
      code: "one_image_only",
      message: "Analyze one image at a time.",
    };
  }
  if (currentTextCount > 0) {
    return {
      accepted: false,
      code: "mixed_attachment_types",
      message: "Image analysis cannot be mixed with text-file attachments in one request.",
    };
  }
  if (!VISION_MEDIA_TYPES.has(type)) {
    return {
      accepted: false,
      code: "unsupported_image_type",
      message: `${name} must be a PNG, JPEG, WebP, or BMP image.`,
    };
  }
  if (!Number.isFinite(size) || size < 1 || size > 25 * 1024 * 1024) {
    return {
      accepted: false,
      code: "vision_file_too_large",
      message: `${name} must contain 1 byte to 25 MiB.`,
    };
  }
  return { accepted: true, kind: "image", name, size, type };
}

export function attachmentPromptSuffix(attachments) {
  return (attachments || []).map((file) => {
    const name = safeAttachmentName(file?.name);
    const content = String(file?.content || "");
    return `\n\n--- BEGIN LOCAL FILE: ${name} ---\n${content}\n--- END LOCAL FILE: ${name} ---`;
  }).join("");
}

export function safeAttachmentName(value) {
  const cleaned = String(value || "file")
    .replace(/[\r\n\u0000-\u001f\u007f]+/g, " ")
    .trim();
  return cleaned || "file";
}
