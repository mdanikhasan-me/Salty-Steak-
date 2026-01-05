export function mappingIsValid(mapping) {
  if (mapping.type === "oasst2") return mapping.branch_policy === "best_ranked_leaf";
  if (mapping.type === "plain_text") return Boolean(mapping.plain_text);
  if (mapping.type === "instruction") {
    return Boolean(mapping.instruction && mapping.assistant_response);
  }
  return Boolean(mapping.conversation_messages);
}

export function formatTrainingExample(mapping, sample) {
  const read = (field) => {
    if (!field) return "";
    const value = sample?.[field];
    return value === undefined || value === null
      ? ""
      : typeof value === "string"
        ? value
        : JSON.stringify(value, null, 2);
  };

  if (mapping.type === "plain_text") {
    return read(mapping.plain_text) || "No text in this sample.";
  }

  if (mapping.type === "oasst2") {
    const prompt = sample?.prompt;
    if (!prompt || typeof prompt !== "object") {
      return "OASST2 flat messages are reconstructed by message_tree_id during validation.";
    }
    const branch = [];
    let current = prompt;
    while (current && typeof current === "object") {
      branch.push(current);
      const replies = Array.isArray(current.replies)
        ? current.replies.filter(
            (reply) =>
              reply &&
              !reply.deleted &&
              reply.review_result !== false &&
              (!mapping.languages ||
                String(mapping.languages)
                  .split(",")
                  .map((value) => value.trim().toLowerCase())
                  .includes(String(reply.lang || "").toLowerCase())),
          )
        : [];
      current = replies.sort(
        (left, right) =>
          Number(left.rank ?? Number.MAX_SAFE_INTEGER) -
            Number(right.rank ?? Number.MAX_SAFE_INTEGER) ||
          String(left.message_id || "").localeCompare(String(right.message_id || "")),
      )[0];
    }
    while (branch.length && branch.at(-1)?.role !== "assistant") branch.pop();
    return (
      branch
        .map(
          (item) =>
            `${item.role === "prompter" ? "User" : "Assistant"}:\n${String(item.text || "")}`,
        )
        .join("\n\n") || "No valid assistant-ending branch in this sample."
    );
  }

  if (mapping.type === "conversation") {
    const system = read(mapping.system_content);
    const messages = sample?.[mapping.conversation_messages];
    const labels = { system: "System", user: "User", assistant: "Assistant" };
    const conversation = Array.isArray(messages)
      ? messages
          .map((message) => {
            const label = labels[String(message?.role || "").toLowerCase()];
            const content = message?.content;
            return label && content !== null && content !== undefined
              ? `${label}:\n${String(content)}`
              : "";
          })
          .filter(Boolean)
          .join("\n\n")
      : "";
    return (
      [system ? `System:\n${system}` : "", conversation]
        .filter(Boolean)
        .join("\n\n") || "No mapped content in this sample."
    );
  }

  const system = read(mapping.system_content);
  const instruction = read(mapping.instruction);
  const input = read(mapping.user_input);
  const response = read(mapping.assistant_response);
  return (
    [
      system ? `System:\n${system}` : "",
      instruction ? `Instruction:\n${instruction}` : "",
      input ? `User input:\n${input}` : "",
      response ? `Assistant:\n${response}` : "",
    ]
      .filter(Boolean)
      .join("\n\n") || "No mapped content in this sample."
  );
}

export function mappingName(type) {
  return {
    plain_text: "Plain text",
    instruction: "Instruction and response",
    conversation: "Conversation messages",
    oasst2: "OASST2 ranked conversation trees",
  }[type];
}

export function stripExtension(filename) {
  return filename.replace(/\.[^.]+$/, "").replaceAll(/[_-]+/g, " ");
}
