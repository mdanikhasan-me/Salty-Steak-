import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  AUTOMATION_SCHEMA,
  COMPUTER_CONTROL_CAPABILITIES,
  automationResultSummary,
  createComputerControlAdapter,
  fullAccessCapabilities,
  fullAccessGrantRequest,
  grantRequest,
  hasComputerControlAdapter,
  normaliseAutomationAudit,
  normaliseAutomationStatus,
  prepareScreenCaptureInvocation,
  prepareTerminalInvocation,
  revokeRequest,
  unavailableAutomationStatus,
} from "./computerControlPermissions.mjs";

const noop = async () => ({});
const adapter = createComputerControlAdapter({
  getStatus: noop,
  grant: noop,
  revoke: noop,
  invoke: noop,
  getAudit: noop,
});

function statusFixture(overrides = {}) {
  return {
    schema: AUTOMATION_SCHEMA,
    platform: "windows",
    local_only: true,
    external_service_required: false,
    starts_network_service: false,
    default_enabled: false,
    explicit_persisted_user_grant_required: true,
    capabilities: [
      {
        id: "terminal.execute",
        capability: "terminal.execute",
        display_name: "Terminal command",
        granted: false,
        constraints: { working_directory_root: "D:\\Projects\\Salty Steak" },
        granted_at: null,
        revoked_at: null,
        platform_supported: true,
        constraint_valid: true,
        constraint_error: null,
        effective_enabled: false,
      },
      {
        id: "screen.capture",
        capability: "screen.capture",
        display_name: "Primary-screen screenshot",
        granted: false,
        constraints: {
          screen: "primary",
          output_root: "D:\\Projects\\Salty Steak\\workspace\\automation\\screenshots",
        },
        granted_at: null,
        revoked_at: null,
        platform_supported: true,
        constraint_valid: true,
        constraint_error: null,
        effective_enabled: false,
      },
    ],
    active_invocations: [],
    audit_record_count: 0,
    limits: {
      max_timeout_seconds: 30,
      max_arguments: 128,
      max_argument_characters: 32768,
    },
    risk_boundaries: {
      argv_only: true,
      subprocess_shell_enabled: false,
      filesystem_sandbox_enforced: false,
      screen_capture_visibility_indicator: false,
    },
    ...overrides,
  };
}

test("the adapter maps exactly the five real automation operations", () => {
  assert.equal(hasComputerControlAdapter(adapter), true);
  assert.equal(hasComputerControlAdapter(null), false);
  assert.deepEqual(Object.keys(adapter), ["getStatus", "grant", "revoke", "invoke", "getAudit"]);
  assert.throws(() => createComputerControlAdapter({ grant: noop }), /requires getStatus/);
});

test("backend status is schema-checked and disabled by default", () => {
  const status = normaliseAutomationStatus(statusFixture());
  assert.deepEqual(COMPUTER_CONTROL_CAPABILITIES.map((item) => item.id), [
    "terminal.execute",
    "files.manage",
    "screen.capture",
    "input.control",
    "application.launch",
    "browser.control",
    "ui.automation",
    "window.control",
    "discord.inspect",
  ]);
  assert.equal(status.platform, "windows");
  assert.equal(status.capabilities[0].stateLabel, "Not granted");
  assert.equal(status.capabilities[0].granted, false);
  assert.equal(status.capabilities[0].effectiveEnabled, false);
  assert.match(status.capabilities[0].detail, /not a filesystem or network sandbox/);
  assert.match(
    status.capabilities.find((item) => item.id === "screen.capture").detail,
    /No visible capture indicator/,
  );

  assert.throws(() => normaliseAutomationStatus({
    ...statusFixture(),
    default_enabled: true,
  }), /fail-closed contract/);
  assert.throws(() => normaliseAutomationStatus({
    ...statusFixture(),
    external_service_required: true,
  }), /fail-closed contract/);
  assert.throws(() => normaliseAutomationStatus({
    ...statusFixture(),
    schema: "unknown",
  }), /schema is unsupported/);
});

test("only a reported effective grant unlocks invocation", () => {
  const fixture = statusFixture();
  fixture.capabilities[0] = {
    ...fixture.capabilities[0],
    granted: true,
    effective_enabled: true,
    granted_at: "2026-08-12T12:00:00Z",
  };
  const granted = normaliseAutomationStatus(fixture).capabilities[0];
  assert.equal(granted.stateLabel, "Granted");
  assert.equal(granted.actionLabel, "Run command");
  assert.equal(granted.effectiveEnabled, true);

  fixture.capabilities[0].effective_enabled = false;
  const inconsistent = normaliseAutomationStatus(fixture).capabilities[0];
  assert.equal(inconsistent.stateLabel, "Needs attention");
  assert.equal(inconsistent.effectiveEnabled, false);

  fixture.capabilities[0].granted = false;
  fixture.capabilities[0].effective_enabled = true;
  const impossible = normaliseAutomationStatus(fixture).capabilities[0];
  assert.equal(impossible.stateLabel, "Not granted");
  assert.equal(impossible.effectiveEnabled, false);
});

test("grant and revoke requests match the backend schema exactly", () => {
  assert.deepEqual(grantRequest("terminal.execute", "D:\\Projects\\Salty Steak\\work"), {
    capabilities: ["terminal.execute"],
    user_confirmed: true,
    working_directory_root: "D:\\Projects\\Salty Steak\\work",
  });
  assert.deepEqual(grantRequest("screen.capture"), {
    capabilities: ["screen.capture"],
    user_confirmed: true,
  });
  assert.deepEqual(revokeRequest("terminal.execute"), {
    capabilities: ["terminal.execute"],
  });
  assert.throws(() => grantRequest("terminal.execute", ""), /working directory root is invalid/);
});

test("full access includes newly shipped file and Discord capabilities", () => {
  const fixture = statusFixture();
  fixture.capabilities.push(
    {
      id: "files.manage",
      capability: "files.manage",
      granted: true,
      constraints: { scope: "user_authorised_paths" },
      platform_supported: true,
      runtime_available: true,
      constraint_valid: true,
      effective_enabled: true,
    },
    {
      id: "discord.inspect",
      capability: "discord.inspect",
      granted: false,
      constraints: { scope: "signed_in_discord_read_navigation" },
      platform_supported: true,
      runtime_available: true,
      constraint_valid: true,
      effective_enabled: false,
    },
  );
  const status = normaliseAutomationStatus(fixture);
  const available = fullAccessCapabilities(status);
  assert.equal(available.some((item) => item.id === "files.manage" && item.alreadyGranted), true);
  assert.equal(available.some((item) => item.id === "discord.inspect" && !item.alreadyGranted), true);
  assert.deepEqual(fullAccessGrantRequest(status)?.capabilities, [
    "terminal.execute",
    "files.manage",
    "screen.capture",
    "discord.inspect",
  ]);
});

test("full access can refresh stale scopes while keeping unavailable helpers excluded", () => {
  const fixture=statusFixture();
  fixture.capabilities[0]={...fixture.capabilities[0],granted:true,constraint_valid:false,constraint_error:"Old installation path",effective_enabled:false};
  fixture.capabilities[1]={...fixture.capabilities[1],runtime_available:false};
  const status=normaliseAutomationStatus(fixture);
  const terminal=status.capabilities.find(item=>item.id==="terminal.execute");
  assert.equal(terminal.available,true);assert.equal(terminal.effectiveEnabled,false);assert.equal(terminal.actionLabel,"Refresh access");
  assert.deepEqual(fullAccessGrantRequest(status).capabilities,["terminal.execute"]);
});

test("terminal invocation accepts argv only and creates an exact review", () => {
  const review = prepareTerminalInvocation({
    argvText: '["python.exe","-c","print(1)"]',
    workingDirectory: "D:\\Projects\\Salty Steak",
    timeoutSeconds: "10",
  }, {
    maxArguments: 128,
    maxArgumentCharacters: 32768,
    maxTimeoutSeconds: 30,
  });
  assert.deepEqual(review, {
    capability: "terminal.execute",
    arguments: {
      argv: ["python.exe", "-c", "print(1)"],
      working_directory: "D:\\Projects\\Salty Steak",
      timeout_seconds: 10,
    },
    summary: 'argv ["python.exe","-c","print(1)"]; working directory D:\\Projects\\Salty Steak; timeout 10 seconds',
  });
  assert.throws(() => prepareTerminalInvocation({
    argvText: "echo unsafe",
    workingDirectory: "D:\\Projects\\Salty Steak",
    timeoutSeconds: 10,
  }), /valid JSON array/);
  assert.throws(() => prepareTerminalInvocation({
    argvText: '"echo unsafe"',
    workingDirectory: "D:\\Projects\\Salty Steak",
    timeoutSeconds: 10,
  }), /must contain 1 to 128 items/);
});

test("screen capture is fixed to the primary screen and app-owned output root", () => {
  const status = normaliseAutomationStatus(statusFixture());
  const screenCapture = status.capabilities.find((item) => item.id === "screen.capture");
  const review = prepareScreenCaptureInvocation(screenCapture);
  assert.deepEqual(review.arguments, { screen: "primary" });
  assert.match(review.summary, /primary screen/);
  assert.match(review.summary, /workspace\\automation\\screenshots/);
  assert.throws(
    () => prepareScreenCaptureInvocation(status.capabilities[0]),
    /requires screen\.capture/,
  );
});

test("audit and result summaries expose actual broker outcomes", () => {
  assert.deepEqual(normaliseAutomationAudit([{
    id: "audit-1",
    event: "invoke",
    capability: "terminal.execute",
    outcome: "failed",
    created_at: "2026-08-12T12:00:00Z",
    completed_at: "2026-08-12T12:00:01Z",
  }]), [{
    id: "audit-1",
    event: "invoke",
    capability: "terminal.execute",
    outcome: "failed",
    createdAt: "2026-08-12T12:00:00Z",
    completedAt: "2026-08-12T12:00:01Z",
  }]);
  assert.match(automationResultSummary({
    capability: "terminal.execute",
    status: "succeeded",
    exit_code: 0,
    audit_record_id: "audit-2",
  }), /succeeded; exit code 0; audit audit-2/);
});

test("unavailable status never invents a grant or broker capability", () => {
  const status = unavailableAutomationStatus("Local service could not be reached.");
  assert.equal(status.capabilities.length, 9);
  assert.equal(status.capabilities.every((item) => !item.available && !item.granted), true);
  assert.equal(status.capabilities.every((item) => item.stateLabel === "Unavailable"), true);
});

test("UI and API source wire real endpoints with separate grant and invocation confirmations", async () => {
  const [component, workflow, client, page] = await Promise.all([
    readFile(new URL("../components/ComputerControlPermissions.jsx", import.meta.url), "utf8"),
    readFile(new URL("./computerControlPermissions.mjs", import.meta.url), "utf8"),
    readFile(new URL("../api/client.js", import.meta.url), "utf8"),
    readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8"),
  ]);
  for (const endpoint of ["status", "grant", "revoke", "invoke", "audit"]) {
    assert.match(client, new RegExp(`/automation/${endpoint}`));
  }
  for (const method of ["getAutomationStatus", "grantAutomation", "revokeAutomation", "invokeAutomation", "getAutomationAudit"]) {
    assert.match(page, new RegExp(`api\\.${method}`));
  }
  assert.match(workflow, /user_confirmed: true/);

  assert.match(component, /Review action/);
  assert.match(component, /confirmInvocation/);
  assert.match(component, /Only the exact arguments shown above will be sent/);
  assert.match(component, /adapter\.invoke\(\{/);
  assert.match(component, /proposedInvocation\.kind !== "terminal\.execute"/);
  assert.match(component, /status\.schema === "unavailable"/);
  assert.match(component, /user_confirmed: true/);
  assert.match(component, /setGrantReview/);
  assert.match(component, /setInvocationReview/);
  assert.match(component, /const refreshingStaleGrant = item\.granted && item\.actionLabel === "Refresh access"/);
  assert.match(component, /item\.granted && !item\.effectiveEnabled && !refreshingStaleGrant/);
  assert.match(component, /item\.granted && item\.effectiveEnabled \? beginInvocation\(item\) : beginGrant\(item\)/);
  assert.doesNotMatch(component, /prepareGrant|prepareInvocation|scope_token|invocation_token/);
});
