import assert from "node:assert/strict";
import test from "node:test";
import { latestMatchingOperation } from "./dataOperations.js";
import {
  formatTrainingExample,
  mappingIsValid,
  mappingName,
  stripExtension,
} from "./importDatasetUtils.js";

test("the newest matching dataset operation is selected", () => {
  const operations = {
    older: {
      type: "dataset_validation",
      target_id: "dataset-1",
      updated_at: "2026-07-24T10:00:00Z",
    },
    otherDataset: {
      type: "dataset_validation",
      target_id: "dataset-2",
      updated_at: "2026-07-24T12:00:00Z",
    },
    newer: {
      type: "dataset_validation",
      target_id: "dataset-1",
      updated_at: "2026-07-24T11:00:00Z",
    },
  };

  assert.equal(
    latestMatchingOperation(
      operations,
      "dataset_validation",
      "dataset-1",
    ),
    operations.newer,
  );
});

test("dataset mappings retain their validation requirements", () => {
  assert.equal(
    mappingIsValid({ type: "plain_text", plain_text: "text" }),
    true,
  );
  assert.equal(
    mappingIsValid({
      type: "instruction",
      instruction: "prompt",
      assistant_response: "",
    }),
    false,
  );
  assert.equal(
    mappingIsValid({
      type: "conversation",
      conversation_messages: "messages",
    }),
    true,
  );
});

test("formatted training examples preserve mapped conversation roles", () => {
  assert.equal(
    formatTrainingExample(
      {
        type: "conversation",
        system_content: "context",
        conversation_messages: "messages",
      },
      {
        context: "Be helpful.",
        messages: [
          { role: "user", content: "Hello" },
          { role: "assistant", content: "Hi" },
        ],
      },
    ),
    "System:\nBe helpful.\n\nUser:\nHello\n\nAssistant:\nHi",
  );
});

test("dataset labels and default names preserve existing formatting", () => {
  assert.equal(mappingName("instruction"), "Instruction and response");
  assert.equal(stripExtension("support_examples.jsonl"), "support examples");
});
