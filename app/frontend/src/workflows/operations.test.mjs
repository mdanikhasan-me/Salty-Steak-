import assert from "node:assert/strict";
import test from "node:test";
import {
  domainForOperation,
  isActive,
  isTerminal,
  latestTrainingOperation,
  measurableProgress,
  mergeOperations,
  nextPollDelay,
  notificationForTransition,
  operationMatches,
  phaseLabel,
  trainingHeaderState,
} from "./operations.mjs";

test("operation states distinguish active and terminal records", () => {
  assert.equal(isActive(null), false);
  assert.equal(isActive(undefined), false);
  assert.equal(isActive({ state: "queued" }), true);
  assert.equal(isActive({ state: "stop_requested" }), true);
  assert.equal(isActive({ state: "completed" }), false);
  assert.equal(isTerminal({ state: "completed" }), true);
  assert.equal(isTerminal({ state: "interrupted" }), true);
  assert.equal(isTerminal({ state: "cancelled" }), true);
  assert.equal(isActive({ state: "cancelled" }), false);
  assert.equal(isTerminal({ state: "running" }), false);
});

test("progress is shown only when the backend reports a measurable total", () => {
  assert.deepEqual(
    measurableProgress({ current_progress: 25, total_progress: 100 }),
    { current: 25, total: 100, percentage: 25 },
  );
  assert.equal(measurableProgress({ current_progress: 2, total_progress: 0 }), null);
  assert.equal(measurableProgress({ phase: "loading_model" }), null);
});

test("phase labels preserve real operation phases in beginner-friendly language", () => {
  assert.equal(phaseLabel({ phase: "preparing_evaluation_data" }), "Preparing evaluation data");
  assert.equal(phaseLabel({ phase: "testing_real_generation" }), "Testing real generation");
  assert.equal(phaseLabel({ phase: "restoring_chat" }), "Restoring Chat");
  assert.equal(phaseLabel({ phase: "custom_backend_phase" }), "Custom Backend Phase");
});

test("mergeOperations rejects an older persisted update", () => {
  const previous = {
    one: {
      id: "one",
      state: "running",
      phase: "training",
      updated_at: "2026-07-25T02:00:05Z",
    },
  };
  const merged = mergeOperations(previous, [
    {
      id: "one",
      state: "queued",
      phase: "waiting",
      updated_at: "2026-07-25T02:00:01Z",
    },
  ]);
  assert.equal(merged.one.state, "running");
  assert.equal(merged.one.phase, "training");
});

test("a late running snapshot cannot resurrect a completed operation",()=>{
  const previous={one:{id:"one",state:"completed"}};
  assert.equal(mergeOperations(previous,[{id:"one",state:"running"}]).one.state,"completed");
  assert.equal(domainForOperation("chat_generation"),"chat");
});

test("completion notifications require an observed committed transition", () => {
  assert.equal(
    notificationForTransition(null, {
      id: "op-1",
      type: "training",
      state: "completed",
    }),
    null,
  );
  assert.deepEqual(
    notificationForTransition(
      { id: "op-1", type: "training", state: "running" },
      {
        id: "op-1",
        type: "training",
        state: "completed",
        result: { notification: "Verified training result committed." },
      },
    ),
    {
      id: "op-1:completed",
      kind: "success",
      message: "Verified training result committed.",
      operationId: "op-1",
      page: "train",
    },
  );
});

test("failed transitions are persistent and carry the backend error", () => {
  const notification = notificationForTransition(
    { id: "op-2", state: "running" },
    {
      id: "op-2",
      type: "evaluation",
      state: "failed",
      error: { message: "Saved-version identity did not match." },
    },
  );
  assert.equal(notification.kind, "error");
  assert.equal(notification.persistent, true);
  assert.equal(notification.page, "evaluate");
  assert.match(notification.message, /identity did not match/);
});

test("failed training uses its persisted failure panel without a duplicate toast", () => {
  const failed = {
    id: "training-failed",
    type: "training",
    state: "failed",
    error: { message: "Version verification failed." },
  };
  assert.equal(
    notificationForTransition({ ...failed, state: "running" }, failed),
    null,
  );
  assert.deepEqual(trainingHeaderState(failed), {
    value: "failed",
    label: "Training failed",
  });
});

test("chat failures are scoped and retryable rather than permanent global toasts", () => {
  const previous = { id: "chat-failed", type: "chat_generation", state: "running" };
  const failed = {
    id: "chat-failed",
    type: "chat_generation",
    state: "failed",
    error: { message: "The operation could not complete." },
  };

  const notification = notificationForTransition(previous, failed);

  assert.equal(notification.kind, "error");
  assert.equal(notification.page, "chat");
  assert.equal(notification.persistent, false);
});

test("an absent training operation is ready, not queued", () => {
  assert.deepEqual(trainingHeaderState(undefined), {
    value: "ready",
    label: "Ready to set up",
  });
});

test("persisted training telemetry survives a frontend refresh", () => {
  const persisted = {
    id: "training-persisted",
    type: "training",
    state: "failed",
    updated_at: "2026-07-25T10:52:38Z",
    result: { training_loss: 4.9875, current_training_step: 10 },
  };
  assert.equal(latestTrainingOperation({}, persisted), persisted);
  assert.equal(
    latestTrainingOperation({}, persisted).result.current_training_step,
    10,
  );
});

test("polling is fast while work is active and backs off when idle", () => {
  assert.equal(
    nextPollDelay(3000, { active: { id: "active", state: "running" } }),
    750,
  );
  assert.equal(nextPollDelay(1000, {}), 1600);
  assert.equal(nextPollDelay(4900, {}), 5000);
});

test("operation matching and domain routing never depend on checkpoint ancestry", () => {
  const operation = {
    id: "op-3",
    type: "version_activation",
    target_id: "version-b",
    state: "queued",
  };
  assert.equal(operationMatches(operation, "version activation", "version-b"), true);
  assert.equal(operationMatches(operation, "version_activation", "version-a"), false);
  assert.equal(domainForOperation(operation.type), "chat");
  assert.equal(domainForOperation("version_deletion"), "versions");
  assert.equal(domainForOperation("training_finalisation_retry"), "training");
  assert.equal(domainForOperation("chat_image_generation"), "chat");
});
