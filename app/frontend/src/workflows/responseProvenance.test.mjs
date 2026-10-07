import assert from "node:assert/strict";
import test from "node:test";

import {
  activityOf,
  completionLabel,
  completionState,
  formatElapsed,
  memoriesOf,
  responseDetails,
  sourcesOf,
  supportFor,
  turnDuration,
  visibleOutputTokens,
  workingLabel,
} from "./responseProvenance.mjs";

test("durations read the way a person says them", () => {
  assert.equal(formatElapsed(400), "0.4s");
  assert.equal(formatElapsed(4200), "4.2s");
  assert.equal(formatElapsed(31_400), "31s");
  assert.equal(formatElapsed(64_000), "1m 04s");
  assert.equal(formatElapsed(82_000), "1m 22s");
  assert.equal(formatElapsed(119_600), "2m 00s");
  assert.equal(formatElapsed(-1), "");
  assert.equal(formatElapsed(undefined), "");
});

test("Instant is done and Cooking is cooked, permanently", () => {



  assert.equal(
    completionLabel({ turn_completion: "done", turn_duration_ms: 4200 }),
    "Done in 4.2s",
  );
  assert.equal(
    completionLabel({ turn_completion: "cooked", turn_duration_ms: 31_400 }),
    "Cooked in 31s",
  );
  assert.equal(
    completionLabel({ turn_completion: "zonted", turn_duration_ms: 31_400 }),
    "Zonted · 31s",
  );
});

test("a turn that did not succeed never claims it did", () => {
  assert.equal(
    completionLabel({ turn_completion: "stopped", turn_duration_ms: 8000 }),
    "Stopped · 8s",
  );
  assert.equal(
    completionLabel({ turn_completion: "failed", turn_duration_ms: 12_000 }),
    "Couldn't finish · 12s",
  );
  assert.equal(
    completionLabel({ turn_completion: "partial", turn_duration_ms: 27_000 }),
    "Incomplete · 27s",
  );
  assert.equal(completionLabel({ turn_completion: "waiting" }), "Waiting");
});

test("history written before the backend recorded a state keeps its identity", () => {
  assert.equal(completionState({ reasoning_mode_effective: "cooking" }), "cooked");
  assert.equal(completionState({ reasoning_mode_effective: "instant" }), "done");
  assert.equal(completionState({ generation_state: "stopped" }), "stopped");
  assert.equal(completionState({ finish_reason: "maximum_output" }), "partial");
});

test("the turn's wall clock is preferred over its decode time", () => {

  assert.equal(
    turnDuration({ turn_duration_ms: 53_000, generation_duration_seconds: 3.2 }),
    53_000,
  );
  assert.equal(turnDuration({ generation_duration_seconds: 3.2 }), 3200);
  assert.equal(turnDuration({}), null);
});

test("a live turn says what it is doing and for how long", () => {


  assert.equal(workingLabel("instant", 4200), "Zonting · 4.2s");
  assert.equal(workingLabel("cooking", 31_000), "Zonting · 31s");
  assert.equal(workingLabel("instant", null), "Zonting");
});

const RESEARCH = {


  turn_completion: "zonted",
  turn_duration_ms: 31_000,
  orchestration: {
    kind: "research",
    research: {
      queries: ["tallest building in the world"],
      source_count: 2,
      corroborated: 3,
      disputed: 1,
      stop_reason: "source_budget",
    },
    sources: [
      {
        source: "src-1",
        url: "https://en.wikipedia.org/wiki/List_of_tallest_buildings",
        title: "List of tallest buildings",
      },
      { source: "src-2", url: "https://www.ctbuh.org/tallest", title: "CTBUH" },

      { source: "src-3", url: "https://www.ctbuh.org/tallest", title: "CTBUH" },
    ],
    claims: [
      { claim: "c1", text: "Burj Khalifa is 828 m tall.", sources: ["src-1", "src-2"] },
      { claim: "c2", text: "It has held the title since 2009.", sources: ["src-1"] },
    ],
  },
};

test("sources are real, deduplicated, and carry their host", () => {
  const sources = sourcesOf(RESEARCH);

  assert.equal(sources.length, 2);
  assert.equal(sources[0].host, "en.wikipedia.org");
  assert.equal(sources[1].host, "ctbuh.org");
  assert.equal(sources[1].title, "CTBUH");
});

test("a source shows what it actually supported", () => {
  assert.deepEqual(supportFor(RESEARCH, "src-2"), ["Burj Khalifa is 828 m tall."]);
  assert.equal(supportFor(RESEARCH, "src-1").length, 2);
  assert.deepEqual(supportFor(RESEARCH, "nothing"), []);
});

test("activity is what was done, not a script of stages", () => {
  const activity = activityOf(RESEARCH).map((event) => event.text);

  assert.ok(activity.some((text) => text.includes("Searched for")));
  assert.ok(activity.includes("Read 2 sources"));
  assert.ok(activity.some((text) => text.includes("3 findings agreed")));
  assert.ok(activity.some((text) => text.includes("1 finding disagreed")));
  assert.ok(activity.some((text) => text.includes("source budget")));
  assert.equal(activity.at(-1), "Zonted · 31s");
});

test("a plain answer still has a real account worth opening", () => {
  const plain = responseDetails({
    turn_completion: "done",
    turn_duration_ms: 4000,
    model_bundle_id: "base-steak-2-0-9b",
    reasoning_mode_effective: "instant",
    generated_output_tokens: 64,
    decode_tokens_per_second: 29.1,
  });

  assert.equal(plain.hasDetails, true);
  assert.deepEqual(plain.sources, []);
  assert.deepEqual(plain.memories, []);
  assert.equal(plain.completion, "Done in 4s");
  const text = plain.activity.map((event) => event.text);
  assert.deepEqual(text, [
    "Answered from base-steak-2-0-9b in instant mode",
    "Generated 64 output tokens at 29.1 tokens/sec",
    "Done in 4s",
  ]);
});

test("the token count belongs to the answer that was shown", () => {



  assert.equal(
    visibleOutputTokens({
      visible_output_tokens: 210,
      generated_output_tokens: 1,
      orchestration: { kind: "research" },
    }),
    210,
  );
});

test("a turn whose answer came from elsewhere reports no count rather than a wrong one", () => {




  assert.equal(
    visibleOutputTokens({
      generated_output_tokens: 64,
      orchestration: { kind: "respond" },
    }),
    64,
  );
  assert.equal(
    visibleOutputTokens({
      generated_output_tokens: 1,
      orchestration: { kind: "research" },
    }),
    null,
  );
  assert.equal(visibleOutputTokens({ generated_output_tokens: 64 }), 64);
});

test("a turn admits what went wrong with its own reply", () => {
  const recovered = responseDetails({
    turn_completion: "done",
    turn_duration_ms: 9000,
    decision_corrected_to_prose: true,
  });
  assert.ok(
    recovered.activity.some((event) => event.text.includes("rewritten as an answer")),
  );

  const failed = responseDetails({
    turn_completion: "cooked",
    turn_duration_ms: 99_000,
    decision_unparsable: true,
  });
  assert.ok(
    failed.activity.some((event) => event.text.includes("could not be corrected")),
  );
});

test("a stopped turn says so in its account", () => {
  const stopped = responseDetails({
    turn_completion: "stopped",
    turn_duration_ms: 8000,
    generation_state: "stopped",
  });

  assert.ok(stopped.activity.some((event) => event.text.includes("Stopped by you")));
  assert.equal(stopped.completion, "Stopped · 8s");
});

test("memory appears only when the turn actually used some", () => {
  assert.deepEqual(memoriesOf({ orchestration: {} }), []);
  assert.deepEqual(
    memoriesOf({ orchestration: { memory: [{ text: "Your GPU is an RTX 3070." }] } }),
    ["Your GPU is an RTX 3070."],
  );
});

test("executed tool steps become activity with their measured time", () => {
  const agent = responseDetails({
    turn_completion: "done",
    turn_duration_ms: 9000,
    orchestration: {
      steps: [
        { action: "application.launch", reason: "Open Notepad", duration_ms: 702 },
        { action: "respond", reason: "done" },
      ],
    },
  });

  assert.equal(agent.hasDetails, true);
  const tools = agent.activity.filter((event) => event.kind === "tool");
  assert.deepEqual(tools.map((event) => [event.tool, event.durationMs]), [
    ["application.launch", 702],
  ]);
});

test("private model reasoning cannot reach the response account", () => {



  const scratchpad =
    'The user just said "hi" and then "/think". This is a greeting, not really '
    + "a request for anything. I should reply briefly rather than use JSON "
    + "action format. Let me answer warmly.";

  const account = responseDetails({
    turn_completion: "cooked",
    turn_duration_ms: 4000,
    reasoning_text: scratchpad,
    reasoned: true,
    reasoning_characters: scratchpad.length,
  });

  const rendered = JSON.stringify(account);
  for (const fragment of [
    "The user just said",
    "I should reply",
    "rather than use JSON",
    "Let me answer",
    "/think",
  ]) {
    assert.ok(!rendered.includes(fragment), `leaked: ${fragment}`);
  }
});

test("an image turn reports both phases and the real end-to-end time", () => {
  const image = responseDetails({
    turn_completion: "cooked",
    turn_duration_ms: 1_110_083,
    turn_duration_breakdown: {
      schema: "salty-steak-turn-duration-breakdown-v1",
      measurement: "end_to_end_until_image_commit",
      text_preparation_ms: 34_219,
      image_operation_ms: 1_075_864,
      total_ms: 1_110_083,
    },
  });

  assert.equal(image.completion, "Cooked in 18m 30s");
  assert.deepEqual(
    image.activity.map((event) => event.text),
    [
      "Prepared the image request in 34s",
      "Generated the image in 17m 56s",
      "Cooked in 18m 30s",
    ],
  );
});

test("that reasoning happened is still reported, as a fact not a quotation", () => {
  const account = responseDetails({
    turn_completion: "cooked",
    turn_duration_ms: 4000,
    reasoned: true,
    reasoning_characters: 143,
  });
  assert.equal(account.reasoning, null);
  assert.deepEqual(account.reasoningSummary, { reasoned: true, characters: 143 });
});

test("a turn with no reasoning claims none", () => {
  const account = responseDetails({ turn_completion: "done", turn_duration_ms: 900 });
  assert.equal(account.reasoningSummary.reasoned, false);
});
