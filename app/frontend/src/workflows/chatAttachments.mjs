export const MAX_ATTACHMENT_BYTES = 512 * 1024 * 1024;
export const MAX_MESSAGE_ATTACHMENT_BYTES = 2 * 1024 * 1024 * 1024;
export const MAX_MESSAGE_ATTACHMENTS = 32;
export const MAX_VISION_ATTACHMENTS = 12;

export function attachmentKind(file) {
  const type = String(file?.type || "").trim().toLowerCase();
  if (type.startsWith("image/")) return "image";
  return "file";
}

export function validateAttachment(file, currentTotalBytes = 0, currentCount = 0) {
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
  if (Number(currentCount) >= MAX_MESSAGE_ATTACHMENTS) {
    return {
      accepted: false,
      code: "too_many_files",
      message: `A message can contain up to ${MAX_MESSAGE_ATTACHMENTS} files.`,
    };
  }
  if (!Number.isFinite(size) || size < 1 || size > MAX_ATTACHMENT_BYTES) {
    return {
      accepted: false,
      code: "file_too_large",
      message: `${name} must contain 1 byte to 512 MiB.`,
    };
  }
  if (Number(currentTotalBytes) + size > MAX_MESSAGE_ATTACHMENT_BYTES) {
    return {
      accepted: false,
      code: "message_too_large",
      message: "Attached files are limited to 2 GiB per message.",
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
  _currentFileCount = 0,
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
  if (currentImageCount >= MAX_VISION_ATTACHMENTS) {
    return {
      accepted: false,
      code: "too_many_images",
      message: `Analyze up to ${MAX_VISION_ATTACHMENTS} images in one message.`,
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
  return (attachments || [])
    .map((file) => String(file?.inspection?.prompt_text || file?.prompt_text || ""))
    .filter(Boolean)
    .map((content) => `\n\n${content}`)
    .join("");
}

export function safeAttachmentName(value) {
  const cleaned = String(value || "file")
    .replace(/[\r\n\u0000-\u001f\u007f]+/g, " ")
    .trim();
  return cleaned || "file";
}
