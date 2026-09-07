import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { parseRichTextBlocks, tokenizeInline } from "../workflows/richText.mjs";

export const RichText = memo(function RichText({ children, className = "" }) {
  const blocks = useMemo(() => parseRichTextBlocks(children), [children]);
  return (
    <div className={`rich-text ${className}`.trim()}>
      {blocks.map((block, index) => <RichBlock key={`${block.type}-${index}`} block={block} />)}
    </div>
  );
});

function RichBlock({ block }) {
  if (block.type === "code") return <CodeBlock block={block} />;
  if (block.type === "heading") {
    const Heading = `h${Math.min(4, block.level + 1)}`;
    return <Heading>{renderInline(block.content)}</Heading>;
  }
  if (block.type === "unordered-list" || block.type === "ordered-list") {
    const List = block.type === "ordered-list" ? "ol" : "ul";
    return <List>{block.items.map((item, index) => <li key={index}>{renderInline(item)}</li>)}</List>;
  }
  if (block.type === "quote") return <blockquote>{renderInlineWithBreaks(block.content)}</blockquote>;
  return <p>{renderInlineWithBreaks(block.content)}</p>;
}

let highlighter;
const loadHighlighter = () => import("../workflows/syntaxHighlight.mjs").then(module => {
  highlighter = module.highlightCode;
  return highlighter;
});

const CodeBlock = memo(function CodeBlock({ block }) {
  const [copied, setCopied] = useState(false);
  const [ready, setReady] = useState(() => Boolean(highlighter));
  const copyTimer = useRef(null);
  useEffect(() => {
    let active = true;
    if (!highlighter) loadHighlighter().then(() => { if (active) setReady(true); }).catch(() => {});
    return () => { active = false; window.clearTimeout(copyTimer.current); };
  }, []);
  const highlighted = useMemo(() => ready && highlighter
    ? highlighter(block.content, block.language) : null, [ready, block.content, block.language]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(block.content);
      setCopied(true);
      window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="rich-code">
      <div className="rich-code__bar">
        <span>{block.language || "Code"}</span>
        <button type="button" onClick={copy} aria-label="Copy code">
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>
      <pre>{highlighted !== null
        ? <code className="hljs" dangerouslySetInnerHTML={{__html:highlighted}} />
        : <code>{block.content}</code>}</pre>
    </div>
  );
}, (previous, next) => previous.block.content === next.block.content && previous.block.language === next.block.language);

function renderInlineWithBreaks(value) {
  return String(value).split("\n").flatMap((line, lineIndex) => [
    ...(lineIndex ? [<br key={`br-${lineIndex}`} />] : []),
    ...renderInline(line, `line-${lineIndex}`),
  ]);
}

function renderInline(value, prefix = "inline") {
  return tokenizeInline(value).map((token, index) => {
    const key = `${prefix}-${index}`;
    if (token.type === "code") return <code key={key}>{token.content}</code>;
    if (token.type === "strong") return <strong key={key}>{token.content}</strong>;
    if (token.type === "emphasis") return <em key={key}>{token.content}</em>;
    if (token.type === "link") {
      return <a key={key} href={token.href} target="_blank" rel="noreferrer">{token.content}</a>;
    }
    return token.content;
  });
}
