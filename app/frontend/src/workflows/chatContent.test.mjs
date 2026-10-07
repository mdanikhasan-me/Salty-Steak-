import assert from "node:assert/strict";
import test from "node:test";

import { presentAssistantContent, splitAssistantContent } from "./chatContent.mjs";

test("completed thinking tags are separated from the visible answer", () => {
  assert.deepEqual(
    splitAssistantContent("<think>check the instruction</think>\n\nready"),
    {
      answer: "ready",
      reasoning: "check the instruction",
      reasoningIncomplete: false,
    },
  );
});

test("empty completed thinking is removed from the visible answer", () => {
  assert.deepEqual(splitAssistantContent("<think>\n\n</think>\n\nready"), {
    answer: "ready",
    reasoning: "",
    reasoningIncomplete: false,
  });
});

test("unfinished thinking is disclosed without leaking a raw template marker", () => {
  assert.deepEqual(splitAssistantContent("<think>still working"), {
    answer: "",
    reasoning: "still working",
    reasoningIncomplete: true,
  });
});

test("Instant keeps the transcript direct even if a model emits a stray think block", () => {
  assert.deepEqual(
    presentAssistantContent("<think>unwanted trace</think>\n\nready", "instant"),
    {
      answer: "ready",
      reasoning: "",
      reasoningIncomplete: false,
      cookingTurn: false,
    },
  );
});

test("Cooking remains attributable to the completed message rather than the current composer mode", () => {
  assert.deepEqual(
    presentAssistantContent("<think>check the recipe</think>\n\nready", "cooking"),
    {
      answer: "ready",
      reasoning: "check the recipe",
      reasoningIncomplete: false,
      cookingTurn: true,
    },
  );
});

test("Cooking does not falsely label ordinary model prose as a completed trace", () => {
  assert.deepEqual(
    presentAssistantContent("Here is a direct response without model trace tags.", "cooking"),
    {
      answer: "Here is a direct response without model trace tags.",
      reasoning: "",
      reasoningIncomplete: false,
      cookingTurn: false,
    },
  );
});

test("every thinking block is stripped, not just the first", () => {



  const parsed = splitAssistantContent(
    "<think>\n\n</think>\n\n<think>\nThe user just said hi again.\n</think>\n\nHi there!",
  );

  assert.equal(parsed.answer, "Hi there!");
  assert.equal(parsed.reasoning, "The user just said hi again.");
  assert.equal(parsed.reasoningIncomplete, false);
  assert.ok(!parsed.answer.includes("<think>"));
});

test("a reply that runs out mid-thought shows no answer and no marker", () => {
  const parsed = splitAssistantContent("<think>\n\n</think>\n\n<think>halfway");

  assert.equal(parsed.answer, "");
  assert.equal(parsed.reasoning, "halfway");
  assert.equal(parsed.reasoningIncomplete, true);
});
