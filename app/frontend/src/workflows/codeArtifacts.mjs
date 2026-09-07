const LANGUAGE_EXTENSIONS = Object.freeze({
  bash: "sh",
  c: "c",
  cpp: "cpp",
  csharp: "cs",
  css: "css",
  csv: "csv",
  html: "html",
  java: "java",
  javascript: "js",
  js: "js",
  json: "json",
  jsx: "jsx",
  markdown: "md",
  md: "md",
  powershell: "ps1",
  ps1: "ps1",
  py: "py",
  python: "py",
  rust: "rs",
  sql: "sql",
  text: "txt",
  toml: "toml",
  ts: "ts",
  tsx: "tsx",
  typescript: "ts",
  xml: "xml",
  yaml: "yaml",
  yml: "yml",
});

const CODE_FENCE = /```([^\n`]*)\r?\n([\s\S]*?)```/g;
const FILE_TOKEN = /(?:^|\s)(?:file|filename)=['"]?([^\s'"]+)['"]?/i;

export function normaliseNamedCodeFences(value) {
  return String(value || "").replace(CODE_FENCE, (fence, header, body) => {
    if (FILE_TOKEN.test(header)) return fence;
    // Some local models put their explicit file marker below the fence label.
    const marker = body.match(/^\s*(?:file|filename)=([^\s'"=]+\.[a-z0-9]+)\r?\n/i);
    if (!marker) return fence;
    return `\`\`\`${header.trim()} file=${marker[1]}\n${body.slice(marker[0].length)}\`\`\``;
  });
}

function safeFilename(value, fallback) {
  const leaf = String(value || "").split(/[\\/]/).at(-1) || fallback;
  const cleaned = leaf
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-")
    .replace(/^\.+/, "")
    .slice(0, 100);
  return cleaned || fallback;
}


export function codeArtifactsFromText(value) {
  const text = normaliseNamedCodeFences(value);
  const artifacts = [];
  for (const match of text.matchAll(CODE_FENCE)) {
    const header = String(match[1] || "").trim();
    const language = String(header.split(/\s+/)[0] || "text").toLowerCase();
    const extension = LANGUAGE_EXTENSIONS[language] || "txt";
    const named = header.match(FILE_TOKEN)?.[1];




    if (!named) continue;
    const index = artifacts.length + 1;
    const fallback = `${artifacts.length ? `script-${index}` : "script"}.${extension}`;
    const filename = safeFilename(named, fallback);
    const content = String(match[2] || "").replace(/\r\n/g, "\n");
    if (!content.trim()) continue;
    artifacts.push({
      id: `${index}-${filename}`,
      filename,
      language,
      content,
      bytes: new TextEncoder().encode(content).length,
    });
  }
  return artifacts;
}
