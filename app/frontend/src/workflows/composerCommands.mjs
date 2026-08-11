











const COMMANDS = [
  {
    names: ["image", "img", "photo", "picture", "draw", "render"],
    mode: "image_mode",
    description: "Create an image from this request",
  },
  {
    names: ["research", "search", "web"],
    mode: "research_mode",
    description: "Use researched, source-backed mode",
  },
];

const PUBLISHED_COMMANDS = [
  {
    command: "/save mem",
    aliases: [],
    action: "save_memory",
    description: "Save this chat context, or the note after the command, to global memory",
  },
  ...COMMANDS.map((command) => ({
    command: `/${command.names[0]}`,
    aliases: command.names.slice(1).map((name) => `/${name}`),
    mode: command.mode,
    description: command.description,
  })),
];










export function readComposerCommand(text) {
  const raw = String(text ?? "");
  const saveMemory = /^\s*\/save\s+mem(?:\s+|$)/i.exec(raw);
  if (saveMemory) {
    return {
      content: raw.slice(saveMemory[0].length).trim(),
      modes: {},
      action: { type: "save_memory" },
    };
  }
  const match = /^\s*\/([a-zA-Z]+)(\s+|$)/.exec(raw);
  if (!match) return { content: raw, modes: {} };

  const typed = match[1].toLowerCase();
  const found = COMMANDS.find((command) => command.names.includes(typed));
  if (!found) return { content: raw, modes: {} };

  const remainder = raw.slice(match[0].length).trim();
  if (!remainder) return { content: raw, modes: {} };

  return { content: remainder, modes: { [found.mode]: true } };
}


export function composerCommands() {
  return PUBLISHED_COMMANDS.map((command) => ({
    ...command,
    aliases: [...command.aliases],
  }));
}


export function composerCommandSuggestions(text) {
  const raw = String(text ?? "");
  if (raw.includes("\n")) return [];
  const typed = raw.trimStart();
  if (!typed.startsWith("/")) return [];
  const lowered = typed.toLocaleLowerCase();
  const completed = PUBLISHED_COMMANDS.some((entry) =>
    [entry.command, ...entry.aliases].some((name) => lowered.startsWith(`${name} `)));
  if (completed) return [];
  return PUBLISHED_COMMANDS.filter((entry) =>
    [entry.command, ...entry.aliases].some((name) => name.startsWith(lowered)));
}

export default readComposerCommand;
