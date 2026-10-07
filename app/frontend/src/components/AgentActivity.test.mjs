import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const panel = readFileSync(
  fileURLToPath(new URL("./AgentActivityPanel.jsx", import.meta.url)),
  "utf8",
);
const styles = readFileSync(
  fileURLToPath(new URL("../styles/agent-activity.css", import.meta.url)),
  "utf8",
);
const page = readFileSync(
  fileURLToPath(new URL("../pages/ChatPage.jsx", import.meta.url)),
  "utf8",
);
const tokens = readFileSync(
  fileURLToPath(new URL("../styles/tokens-base.css", import.meta.url)),
  "utf8",
);

test("Activity speaks to a person before it speaks to a developer", () => {


  assert.match(panel, /Working on your request/);
  assert.match(panel, /Confirming it worked/);
  assert.match(panel, /aria-expanded=\{showDetail\}/);
  assert.match(panel, /Technical details/);
  const disclosureAt = panel.indexOf("Technical details");
  const metricsAt = panel.indexOf("Model calls");
  assert.ok(disclosureAt > 0 && metricsAt > disclosureAt);
});

test("a total is only claimed when a plan really declares one", () => {


  assert.match(panel, /planned_step_count/);
  assert.match(panel, /if \(!completed\) return running \? "Starting" : "";/);
});

test("waiting states name the thing the user has to do", () => {
  for (const reason of [
    "user_sign_in",
    "user_approval",
    "user_answer",
    "user_2fa",
    "ambiguous_destructive_choice",
  ]) {
    assert.ok(panel.includes(reason), `${reason} has no copy`);
  }
  assert.match(panel, /Sign in to continue/);
});

test("Stop disables itself the moment it is pressed", () => {


  assert.match(panel, /disabled=\{stopping\}/);
  assert.match(panel, /Stopping\.\.\./);
  assert.match(styles, /\.agent-activity__stop\[disabled\]/);
});

test("an approval record states what happens, where, and how many", () => {
  assert.match(panel, /approval\.summary/);
  assert.match(panel, /approval\.resource/);
  assert.match(panel, /item_count/);
  assert.match(panel, /approval\.reason/);
  assert.match(panel, /onApprove/);
  assert.match(panel, /onReject/);
});

test("approving is not made the easy path", () => {
  assert.match(styles, /\.agent-approval__reject \{[^}]*border: 1px solid/);
  assert.match(styles, /\.agent-approval__approve \{/);
});

test("status is never carried by colour alone", () => {
  assert.match(panel, /Confirmed<\/small>/);
  assert.match(styles, /\.agent-step__verified/);
});

test("every animation is behind a reduced-motion guard", () => {
  const guards = styles.match(/@media \(prefers-reduced-motion: no-preference\)/g) || [];
  const transitions = styles.match(/transition:/g) || [];
  assert.ok(guards.length >= 3, "expected motion to be guarded");

  const unguarded = styles
    .split("@media (prefers-reduced-motion: no-preference)")
    .shift()
    .match(/transition:/g);
  assert.equal(unguarded, null, "a transition sits outside the reduced-motion guard");
  assert.ok(transitions.length >= 1);
});

test("the design system declares scales rather than scattering values", () => {
  for (const token of [
    "--space-3",
    "--text-base",
    "--text-base-leading",
    "--weight-strong",
    "--radius-xl",
    "--elevation-2",
    "--mono-font",
  ]) {
    assert.ok(tokens.includes(token), `${token} is missing from the token scale`);
  }
});

test("elapsed and counted values do not shift the layout as they change", () => {
  const tabular = styles.match(/font-variant-numeric: tabular-nums/g) || [];
  assert.ok(tabular.length >= 3);
});

test("live Activity shows measured planning counts and the real mission budget", () => {
  assert.match(panel, /generation_preview/);
  assert.match(panel, /planning_output_tokens/);
  assert.match(panel, /mission_budget/);
  assert.match(panel, /Total output tokens/);
  assert.match(panel, /Current decision tokens/);
  assert.doesNotMatch(panel, /tokens this decision/);
  assert.doesNotMatch(panel, /mission tokens/);
  assert.match(panel, /Mission time limit/);
  assert.match(panel, /Decision guard/);
  assert.doesNotMatch(panel, /preview\.tail_text/);
  assert.match(styles, /\.agent-activity__live/);
});

test("Activity is one flat instrument surface rather than nested cards", () => {
  assert.match(panel, /agent-activity__live-metrics/);
  assert.match(
    styles,
    /\.agent-activity__live\s*\{[^}]*background:\s*transparent[^}]*border-left:\s*2px[^}]*border-radius:\s*0[^}]*box-shadow:\s*none/s,
  );
  assert.match(
    styles,
    /\.agent-activity__group-marker\s*\{[^}]*background:\s*transparent[^}]*border:\s*0[^}]*border-radius:\s*0/s,
  );
  assert.match(
    styles,
    /\.agent-activity__waiting,\s*\.agent-approval\s*\{[^}]*background:\s*transparent[^}]*border:\s*0[^}]*border-left:\s*2px[^}]*border-radius:\s*0[^}]*box-shadow:\s*none/s,
  );
});

test("agent and Cooking activity share one non-overlapping workspace", () => {
  assert.match(page, /const showAgentActivity/);
  assert.match(page, /const showCookingActivity/);
  assert.match(page, /const activityWorkspaceOpen = showAgentActivity \|\| showCookingActivity/);
  assert.match(page, /showAgentActivity \? \([\s\S]*?<AgentActivityPanel[\s\S]*?: \([\s\S]*?<CookingActivityPanel/);
  assert.equal((page.match(/<AgentActivityPanel/g) || []).length, 1);
  assert.doesNotMatch(styles, /\.agent-activity\s*\{[^}]*position:\s*fixed/);
  assert.match(styles, /\.agent-activity\s*\{[\s\S]*?border-left:/);
});

test("Activity groups repeated implementation steps into human-readable phases", () => {
  assert.match(panel, /groupActivitySteps/);
  assert.match(panel, /Worked in Discord/);
  assert.match(panel, /What happened/);
  assert.match(panel, /agent-activity__section-label">Now/);
  assert.match(panel, /Show \$\{count\} steps/);
  assert.doesNotMatch(panel, />\{step\.action\}</);
});

test("the panel animates only compositor-friendly properties", () => {


  const animated = [...styles.matchAll(/transition:\s*([^;]+);/g)].map((match) => match[1]);
  for (const declaration of animated) {
    assert.ok(
      !/\b(width|height|top|left|filter|box-shadow)\b/.test(declaration),
      `layout-triggering property animated: ${declaration}`,
    );
  }
});
