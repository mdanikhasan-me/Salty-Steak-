const CLOSED_THINK = /<think>([\s\S]*?)<\/think>/gi;
const UNCLOSED_THINK = /<think>([\s\S]*)$/i;

export function splitAssistantContent(value) {
  const content = String(value || "");
  const thoughts = [];





  let answer = content.replace(CLOSED_THINK, (_, inner) => {
    thoughts.push(String(inner).trim());
    return "";
  });




  let incomplete = false;
  const unclosed = answer.match(UNCLOSED_THINK);
  if (unclosed && unclosed.index !== undefined) {
    thoughts.push(String(unclosed[1]).trim());
    answer = answer.slice(0, unclosed.index);
    incomplete = true;
  }

  return {
    answer: answer.trim(),
    reasoning: thoughts.filter(Boolean).join("\n\n").trim(),
    reasoningIncomplete: incomplete,
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
