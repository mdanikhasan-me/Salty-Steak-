export function splitAssistantContent(value) {
  const content = String(value || "");
  const match = content.match(/<think>([\s\S]*?)<\/think>/i);
  if (match && match.index !== undefined) {
    const before = content.slice(0, match.index).trim();
    const after = content.slice(match.index + match[0].length).trim();
    return {
      answer: [before, after].filter(Boolean).join("\n\n"),
      reasoning: match[1].trim(),
      reasoningIncomplete: false,
    };
  }





  const openMatch = content.match(/<think>([\s\S]*)$/i);
  if (openMatch && openMatch.index !== undefined) {
    return {
      answer: content.slice(0, openMatch.index).trim(),
      reasoning: openMatch[1].trim(),
      reasoningIncomplete: true,
    };
  }

  return {
    answer: content.trim(),
    reasoning: "",
    reasoningIncomplete: false,
  };
}

export function presentAssistantContent(value, reasoningMode) {
  const parsed = splitAssistantContent(value);
  const mode = String(reasoningMode || "").trim().toLowerCase();
  if (mode === "instant") {
    return {
      ...parsed,
      reasoning: "",
      reasoningIncomplete: false,
      cookingTurn: false,
    };
  }
  return {
    ...parsed,



    cookingTurn: Boolean(parsed.reasoning),
  };
}
