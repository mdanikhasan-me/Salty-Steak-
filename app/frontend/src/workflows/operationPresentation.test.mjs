import test from "node:test";
import assert from "node:assert/strict";
import { operationPresentation } from "./operationPresentation.mjs";
import { validationPresentation } from "./validationPresentation.mjs";

test("a failed non-training operation never claims training finalisation", () => {
  for (const [type, label] of [["evaluation", "Evaluation"], ["dataset_validation", "Dataset validation"], ["dataset_preparation", "Dataset preparation"]]) {
    const report = operationPresentation({ type, state: "failed", current_progress: 10, total_progress: 10 });
    assert.equal(report.outcomeTitle, `${label} did not complete`);
    assert.equal(report.errorTitle, `${label} failed`);
    assert.equal(report.completedMeasuredWork, false);
  }
});
test("training retains explicit failed finalisation after all measured steps", () => {
  const report = operationPresentation({ type: "training", state: "failed", current_progress: 10, total_progress: 10 });
  assert.equal(report.completedMeasuredWork, true);
  assert.match(report.outcomeTitle, /steps completed; version finalisation failed/);
});
test("terminal operation headline does not retain its previous running phase", () => {
  assert.equal(operationPresentation({ type: "evaluation", state: "cancelled", phase: "evaluating_records" }).outcomeTitle, "Evaluation cancelled");
  assert.equal(operationPresentation({ type: "dataset_preparation", state: "interrupted", phase: "tokenising" }).outcomeTitle, "Dataset preparation interrupted");
  assert.equal(operationPresentation({ type: "evaluation", state: "completed", phase: "evaluating_records" }).outcomeTitle, "Evaluation completed");
  assert.equal(operationPresentation({ type: "training", state: "completed", phase: "training" }).outcomeTitle, "Training completed");
});
test("reported counts cannot become a clean validation through omitted details", () => {
  const report = validationPresentation({ blocking_error_count: 4, blocking_errors: [], warning_count: 1 });
  assert.equal(report.clean, false);
  assert.equal(report.blockingCount, 4);
  assert.equal(report.warningCount, 1);
});
test("actual issue rows take precedence over contradictory zero counts", () => {
  const report = validationPresentation({ blocking_error_count: 0, errors: [{ message: "Missing assistant target" }] });
  assert.equal(report.blockingCount, 1);
  assert.equal(report.clean, false);
  assert.equal(validationPresentation({ records_checked: 10, blocking_error_count: 0, warning_count: 0 }).clean, true);
});
