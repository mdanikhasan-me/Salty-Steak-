const TOPICS = {
  command_line: "Considering the command-line interface",
  input: "Considering input handling",
  validation: "Considering validation rules",
  edge_cases: "Considering errors and edge cases",
  checks: "Planning checks and examples",
  implementation: "Organizing the implementation",
  evidence: "Considering the available evidence",
  calculation: "Working through the calculation",
  reasoning: "Thinking",
};

export function liveProgress(operation, previous = null, now = null) {
  if (!operation?.id) return null;
  const details = operation.result || operation.progress || {};
  const research = details.research_progress;
  const preview = research?.generation_preview || details.generation_preview || {};
  const verification = details.lock_in_verification;
  const started = Date.parse(operation.started_at);
  const updated = Date.parse(operation.updated_at);
  const running = ["queued", "running", "stop_requested"].includes(operation.state);
  const elapsed = Math.max(0, running && Number.isFinite(now) && Number.isFinite(started)
    ? (now - started) / 1000 : Number(details.elapsed_seconds)
      || ((updated - started) / 1000) || 0);
  const operationPhase = String(operation.phase || "").trim().slice(0, 160);
  const journal = details.activity_journal || [];
  const event = Array.isArray(journal) ? journal.findLast(item => item?.label === operationPhase)
    || journal.findLast(item => item?.state === "running") : null;
  const checking = ["checking", "repairing", "testing", "planning", "reviewing"].includes(verification?.status);
  const engineStage = operationPhase && !/^(generating response|analyzing the response|writing the answer)$/i.test(operationPhase);
  const thinking = !checking && !engineStage && preview.kind === "reasoning";
  const writing = !checking && !engineStage && preview.kind === "output";
  const phase = checking ? `verification-${verification.status}` : thinking ? "reasoning" : writing ? "output"
    : research ? `research-${research.phase || 'working'}` : operationPhase || "waiting";
  const researchTitle = ({searching:"Looking for sources",reading:"Reading sources",
    comparing:"Comparing the evidence",verifying:"Checking source support",
    search_failed:"Source search unavailable",completed:"Preparing the researched answer"})[research?.phase] || "Research in progress";
  const key = checking ? `${phase}:${verification.name || operationPhase}` : thinking ? (Object.hasOwn(TOPICS, preview.focus) ? preview.focus : "reasoning")
    : writing ? "answer" : research ? phase : operationPhase || "waiting";
  const title = checking ? verification.status === "testing" && verification.name
    ? `Running: ${String(verification.name).slice(0,120)}` : operationPhase || "Checking the answer"
    : thinking ? TOPICS[key] : writing ? "Writing the answer" : research ? researchTitle
    : operationPhase || "Waiting for a progress update";
  const telemetry = checking ? verification : preview;
  const tokens = Math.max(0, Number(telemetry.stream_token_count ?? telemetry.token_count) || 0);
  let body = "";
  if (checking) {
    if (verification.status === "testing") body = `Check ${verification.index || 1} of ${verification.total || 1}`;
    else if (tokens > 0) body = `${tokens.toLocaleString()} tokens generated in this ${verification.status === "planning" ? "test-planning" : verification.status === "reviewing" ? "review" : "revision"} pass`;
    else body = String(event?.detail || "").slice(0,400);
  } else if (thinking && tokens > 0) body = `${tokens.toLocaleString()} reasoning tokens generated`;
  else if (writing && tokens > 0) body = `${tokens.toLocaleString()} answer-pass tokens generated`;
  else if (research?.phase === "search_failed") body = "The source search could not finish. Activity has the details.";
  else if (event?.detail) body = String(event.detail).slice(0,400);
  const same = previous?.operationId === operation.id;
  let history = same ? [...previous.history] : [];
  let current = same ? previous.current : null;
  const stream = checking ? phase : String(preview.stream_id || "primary");
  const rawSpeed = telemetry.decode_tokens_per_second;
  const speed = rawSpeed !== null && rawSpeed !== undefined && Number.isFinite(Number(rawSpeed)) && Number(rawSpeed) >= 0
    ? Number(rawSpeed) : null;
  const canChange = !current || current.key === key || previous?.phase !== phase || previous?.stream !== stream
    || tokens < (previous?.tokens || 0) || !thinking || tokens - current.sinceTokens >= 48;
  if (canChange && current?.key !== key) {
    if (current && current.key !== "preparing") history.push({key:current.key,title:current.title});
    history = history.filter((item, i, all) => all.findLastIndex(other => other.key === item.key) === i).slice(-3);
    current = {key,title,sinceTokens:tokens};
  }
  if (current && same && (tokens < previous.tokens || previous.stream !== stream)) current = {...current,sinceTokens:tokens};
  if (canChange && current?.key === key && current.title !== title) current = {...current,title};
  const focusNote = thinking && body ? {title:current.title,description:body,specific:current.key!=="reasoning"} : null;
  return {operationId:operation.id,current,history,body,thinking,writing,phase,stream,elapsed,tokens,speed,
    focusNote,
    reasoningOnly: thinking && elapsed >= 120,
    updatedAt: operation.updated_at || null};
}
