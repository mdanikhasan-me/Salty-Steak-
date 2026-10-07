import assert from "node:assert/strict";
import test from "node:test";



globalThis.__SALTY_POTATO_BUILD_ID__ = "development";

const { ApiError, assertBuildIdentity } = await import("./client.js");

test("matching frontend and backend build identities are accepted", () => {
  assert.equal(assertBuildIdentity("r4", "r4"), "r4");
});

test("a frontend/backend build mismatch is rejected without fallback", () => {
  assert.throws(
    () => assertBuildIdentity("backend-r4", "frontend-r3"),
    (error) =>
      error instanceof ApiError
      && error.code === "build_identity_mismatch"
      && error.details.frontend_build_id === "frontend-r3"
      && error.details.backend_build_id === "backend-r4",
  );
});
