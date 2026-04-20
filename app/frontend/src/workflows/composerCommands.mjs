











const COMMANDS = [
  { names: ["image", "img", "photo", "picture", "draw", "render"], mode: "image_mode" },
  { names: ["research", "search", "web"], mode: "research_mode" },
];










export function readComposerCommand(text) {
  const raw = String(text ?? "");
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
  return COMMANDS.map((command) => ({
    command: `/${command.names[0]}`,
    aliases: command.names.slice(1).map((name) => `/${name}`),
    mode: command.mode,
  }));
}

export default readComposerCommand;
