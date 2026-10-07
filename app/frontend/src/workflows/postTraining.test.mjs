import assert from "node:assert/strict";
import test from "node:test";
import {
  ACTIVATION_PHASES,
  DEFAULT_POST_TRAINING_POLICY,
  EVALUATION_PHASES,
  POST_TRAINING_POLICIES,
  SAVE_PHASES,
  phasesForPolicy,
  trainingCompletionEvidence,
} from "./postTraining.mjs";

const values = (phases) => phases.map((phase) => phase.value);

test("the explicit policy selector has the required labels and recommended default", () => {
  assert.equal(DEFAULT_POST_TRAINING_POLICY, "evaluate_and_activate");
  assert.deepEqual(
    POST_TRAINING_POLICIES.map(({ value, label }) => ({ value, label })),
    [
      {
        value: "evaluate_and_activate",
        label: "Evaluate and use in Chat",
      },
      { value: "evaluate", label: "Evaluate after training" },
      { value: "save_only", label: "Save version only" },
    ],
  );
  assert.equal(
    POST_TRAINING_POLICIES.find(
      (policy) => policy.value === DEFAULT_POST_TRAINING_POLICY,
    )?.recommended,
    true,
  );
});

test("policy timelines contain only requested evidence phases", () => {
  assert.equal(phasesForPolicy("save_only"), SAVE_PHASES);
  assert.equal(phasesForPolicy("evaluate"), EVALUATION_PHASES);
  assert.equal(phasesForPolicy("evaluate_and_activate"), ACTIVATION_PHASES);

  assert.equal(values(SAVE_PHASES).includes("preparing_evaluation"), false);
  assert.equal(values(SAVE_PHASES).includes("loading_salty_potato"), false);
  assert.equal(
    values(EVALUATION_PHASES).includes("confirming_evaluation_evidence"),
    true,
  );
  assert.equal(values(EVALUATION_PHASES).includes("loading_salty_potato"), false);
  assert.equal(values(ACTIVATION_PHASES).includes("loading_salty_potato"), true);
  assert.equal(values(ACTIVATION_PHASES).includes("testing_real_generation"), true);
  assert.equal(values(ACTIVATION_PHASES).includes("failed"), false);
});

test("success marks require retention and exact activation evidence", () => {
  const incomplete = trainingCompletionEvidence({
    result: {
      post_training_policy: "evaluate_and_activate",
      active_in_chat: false,
      runtime_identity: null,
    },
  });
  assert.equal(incomplete.has("saving_evaluation_report"), true);
  assert.equal(incomplete.has("loading_salty_potato"), false);
  assert.equal(incomplete.has("applying_retention"), false);

  const complete = trainingCompletionEvidence({
    result: {
      post_training_policy: "evaluate_and_activate",
      active_in_chat: true,
      runtime_identity: { runtime_id: "runtime-1" },
      retention_completed: true,
    },
  });
  assert.equal(complete.has("loading_salty_potato"), true);
  assert.equal(complete.has("switching_chat_version"), true);
  assert.equal(complete.has("applying_retention"), true);
});
