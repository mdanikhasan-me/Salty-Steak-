const FENCE = /^```\s*([\w.+-]*)\s*$/;
const HEADING = /^(#{1,4})\s+(.+)$/;
const UNORDERED = /^\s*[-*+]\s+(.+)$/;
const ORDERED = /^\s*\d+[.)]\s+(.+)$/;
const QUOTE = /^\s*>\s?(.*)$/;

export function parseRichTextBlocks(value) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(FENCE);
    if (fence) {
      const content = [];
      index += 1;
      while (index < lines.length && !FENCE.test(lines[index])) {
        content.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: "code", language: fence[1] || "", content: content.join("\n") });
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, content: heading[2].trim() });
      index += 1;
      continue;
    }

    const unordered = line.match(UNORDERED);
    const ordered = line.match(ORDERED);
    if (unordered || ordered) {
      const type = ordered ? "ordered-list" : "unordered-list";
      const pattern = ordered ? ORDERED : UNORDERED;
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(pattern);
        if (!item) break;
        items.push(item[1].trim());
        index += 1;
      }
      blocks.push({ type, items });
      continue;
    }

    const quote = line.match(QUOTE);
    if (quote) {
      const content = [];
      while (index < lines.length) {
        const quoted = lines[index].match(QUOTE);
        if (!quoted) break;
        content.push(quoted[1]);
        index += 1;
      }
      blocks.push({ type: "quote", content: content.join("\n").trim() });
      continue;
    }

    const content = [line];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !FENCE.test(lines[index]) &&
      !HEADING.test(lines[index]) &&
      !UNORDERED.test(lines[index]) &&
      !ORDERED.test(lines[index]) &&
      !QUOTE.test(lines[index])
    ) {
      content.push(lines[index]);
      index += 1;
    }
    blocks.push({ type: "paragraph", content: content.join("\n").trim() });
  }

  return blocks;
}

export function tokenizeInline(value) {
  const source = String(value || "");
  const tokens = [];
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|_([^_\n]+)_|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let cursor = 0;
  let match;

  while ((match = pattern.exec(source))) {
    if (match.index > cursor) tokens.push({ type: "text", content: source.slice(cursor, match.index) });
    const token = match[0];
    if (token.startsWith("`")) {
      tokens.push({ type: "code", content: token.slice(1, -1) });
    } else if (token.startsWith("**") || token.startsWith("__")) {
      tokens.push({ type: "strong", content: token.slice(2, -2) });
    } else if (token.startsWith("[")) {
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      tokens.push(link
        ? { type: "link", content: link[1], href: link[2] }
        : { type: "text", content: token });
    } else {
      tokens.push({ type: "emphasis", content: token.slice(1, -1) });
    }
    cursor = pattern.lastIndex;
  }

  if (cursor < source.length) tokens.push({ type: "text", content: source.slice(cursor) });
  return tokens;
}
