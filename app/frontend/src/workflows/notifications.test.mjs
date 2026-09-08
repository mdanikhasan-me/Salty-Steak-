import assert from "node:assert/strict";
import test from "node:test";
import {
  addNotification,
  expireNotifications,
  notificationReducer,
  notificationVisibleOnPage,
  pauseNotification,
  removeNotification,
  resumeNotification,
} from "./notifications.mjs";

test("notifications are deduplicated by committed transition ID", () => {
  const notification = {
    id: "operation-1:completed",
    kind: "success",
    message: "Dataset is ready for training.",
  };
  const once = addNotification([], notification, 1_000);
  const twice = addNotification(once, notification, 2_000);
  assert.equal(twice.length, 1);
  assert.equal(twice[0].createdAt, 1_000);
});

test("repeated UI acknowledgements coalesce but different operation failures remain distinct",()=>{
  const notice={id:'first',kind:'success',message:'Permissions updated.'};
  const first=addNotification([],notice,1000);
  assert.equal(addNotification(first,{...notice,id:'second'},1100),first);
  const error={id:'a',kind:'error',message:'Failed.',operationId:'a'};
  assert.equal(addNotification(addNotification([],error),{...error,id:'b',operationId:'b'}).length,2);
});

test("success and information notifications expire after ten seconds", () => {
  const item = addNotification(
    [],
    { id: "one", kind: "information", message: "Checking status." },
    5_000,
  );
  assert.equal(item[0].expiresAt, 15_000);
  assert.equal(expireNotifications(item, 14_999).length, 1);
  assert.equal(expireNotifications(item, 15_000).length, 0);
});

test("persistent errors do not expire automatically", () => {
  const item = addNotification(
    [],
    {
      id: "failure",
      kind: "error",
      message: "Evaluation failed.",
      persistent: true,
    },
    5_000,
  );
  assert.equal(item[0].expiresAt, null);
  assert.equal(expireNotifications(item, 500_000).length, 1);
});

test("notification timeout pauses while the notification is hovered or focused", () => {
  const item = addNotification([], { id: "one", message: "Saved." }, 1_000);
  const paused = pauseNotification(item, "one", 5_000);
  assert.equal(expireNotifications(paused, 20_000).length, 1);
  const resumed = resumeNotification(paused, "one", 20_000);
  assert.equal(resumed[0].expiresAt, 26_000);
});

test("notification reducer supports explicit dismissal", () => {
  const initial = [
    { id: "one", message: "One" },
    { id: "two", message: "Two" },
  ];
  assert.deepEqual(removeNotification(initial, "one"), [{ id: "two", message: "Two" }]);
  assert.deepEqual(notificationReducer(initial, { type: "remove", id: "two" }), [
    { id: "one", message: "One" },
  ]);
});

test("operation errors render only on their owning page", () => {
  const trainingError = { id: "training:failed", page: "train" };
  assert.equal(notificationVisibleOnPage(trainingError, "train"), true);
  assert.equal(notificationVisibleOnPage(trainingError, "evaluate"), false);
  assert.equal(notificationVisibleOnPage(trainingError, "data"), false);
  assert.equal(notificationVisibleOnPage(trainingError, "versions"), false);
  assert.equal(notificationVisibleOnPage(trainingError, "chat"), false);
});
