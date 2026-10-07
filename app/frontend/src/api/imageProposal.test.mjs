import assert from "node:assert/strict";
import test from "node:test";

globalThis.__SALTY_POTATO_BUILD_ID__ = "development";
const { api } = await import("./client.js");

test("image confirmation uses the proposal endpoint and never resends a prompt", async () => {
  const previousFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ data: { id: "operation-image-1", state: "queued" } }),
    };
  };
  try {
    const settings = {
      width: 512,
      height: 512,
      num_inference_steps: 8,
      seed: 42,
    };
    const result = await api.confirmImageProposal(
      "conversation/a",
      "proposal/a",
      "assistant-a",
      settings,
      "request-image-a",
    );
    assert.equal(result.id, "operation-image-1");
    assert.equal(
      captured.url,
      "/api/chat/conversations/conversation%2Fa/host-actions/proposal%2Fa/confirm",
    );
    assert.equal(captured.options.method, "POST");
    assert.equal(captured.options.headers.get("Idempotency-Key"), "request-image-a");
    assert.deepEqual(JSON.parse(captured.options.body), {
      assistant_message_id: "assistant-a",
      user_confirmed: true,
      generation_settings: settings,
      request_key: "request-image-a",
    });
    assert.equal(captured.options.body.includes("prompt"), false);
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("temporary cleanup confirmation carries a separate destructive-action phrase", async () => {
  const previousFetch = globalThis.fetch;
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      headers: { get: () => "application/json" },
      json: async () => ({ data: { id: "operation-cleanup-1", state: "queued" } }),
    };
  };
  try {
    await api.confirmHostActionProposal(
      "conversation-a",
      "cleanup-a",
      "assistant-a",
      null,
      "CLEAN_WINDOWS_TEMPORARY_FILES",
      "request-cleanup-a",
    );
    assert.deepEqual(JSON.parse(captured.options.body), {
      assistant_message_id: "assistant-a",
      user_confirmed: true,
      generation_settings: null,
      confirmation_text: "CLEAN_WINDOWS_TEMPORARY_FILES",
      request_key: "request-cleanup-a",
    });
  } finally {
    globalThis.fetch = previousFetch;
  }
});
