import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("model reasoning opens in the dedicated Cooking activity pane", async () => {
  const page = await readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8");
  const message = await readFile(new URL("./ChatMessage.jsx", import.meta.url), "utf8");
  const panel = await readFile(new URL("./CookingActivityPanel.jsx", import.meta.url), "utf8");
  const activity = await readFile(new URL("../workflows/activityJournal.mjs", import.meta.url), "utf8");

  assert.match(page, /chat-page--activity-open/);
  assert.match(page, /<CookingActivityPanel/);
  assert.match(page, /<CookingStatus[\s\S]*busy/);
  assert.match(message, /Cooking paused before the final answer/);
  assert.match(message, /: "Cooked"/);
  assert.match(message, /savedActivity/);
  assert.match(message, /activityComplete/);
  assert.match(message, /"Research activity"/);
  assert.match(message, /"Response activity"/);
  assert.match(message, /presentAssistantContent/);
  assert.match(message, /reasoningIncomplete/);
  assert.match(
    message,
    /const degradedOutput = assistant\s*&& !actionProposal\s*&& !generatedImage\s*&& looksDegradedOutput/,
  );
  assert.doesNotMatch(message, /<summary>Reasoning<\/summary>/);
  assert.match(panel, /aria-label=\{panelLabel\}/);
  assert.match(panel, /title\.toLowerCase\(\)\.endsWith\("activity"\)/);
  assert.match(panel, /className="activity-source"/);
  assert.match(panel, /details\?\.web_search/);






  assert.doesNotMatch(panel, /Live cooking trace/);
  assert.doesNotMatch(panel, /Live model output/);
  assert.doesNotMatch(panel, /Model reasoning/);
  assert.doesNotMatch(panel, /<RichText/);
  assert.match(panel, /Activity journal/);
  assert.match(panel, /"Researching"/);
  assert.match(panel, /"Research activity"/);
  assert.match(panel, /Technical details/);
  assert.match(panel, /Raw local trace \(developer\)/);
  assert.match(panel, /reasoningVisibility === "raw_local"/);
  assert.match(panel, /activityEntryTelemetry/);
  assert.match(activity, /character_count/);
  assert.match(panel, /activityTelemetry\(operation, details\)/);
  assert.match(panel, /preview\?\.tail_text/);
  assert.match(panel, /generation_preview/);
  assert.match(panel, /token_count/);
  for (const metric of ["Input", "Output", "Output limit", "Elapsed"]) {
    assert.match(panel, new RegExp(`label: \\"${metric}\\"`));
  }
  assert.match(panel, /formatTokenCount/);
  assert.match(page, /cooking-activity-backdrop/);
  assert.match(page, /const closeCookingActivity = useCallback/);
  assert.match(page, /onClick=\{showAgentActivity/);
  assert.match(page, /: closeCookingActivity\}/);
  assert.match(page, /onClose=\{closeCookingActivity\}/);
  assert.match(page, /className="message-generating__instant"/);
  assert.doesNotMatch(page, /activeReasoningMode === "cooking"\s*\n\s*: Boolean/);
  assert.match(
    page,
    /const toggleCookingActivity[\s\S]*?setResponseDetails\(null\)/,
  );
  assert.match(
    page,
    /onOpenDetails=\{\(details\) => \{[\s\S]*?setCookingActivityMessageId\(null\)[\s\S]*?setResponseDetails\(details\)/,
  );
  assert.match(
    page,
    /useEffect\(\(\) => \{\s*if \(agentRunning\) setResponseDetails\(null\)/,
  );
});

test("chat transcript renders model output as safe rich text and exposes polished states", async () => {
  const page = await readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8");
  const message = await readFile(new URL("./ChatMessage.jsx", import.meta.url), "utf8");

  assert.match(message, /<RichText className="message__content">/);
  assert.match(message, /message--pending/);
  assert.match(page, /addNotification: notify/);
  assert.doesNotMatch(page, /setLocalError\(/);
  assert.match(page, /notify\(\{ message: errorMessage\(error\), kind: "error" \}\)/);
  assert.doesNotMatch(page, /chat-error-banner/);
  assert.match(message, /requestNativeSave/);
  assert.match(message, /type: "save_file"/);
  assert.match(message, /source: "url"/);
  assert.match(message, /source: "text"/);
  assert.match(message, /details\?\.code_file_artifacts_allowed === true/);
  assert.doesNotMatch(message, /<a href=\{generatedImage\.url\}/);
  assert.match(page, /Opening conversation/);
  assert.doesNotMatch(page, /PRIVATE · LOCAL · YOUR COMPUTER/);
});

test("Cooking activity uses factual backend-driven stages and one reduced-motion-safe light sweep", async () => {
  const [page, panel, styles] = await Promise.all([
    readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8"),
    readFile(new URL("./CookingActivityPanel.jsx", import.meta.url), "utf8"),
    readFile(new URL("../styles/chat-cooking.css", import.meta.url), "utf8"),
  ]);

  for (const label of ["Waiting to respond", "Searching sources", "Preparing runtime", "Preparing response", "Finishing response", "Cooking"]) {
    assert.match(`${page}\n${panel}`, new RegExp(label));
  }
  assert.doesNotMatch(styles, /salty-cooking-pulse/);
  assert.doesNotMatch(styles, /cooking-glyph--animated/);
  assert.doesNotMatch(styles, /salty-cooking-turn/);
  assert.match(styles, /@keyframes salty-activity-sweep/);
  assert.match(styles, /prefers-reduced-motion: reduce/);
  assert.match(styles, /\.activity-phrase[\s\S]*?animation: salty-activity-sweep/);
  assert.match(styles, /prefers-reduced-motion: reduce[\s\S]*?\.activity-phrase[\s\S]*?animation: none/);
  assert.doesNotMatch(styles, /@keyframes salty-activity-sweep[\s\S]*?opacity\s*:/);
  assert.match(page, /cookingActivityMessageId === "active"[\s\S]*?activity-phrase activity-phrase--response/);
  assert.doesNotMatch(panel, /cooking-activity__live" role="status"/);
  assert.match(panel, /active \? stage : summary.label/);
});

test("response Activity is event-driven and never starts from a fixed 14-step script", async () => {
  const service = await readFile(
    new URL("../../../backend/chat/service.py", import.meta.url),
    "utf8",
  );

  assert.match(service, /activity_journal:\s*list\[dict\[str, Any\]\]\s*=\s*\[\]/);
  assert.match(service, /activity_journal/);
  assert.doesNotMatch(service, /visible_events\s*=\s*\{/);
  assert.match(service, /checked_state not in \{/);
  assert.match(service, /context\.update\(phase=entry\["label"\], details=relationship\)/);
  assert.doesNotMatch(service, /label="Understanding the request"/);
  assert.doesNotMatch(service, /label="Organizing the response"/);
  assert.doesNotMatch(service, /label="Recording response evidence"/);
  assert.doesNotMatch(service, /"sequence":\s*14/);
  assert.doesNotMatch(service, /activity_journal:\s*list\[dict\[str, Any\]\]\s*=\s*\[\s*\{/);
});

test("response telemetry and current work use flat editorial rules, not cards", async () => {
  const styles = await readFile(new URL("../styles/chat-cooking.css", import.meta.url), "utf8");

  assert.match(
    styles,
    /\.cooking-activity__metrics\s*\{[^}]*border-top:\s*1px[^}]*border-bottom:\s*1px/s,
  );
  assert.match(
    styles,
    /\.cooking-activity__metrics > div\s*\{[^}]*background:\s*transparent[^}]*border:\s*0[^}]*border-radius:\s*0/s,
  );
  assert.match(
    styles,
    /\.cooking-activity__current\s*\{[^}]*background:\s*transparent[^}]*border:\s*0[^}]*border-left:\s*2px[^}]*border-radius:\s*0[^}]*box-shadow:\s*none/s,
  );
});

test("Activity uses the shared full-height workspace panel instead of a second grid row", async () => {
  const [page, styles] = await Promise.all([
    readFile(new URL("../pages/ChatPage.jsx", import.meta.url), "utf8"),
    readFile(new URL("../styles/approved-workspace.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(page, /CodeWorkspacePanel/);
  assert.match(styles, /grid-template-rows:minmax\(0,1fr\)/);
  assert.match(styles, /grid-column:3; grid-row:1/);
  assert.doesNotMatch(page, /sharedMediumWorkspace/);
});
