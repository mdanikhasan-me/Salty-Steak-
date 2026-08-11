







export function contactSheetLayout(count, width = 2048, height = 2048) {
  const total = Math.max(1, Math.min(12, Number(count) || 1));
  const columns = Math.ceil(Math.sqrt(total));
  const rows = Math.ceil(total / columns);
  const gap = 16;
  const labelHeight = 42;
  const cellWidth = Math.floor((width - gap * (columns + 1)) / columns);
  const cellHeight = Math.floor((height - gap * (rows + 1)) / rows);
  return { width, height, total, columns, rows, gap, labelHeight, cellWidth, cellHeight };
}

async function decodeImage(file) {
  if (typeof createImageBitmap === "function") return createImageBitmap(file);
  const url = URL.createObjectURL(file);
  try {
    const image = new globalThis.Image();
    image.decoding = "async";
    image.src = url;
    await image.decode();
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

function drawContained(context, image, x, y, width, height) {
  const sourceWidth = Number(image.width || image.naturalWidth || 1);
  const sourceHeight = Number(image.height || image.naturalHeight || 1);
  const scale = Math.min(width / sourceWidth, height / sourceHeight);
  const drawWidth = Math.max(1, Math.floor(sourceWidth * scale));
  const drawHeight = Math.max(1, Math.floor(sourceHeight * scale));
  context.drawImage(
    image,
    x + Math.floor((width - drawWidth) / 2),
    y + Math.floor((height - drawHeight) / 2),
    drawWidth,
    drawHeight,
  );
}

function canvasBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("Could not encode image contact sheet."))),
      type,
      quality,
    );
  });
}

export async function buildVisionContactSheet(attachments) {
  const selected = (attachments || []).filter((item) => item?.file).slice(0, 12);
  if (!selected.length) throw new Error("Select at least one image.");
  if (selected.length === 1) return selected[0].file;

  const layout = contactSheetLayout(selected.length);
  const canvas = document.createElement("canvas");
  canvas.width = layout.width;
  canvas.height = layout.height;
  const context = canvas.getContext("2d", { alpha: false });
  if (!context) throw new Error("The image compositor is unavailable.");
  context.fillStyle = "#151515";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.font = "600 22px system-ui, sans-serif";
  context.textBaseline = "middle";

  const decoded = await Promise.all(selected.map((item) => decodeImage(item.file)));
  try {
    decoded.forEach((image, index) => {
      const column = index % layout.columns;
      const row = Math.floor(index / layout.columns);
      const x = layout.gap + column * (layout.cellWidth + layout.gap);
      const y = layout.gap + row * (layout.cellHeight + layout.gap);
      context.fillStyle = "#090909";
      context.fillRect(x, y, layout.cellWidth, layout.cellHeight);
      drawContained(
        context,
        image,
        x,
        y + layout.labelHeight,
        layout.cellWidth,
        layout.cellHeight - layout.labelHeight,
      );
      context.fillStyle = "rgba(8, 8, 8, 0.9)";
      context.fillRect(x, y, layout.cellWidth, layout.labelHeight);
      context.fillStyle = "#f4f0eb";
      const label = `Image ${index + 1} — ${String(selected[index].name || "image").slice(0, 54)}`;
      context.fillText(label, x + 12, y + layout.labelHeight / 2, layout.cellWidth - 24);
    });
    const blob = await canvasBlob(canvas, "image/jpeg", 0.94);
    return new File([blob], "salty-steak-reference-contact-sheet.jpg", {
      type: "image/jpeg",
      lastModified: Date.now(),
    });
  } finally {
    decoded.forEach((image) => image?.close?.());
  }
}

export default buildVisionContactSheet;
