import test from "node:test";
import assert from "node:assert/strict";
import {
  chatGenerationConversationId,
  canStartChatSubmission,
  createSerialGenerationExecutor,
  imageGenerationMessageId,
  imageGenerationProposalId,
  isChatGenerationOperation,
  isImageGenerationOperation,
  latestRetryableUserMessageId,
  mergeFetchedConversationWithPending,
  ownsConversationGeneration,
  ownsImageProposalGeneration,
  ownsGenerationTask,
  shouldRenderConversationGeneration,
  visibleConversationForSelection,
} from "./chatGeneration.mjs";

test("only the latest completed user turn is retryable", () => {
  const messages = [
    { id: "u1", role: "user" },
    { id: "a1", role: "assistant" },
    { id: "u2", role: "user" },
    { id: "a2", role: "assistant" },
  ];
  assert.equal(latestRetryableUserMessageId(messages), "u2");
  assert.equal(
    latestRetryableUserMessageId([...messages, { id: "u3", role: "user" }]),
    null,
  );
  assert.equal(
    latestRetryableUserMessageId([
      {
        id: "u-action",
        role: "user",
        technical_details: {
          host_action: { intent: "filesystem.trash_file" },
        },
      },
      { id: "a-action", role: "assistant" },
    ]),
    null,
    "a destructive host action must never fall through to model retry",
  );
});

test("ordinary turns with an explicit no-action record remain retryable",()=>{
  const messages=[{id:"u",role:"user",technical_details:{host_action:{state:"not_requested",intent:null,execution_requested:false,execution_performed:false}}},{id:"a",role:"assistant"}];
  assert.equal(latestRetryableUserMessageId(messages),"u");
  messages[0].technical_details.host_action.execution_requested=true;
  assert.equal(latestRetryableUserMessageId(messages),null);
});

test("generation submissions retain invocation order across delayed requests", async () => {
  const execute = createSerialGenerationExecutor();
  const events = [];
  let releaseFirst;
  const firstGate = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const first = execute(async () => {
    events.push("first:start");
    await firstGate;
    events.push("first:end");
    return "first";
  });
  const second = execute(async () => {
    events.push("second:start");
    events.push("second:end");
    return "second";
  });

  await Promise.resolve();
  assert.deepEqual(events, ["first:start"]);
  releaseFirst();
  assert.deepEqual(await Promise.all([first, second]), ["first", "second"]);
  assert.deepEqual(events, [
    "first:start",
    "first:end",
    "second:start",
    "second:end",
  ]);
});

test("a stale generation task cannot replace the newest owner", () => {
  assert.equal(ownsGenerationTask(7, 7), true);
  assert.equal(ownsGenerationTask(8, 7), false);
});

test("generation ownership is conversation-scoped and fails closed", () => {
  const operation = {
    type: "chat_generation",
    target_id: "conversation-a",
    result: { conversation_id: "conversation-a" },
  };
  assert.equal(chatGenerationConversationId(operation), "conversation-a");
  assert.equal(ownsConversationGeneration(operation, "conversation-a"), true);
  assert.equal(ownsConversationGeneration(operation, "conversation-b"), false);
  assert.equal(ownsConversationGeneration({ type: "training", target_id: "conversation-a" }, "conversation-a"), false);
  assert.equal(ownsConversationGeneration({ type: "chat_generation" }, "conversation-a"), false);
});

test("image generation is owned by one conversation and one durable proposal", () => {
  const operation = {
    type: "chat_image_generation",
    target_id: "conversation-a",
    state: "running",
    details: {
      conversation_id: "conversation-a",
      proposal_id: "proposal-image-a",
      assistant_message_id: "assistant-image-a",
    },
  };
  assert.equal(isChatGenerationOperation(operation), true);
  assert.equal(isImageGenerationOperation(operation), true);
  assert.equal(imageGenerationProposalId(operation), "proposal-image-a");
  assert.equal(imageGenerationMessageId(operation), "assistant-image-a");
  assert.equal(
    ownsImageProposalGeneration(
      operation,
      "conversation-a",
      "proposal-image-a",
      "assistant-image-a",
    ),
    true,
  );
  assert.equal(
    ownsImageProposalGeneration(operation, "conversation-b", "proposal-image-a"),
    false,
  );
  assert.equal(
    ownsImageProposalGeneration(operation, "conversation-a", "proposal-image-b"),
    false,
  );
});

test("exactly the active owner conversation may render a generating response", () => {
  const running = {
    type: "chat_generation",
    target_id: "conversation-a",
    state: "running",
    result: { conversation_id: "conversation-a" },
  };
  assert.equal(
    shouldRenderConversationGeneration(running, "conversation-a", true),
    true,
  );
  assert.equal(
    shouldRenderConversationGeneration(running, "conversation-b", true),
    false,
  );
  assert.equal(
    shouldRenderConversationGeneration(running, "conversation-a", false),
    false,
  );
  for (const state of ["completed", "failed", "interrupted", "cancelled", ""]) {
    assert.equal(
      shouldRenderConversationGeneration({ ...running, state }, "conversation-a", true),
      false,
    );
  }
  assert.equal(
    shouldRenderConversationGeneration(
      {
        ...running,
        result: {
          conversation_id: "conversation-a",
          assistant_message_id: "assistant-a",
        },
      },
      "conversation-a",
      true,
      [{ id: "assistant-a", role: "assistant" }],
    ),
    false,
  );
  assert.equal(
    shouldRenderConversationGeneration(
      {
        type: "chat_image_generation",
        target_id: "conversation-a",
        state: "running",
        details: { conversation_id: "conversation-a" },
      },
      "conversation-a",
      true,
    ),
    false,
    "image progress belongs to its proposal instead of a duplicate assistant row",
  );
});

test("a transcript is visible only under its selected conversation identity", () => {
  const conversation = { id: "conversation-a", messages: [{ id: "u1" }] };
  assert.equal(
    visibleConversationForSelection(conversation, "conversation-a"),
    conversation,
  );
  assert.equal(
    visibleConversationForSelection(conversation, "conversation-b"),
    null,
  );
  assert.equal(visibleConversationForSelection(null, "conversation-a"), null);
});

test("a background refresh cannot erase a locally pending user turn", () => {
  const pending = {
    id: "pending-user-1",
    role: "user",
    content: "hello",
    pending: true,
  };
  assert.deepEqual(
    mergeFetchedConversationWithPending(
      { id: "conversation-a", messages: [] },
      { id: "conversation-a", messages: [pending] },
    ).messages,
    [pending],
  );
  assert.deepEqual(
    mergeFetchedConversationWithPending(
      {
        id: "conversation-a",
        messages: [{ id: "user-1", role: "user", content: "hello" }],
      },
      { id: "conversation-a", messages: [pending] },
    ).messages,
    [{ id: "user-1", role: "user", content: "hello" }],
  );
});

test("chat submission is single-flight and preserves a draft while generating", () => {
  assert.equal(canStartChatSubmission({ sending: false, content: "hello" }), true);
  assert.equal(canStartChatSubmission({ sending: true, content: "hello" }), false);
  assert.equal(canStartChatSubmission({ sending: false, content: "   " }), false);
});
