import assert from "node:assert/strict";
import test from "node:test";
import { WORKSPACE_MODES, normaliseWorkspaceMode } from "./workspaceModes.mjs";
import { generationTurnSettingsForRequest } from "./composerControls.mjs";

test("an invalid stored mode falls back to Chat", () => {
  for (const value of [null, "", "training", "full_access", "unknown"]) assert.equal(normaliseWorkspaceMode(value), "chat");
});

test("navigation labels are Sawlper, Chat, Coding while persisted IDs stay stable", () => {
  assert.deepEqual(WORKSPACE_MODES.map(({id,label})=>({id,label})), [
    {id:"agent",label:"Sawlper"},{id:"chat",label:"Chat"},{id:"code",label:"Coding"},
  ]);
  assert.equal(normaliseWorkspaceMode("agent"),"agent");
  assert.equal(normaliseWorkspaceMode("code"),"code");
});

test("Code changes the turn mode without granting computer authority or changing saved instructions", () => {
  const settings = { system_prompt: "Use short explanations.", computer_authority_mode: "ask_every_time" };
  const result = generationTurnSettingsForRequest(settings, {}, { codeMode: true });
  assert.equal(result.code_mode, true);
  assert.equal(result.agent_mode, false);
  assert.equal(result.computer_authority_mode, "ask_every_time");
  assert.equal(result.system_prompt, settings.system_prompt);
  assert.equal(generationTurnSettingsForRequest(settings).code_mode, false);
  assert.equal(settings.code_mode, undefined);
});

test("a blank line while editing stop sequences is not sent to the runtime", () => {
  const draft = { stop_sequences: ["END", ""] };
  assert.deepEqual(generationTurnSettingsForRequest(draft).stop_sequences, ["END"]);
  assert.deepEqual(draft.stop_sequences, ["END", ""]);
});
