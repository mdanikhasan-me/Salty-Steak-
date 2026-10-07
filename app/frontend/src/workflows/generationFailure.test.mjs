import assert from "node:assert/strict";
import test from "node:test";

import { generationFailureForMessage } from "./generationFailure.mjs";

test("failed user turns expose the durable generation error", () => {
  assert.deepEqual(
    generationFailureForMessage({
      role: "user",
      content: "what is your name",
      technical_details: {
        generation_state: "failed",
        generation_error: {
          type: "SaltyNativeRuntimeError",
          message: "The latest message exceeds the selected context window",
        },
      },
    }),
    {
      type: "SaltyNativeRuntimeError",
      message: "The latest message exceeds the selected context window",
    },
  );
});

test("completed and assistant messages do not render a failure", () => {
  assert.equal(
    generationFailureForMessage({ role: "user", technical_details: { generation_state: "completed" } }),
    null,
  );
  assert.equal(
    generationFailureForMessage({ role: "assistant", technical_details: { generation_state: "failed" } }),
    null,
  );
});
