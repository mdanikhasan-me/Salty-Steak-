import assert from "node:assert/strict";
import test from "node:test";

import {
  CONNECTOR_DEFINITIONS,
  connectorPresentation,
  normaliseConnector,
} from "./pluginConnections.mjs";

test("external plugins default to disconnected", () => {
  assert.equal(CONNECTOR_DEFINITIONS.length, 3);
  assert.equal(normaliseConnector().status, "disconnected");
  assert.deepEqual(connectorPresentation(), {
    stateLabel: "Not connected",
    actionLabel: "Connect",
    action: "connect",
    disabled: false,
  });
});

test("configured, degraded, error, attention, and unavailable states produce truthful actions", () => {
  assert.equal(connectorPresentation("configured").actionLabel, "Manage");
  assert.equal(connectorPresentation("degraded").stateLabel, "Limited");
  assert.equal(connectorPresentation("error").stateLabel, "Connection failed");
  assert.equal(connectorPresentation("attention").actionLabel, "Review");
  assert.deepEqual(connectorPresentation("unavailable"), {
    stateLabel: "Unavailable",
    actionLabel: "Unavailable",
    action: "none",
    disabled: true,
  });
});

test("busy connection state cannot be submitted twice", () => {
  assert.equal(connectorPresentation("disconnected", true).disabled, true);
  assert.equal(connectorPresentation("disconnected", true).actionLabel, "Connecting…");
});
