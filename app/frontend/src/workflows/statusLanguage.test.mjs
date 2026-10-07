import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { glob } from "node:fs/promises";

import {
  completionLabel,
  workingLabel,
} from "./responseProvenance.mjs";
import { outcomeLabel } from "./activitySummary.mjs";

const turn = (state, ms = 41_000) => ({
  turn_completion: state,
  turn_duration_ms: ms,
});

test("work in progress is Zonting", () => {
  assert.equal(workingLabel("instant", 8_000), "Zonting · 8s");


  assert.equal(workingLabel("cooking", 72_000), "Zonting · 1m 12s");
});

test("an ordinary answer keeps its own word", () => {



  assert.match(completionLabel(turn("done")), /^Done in /);
  assert.match(completionLabel(turn("cooked")), /^Cooked in /);
});

test("only verified external work is Zonted", () => {
  assert.match(completionLabel(turn("zonted")), /^Zonted · /);

  assert.match(completionLabel(turn("partial")), /^Incomplete · /);
});

test("a turn that could not read its own decision is never Zonted", () => {







  assert.match(
    completionLabel({
      turn_duration_ms: 13_000,
      reasoning_mode_effective: "cooking",
      orchestration: {
        schema: "salty-steak-turn-dispatch-v1",
        kind: "respond",
        decision_unparsable: true,
      },
    }),
    /^Couldn't finish · /,
  );


  assert.match(
    completionLabel({ decision_unparsable: true, turn_duration_ms: 13_000 }),
    /^Couldn't finish · /,
  );
});

test("every other ending says what actually happened", () => {
  assert.match(completionLabel(turn("stopped")), /^Stopped · /);
  assert.match(completionLabel(turn("failed")), /^Couldn't finish · /);
  assert.match(completionLabel(turn("partial")), /^Incomplete · /);
  assert.match(completionLabel(turn("waiting")), /^Waiting · /);
});

test("the engineering word never reaches the reader", () => {


  for (const state of ["done", "cooked", "stopped", "failed", "partial", "waiting"]) {
    assert.doesNotMatch(completionLabel(turn(state)), /partial/i);
  }
  assert.equal(outcomeLabel("partial"), "Incomplete");
  assert.doesNotMatch(outcomeLabel("partial"), /partial/i);
});

test("no user-facing surface prints the engineering word", async () => {
  const offenders = [];
  for await (const path of glob("app/frontend/src/**/*.{jsx,mjs,js}")) {
    if (path.includes(".test.")) continue;


    const source = (await readFile(path, "utf8"))
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/^\s*\/\/.*$/gm, "");


    for (const match of source.matchAll(/["'`]([^"'`\n]*Partial[^"'`\n]*)["'`]/g)) {
      offenders.push(`${path}: ${match[1]}`);
    }
  }
  assert.deepEqual(offenders, []);
});
