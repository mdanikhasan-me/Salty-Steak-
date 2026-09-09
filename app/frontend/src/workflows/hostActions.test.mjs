import assert from "node:assert/strict";
import test from "node:test";

import {
  HOST_ACTION_SCHEMA,
  hostActionProposalForMessage,
  hostSafeAssistantContent,
} from "./hostActions.mjs";

test("image proposal retains its reviewed model and canvas settings across normalization", () => {
  const settings = { model_id: "reviewed-model", aspect_ratio: "16:9", resolution: 768, steps: 12 };
  const proposal = hostActionProposalForMessage({ role: "assistant", technical_details: { host_action_proposal: {
    schema: HOST_ACTION_SCHEMA, id: "image-settings", kind: "image.generate", state: "pending_review",
    summary: "A quiet lake", arguments: {}, generation_settings: settings,
    image_settings: { default_model_id: "reviewed-model", models: [{ id: "reviewed-model", name: "Reviewed model" }] },
  } } });
  assert.deepEqual(proposal.generation_settings, settings);
  assert.equal(proposal.image_settings.models[0].id, "reviewed-model");
  assert.notEqual(proposal.generation_settings, settings);
});

test("durable host proposal is normalised without granting execution", () => {
  const proposal = hostActionProposalForMessage({
    role: "assistant",
    content: "A Windows terminal action is prepared.",
    technical_details: {
      host_action_proposal: {
        schema: HOST_ACTION_SCHEMA,
        id: "p1",
        kind: "terminal.execute",
        title: "Review terminal command",
        summary: "git status --short",
        state: "pending_review",
        arguments: { argv: ["git", "status", "--short"], timeout_seconds: 5 },
        requires_confirmation: true,
        execution_allowed: false,
      },
    },
  });
  assert.equal(proposal.kind, "terminal.execute");
  assert.equal(proposal.executionAllowed, false);
  assert.deepEqual(proposal.arguments.argv, ["git", "status", "--short"]);
});

test("an exact file Recycle Bin proposal survives the durable message boundary", () => {
  const proposal = hostActionProposalForMessage({
    role: "assistant",
    content: "Confirm below to move the exact file to the Windows Recycle Bin.",
    technical_details: {
      host_action_proposal: {
        schema: HOST_ACTION_SCHEMA,
        id: "file-proposal-1",
        kind: "filesystem.trash_file",
        title: "Move a file to Recycle Bin",
        summary: "C:\\Users\\example\\Desktop\\fixture.txt",
        state: "pending_review",
        risk: "destructive_recoverable",
        arguments: {
          path: "C:\\Users\\example\\Desktop\\fixture.txt",
          expected_size_bytes: 12,
          expected_modified_ns: 34,
        },
        requires_confirmation: true,
        execution_allowed: false,
      },
    },
  });

  assert.equal(proposal.kind, "filesystem.trash_file");
  assert.equal(proposal.state, "pending_review");
  assert.equal(proposal.executionAllowed, false);
  assert.equal(proposal.arguments.expected_size_bytes, 12);
});

test("a Windows temporary-file cleanup proposal survives the durable message boundary", () => {
  const proposal = hostActionProposalForMessage({
    role: "assistant",
    content: "The cleanup is ready for review.",
    technical_details: {
      host_action_proposal: {
        schema: HOST_ACTION_SCHEMA,
        id: "cleanup-proposal-1",
        kind: "system.clean_temp",
        title: "Clean Windows temporary files",
        summary: "Clean the contents of three reviewed Windows temporary folders.",
        state: "pending_review",
        risk: "destructive_irrecoverable",
        arguments: {
          roots: [
            { name: "User Temp", path: "C:\\Users\\example\\AppData\\Local\\Temp", accessible: true },
            { name: "Prefetch", path: "C:\\Windows\\Prefetch", accessible: false },
          ],
        },
        requires_confirmation: true,
        execution_allowed: false,
      },
    },
  });

  assert.equal(proposal.kind, "system.clean_temp");
  assert.equal(proposal.state, "pending_review");
  assert.equal(proposal.executionAllowed, false);
  assert.equal(proposal.arguments.roots.length, 2);
  assert.equal(proposal.arguments.roots[1].accessible, false);
});

test("exact legacy image JSON is hidden and becomes a blocked proposal", () => {
  const message = {
    id: "a1",
    role: "assistant",
    content: '{"action":"generate_image","prompt":"a person in a plane"}',
  };
  const proposal = hostActionProposalForMessage(message);
  assert.equal(proposal.kind, "image.generate");
  assert.equal(proposal.legacy, true);
  assert.equal(proposal.state, "blocked_runtime_unavailable");
  assert.equal(hostSafeAssistantContent(message, proposal), "");
});

test("ordinary or expanded JSON remains ordinary chat content", () => {
  const content = '{"action":"generate_image","prompt":"x","extra":true}';
  const message = { role: "assistant", content };
  assert.equal(hostActionProposalForMessage(message), null);
  assert.equal(hostSafeAssistantContent(message), content);
});

test("known historical Unix deletion advice is withheld in the Windows UI", () => {
  const safe = hostSafeAssistantContent({
    role: "assistant",
    content: "Use rm /path/to/temp.tmp and then sudo rm -rf /tmp/*",
  });
  assert.doesNotMatch(safe, /rm -rf/);
  assert.match(safe, /withheld them in the Windows app/);
});
