import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Copy,
  Download,
  FileCode2,
  RotateCcw,
} from "lucide-react";
import { formatDuration, formatNumber } from "../workflows/formatters.js";
import {
  completionState,
  responseDetails,
  sourceMarks,
  visibleOutputTokens,
} from "../workflows/responseProvenance.mjs";
import { usedSources } from "../workflows/claimEvidence.mjs";
import { presentAssistantContent } from "../workflows/chatContent.mjs";
import { generatedImageForMessage } from "../workflows/generatedImages.mjs";
import {
  hostActionProposalForMessage,
  hostSafeAssistantContent,
} from "../workflows/hostActions.mjs";
import { CookingStatus } from "./CookingStatus.jsx";
import { AgentTimeline } from "./AgentTimeline.jsx";
import { ActionTimeline } from './ActionTimeline.jsx';
import { executionEvents, timelineFor } from "../workflows/agentTimeline.mjs";
import { HostActionProposal } from "./HostActionProposal.jsx";
import { RichText } from "./RichText.jsx";
import { SiteIcon } from "./SiteIcon.jsx";
import { generationFailureForMessage } from "../workflows/generationFailure.mjs";
import { codeArtifactsFromText, normaliseNamedCodeFences } from "../workflows/codeArtifacts.mjs";
import "../styles/generated-image-recovery.css";

export function ChatMessage({
  message,
  workspaceMode = 'chat',
  copied,
  onCopy,
  retryEligible,
  retryBusy,
  onRetry,
  cookingOpen = false,
  onOpenCooking,
  onReviewAction,
  actionOperation = null,
  actionBusy = false,
  onConfirmAction,
  onStopAction,
  onOpenDetails,
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [actionsVisible, setActionsVisible] = useState(false);
  const hideTimerRef = useRef(null);
  const assistant = message.role === "assistant";
  const details = message.technical_details || message.details;



  const agentEvents = assistant
    ? executionEvents(timelineFor({ messageDetails: details }))
    : [];
  const duration = assistant ? thoughtDuration(details) : null;
  const reasoningMode = assistant ? messageReasoningMode(details) : "";
  const actionProposal = assistant ? hostActionProposalForMessage(message) : null;
  const generatedImage = assistant ? generatedImageForMessage(message) : null;
  const [actionDismissed, setActionDismissed] = useState(false);


  const visibleContent = assistant
    ? presentAssistantContent(
      hostSafeAssistantContent(message, actionProposal, actionOperation),
      reasoningMode,
    )
    : { answer: String(message.content || "").trim(), reasoning: "", cookingTurn: false };
  const cookingTurn = assistant
    && visibleContent.cookingTurn
    && !actionProposal
    && !generatedImage;
  const savedActivity = assistant
    && !actionProposal
    && !generatedImage
    && [
      details?.orchestration?.activity_journal,
      details?.activity_journal,
    ].some((journal) => Array.isArray(journal) && journal.length > 0);
  const researchActivity = Boolean(
    details?.orchestration?.kind === "research"
      || details?.research_progress
      || details?.orchestration?.research,
  );
  const activityComplete = ["done", "cooked", "zonted"].includes(
    completionState(details),
  );


  const degradedOutput = assistant
    && !actionProposal
    && !generatedImage
    && looksDegradedOutput(visibleContent.answer);
  const generationFailure = generationFailureForMessage(message);
  const codeArtifacts = assistant && details?.code_file_artifacts_allowed === true
    ? codeArtifactsFromText(visibleContent.answer)
    : [];

  useEffect(() => () => window.clearTimeout(hideTimerRef.current), []);

  function showActions() {
    window.clearTimeout(hideTimerRef.current);
    setActionsVisible(true);
  }

  function scheduleHideActions() {
    window.clearTimeout(hideTimerRef.current);
    hideTimerRef.current = window.setTimeout(() => setActionsVisible(false), 170);
  }

  return (
    <article
      className={`message message--${assistant ? "assistant" : "user"} ${
        actionsVisible ? "message--actions-visible" : ""
      } ${message.pending ? "message--pending" : ""}`}
      aria-busy={message.pending || undefined}
      tabIndex="0"
      onPointerEnter={showActions}
      onPointerLeave={scheduleHideActions}
      onFocusCapture={showActions}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) scheduleHideActions();
      }}
    >
      <div
        className={`message__avatar ${
          assistant ? "message__avatar--assistant" : "message__avatar--user"
        }`}
        aria-hidden="true"
      >
        {assistant ? (
          <img
            src="/assets/salty-potato-symbol.svg"
            alt=""
            width="24"
            height="24"
          />
        ) : (
          <span>Y</span>
        )}
      </div>
      <div className="message__body">
        <header className="message__header">
          <strong>{assistant ? "Salty Steak" : "You"}</strong>
          {message.created_at ? (
            <time dateTime={message.created_at}>{messageTime(message.created_at)}</time>
          ) : null}
        </header>
        <ActionTimeline details={details || {}} workspaceMode={workspaceMode}/>
        {workspaceMode === 'chat' && agentEvents.length && !details?.image_sha256 ? (
          <AgentTimeline
            events={agentEvents}
            artifactSource={null}


            {...(({ sites, pages, marks }) => ({
              sites,
              pages,
              siteList: marks,
            }))(sourceMarks(message?.technical_details, { limit: 40 }))}
            state={completionState(message?.technical_details)}
          />
        ) : null}
        <div className="message__surface">
          {cookingTurn || savedActivity ? (
            <CookingStatus
              label={cookingTurn
                ? visibleContent.reasoningIncomplete
                  ? "Cooking paused before the final answer"
                  : "Cooked"
                : researchActivity
                  ? "Research activity"
                  : "Response activity"}
              active={cookingOpen}
              complete={cookingTurn
                ? !visibleContent.reasoningIncomplete
                : activityComplete}
              controls="cooking-activity-panel"
              onClick={onOpenCooking}
            />
          ) : null}
          {generatedImage ? (
            <GeneratedImage key={generatedImage.url} image={generatedImage} />
          ) : null}
          {visibleContent.answer && !generatedImage ? (
            <RichText className="message__content">{codeArtifacts.length ? normaliseNamedCodeFences(visibleContent.answer) : visibleContent.answer}</RichText>
          ) : (!actionProposal && !generatedImage ? (
            <p className="message__content">No response text was returned.</p>
          ) : null)}
          {codeArtifacts.length ? (
            <div className="message-code-artifacts" aria-label="Generated files">
              {codeArtifacts.map((artifact) => (
                <button
                  type="button"
                  className="message-code-artifact"
                  key={artifact.id}
                  onClick={() => downloadCodeArtifact(artifact)}
                >
                  <FileCode2 aria-hidden="true" />
                  <span>
                    <strong>{artifact.filename}</strong>
                    <small>{artifact.language} · {formatNumber(artifact.bytes)} bytes</small>
                  </span>
                  <Download aria-hidden="true" />
                </button>
              ))}
            </div>
          ) : null}
          {actionProposal && !actionDismissed && !generatedImage ? (
            <HostActionProposal
              proposal={actionProposal}
              operation={actionOperation}
              busy={actionBusy}
              onReview={onReviewAction}
              onConfirm={onConfirmAction}
              onStop={onStopAction}
              onDismiss={() => setActionDismissed(true)}
            />
          ) : null}
          {degradedOutput ? (
            <div className="message-quality-warning" role="note">
              <AlertTriangle aria-hidden="true" />
              <p>
                <strong>Malformed output.</strong>{" "}
                Retry this turn or choose a verified version in Versions.
              </p>
            </div>
          ) : null}
          {generationFailure ? (
            <div className="message-quality-warning" role="alert">
              <AlertTriangle aria-hidden="true" />
              <p>
                <strong>Response failed.</strong>{" "}
                {generationFailure.message} Your message was kept. You can retry this turn.
              </p>
            </div>
          ) : null}
          {message.context_omitted || details?.context_omitted ? (
            <p className="context-omission">
              Older conversation content was omitted to stay within this version’s token limit.
            </p>
          ) : null}
          {assistant && detailsOpen ? <TechnicalDetails details={details || {}} /> : null}
        </div>
        {assistant ? (
          <>
            <ClaimEvidence details={details} onOpenDetails={onOpenDetails} />
            <ResponseProvenance details={details} onOpenDetails={onOpenDetails} />
          </>
        ) : null}
        <div className="message__footer">
          {assistant ? <ResponseMetadata details={details} duration={duration} /> : <span />}
        <div
          className="message__tools"
          aria-label={assistant ? "Assistant message actions" : "User message actions"}
        >
          <button type="button" aria-label="Copy message" title="Copy" onClick={onCopy}>
            {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
            <span className="sr-only">{copied ? "Copied" : "Copy"}</span>
          </button>
          {assistant ? (
            <button
              type="button"
              aria-label="Technical details"
              title="Technical details"
              aria-expanded={detailsOpen}
              onClick={() => setDetailsOpen((open) => !open)}
            >
              <FileCode2 aria-hidden="true" />
              <span className="sr-only">Technical details</span>
            </button>
          ) : (
            <button
              type="button"
              aria-label="Retry message"
              title={
                retryEligible
                  ? "Generate another response for this message"
                  : "Only the latest completed user turn can be retried"
              }
              disabled={!retryEligible || retryBusy}
              onClick={onRetry}
            >
              <RotateCcw aria-hidden="true" />
              <span className="sr-only">Retry</span>
            </button>
          )}
          {copied ? <span className="message__copied" role="status">Copied</span> : null}
        </div>
        </div>
      </div>
    </article>
  );
}

export function downloadCodeArtifact(artifact) {
  if (requestNativeSave({
    source: "text",
    suggested_name: artifact.filename,
    content: artifact.content,
  })) return;
  const blob = new Blob([artifact.content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = artifact.filename;
  link.style.display = "none";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function GeneratedImage({ image }) {
  const [status, setStatus] = useState("loading");
  const [attempt, setAttempt] = useState(0);
  const source = attempt
    ? `${image.url}${image.url.includes("?") ? "&" : "?"}retry=${attempt}`
    : image.url;
  return (
    <figure className={`message-generated-image message-generated-image--${status}`}>
      {status === "failed" ? (
        <div className="message-generated-image__recovery">
          <p role="alert">The saved image could not be loaded.</p>
          <button type="button" onClick={() => {
            setStatus("loading");
            setAttempt((value) => value + 1);
          }}>Retry image</button>
        </div>
      ) : (
        <img
          key={source}
          src={source}
          alt={image.prompt || "Generated image"}
          width={image.width}
          height={image.height}
          loading="lazy"
          decoding="async"
          onLoad={() => setStatus("loaded")}
          onError={() => setStatus("failed")}
        />
      )}
      <figcaption>
        <span>{image.modelName}</span>
        <button type="button" disabled={status !== "loaded"} onClick={() => saveGeneratedImage(image)}>
          Save image
        </button>
      </figcaption>
    </figure>
  );
}

function saveGeneratedImage(image) {
  const suggested = `salty-steak-${image.id}.png`;
  if (requestNativeSave({
    source: "url",
    suggested_name: suggested,
    url: image.url,
  })) return;
  const link = document.createElement("a");
  link.href = image.url;
  link.download = suggested;
  link.style.display = "none";
  document.body.append(link);
  link.click();
  link.remove();
}

export function requestNativeSave(payload) {
  const bridge = window.chrome?.webview;
  if (!bridge || typeof bridge.postMessage !== "function") return false;
  bridge.postMessage({ type: "save_file", ...payload });
  return true;
}

function messageReasoningMode(details) {
  const value = String(
    details?.reasoning_mode_effective ?? details?.reasoning_mode ?? "",
  ).trim().toLowerCase();
  if (value === "cooking" || value === "instant") return value;
  return "";
}












function SourceMarks({ details }) {
  const { marks, overflow } = sourceMarks(details, { limit: 3 });
  if (!marks.length) return null;
  return (
    <span className="source-marks">
      {marks.map((mark) => (
        <span
          key={mark.host}
          className="source-marks__pill"
          title={`${mark.host} — ${mark.title}`}
        >
          <span className="source-marks__icon" aria-hidden="true">
            <SiteIcon url={mark.url || `https://${mark.host}`} host={mark.host}/>
          </span>
          <span className="source-marks__host">{mark.host}</span>
        </span>
      ))}
      {overflow ? (
        <span className="source-marks__pill source-marks__pill--more">
          +{overflow} more
        </span>
      ) : null}
    </span>
  );
}








function ClaimEvidence({ details, onOpenDetails }) {
  const rows = usedSources(details);
  if (!rows.length) return null;
  return (
    <div className="claim-evidence">
      {rows.map((row) => (
        <button
          type="button"
          key={row.ref}
          className="claim-evidence__row"
          onClick={() => onOpenDetails?.(details)}
          title={`${row.title} — ${row.host}`}
        >
          <span className="claim-evidence__icon" aria-hidden="true">
            {row.icon ? (
              <img
                src={row.icon}
                alt=""
                loading="lazy"
                onError={(event) => {
                  event.currentTarget.style.visibility = "hidden";
                }}
              />
            ) : null}
          </span>
          <span className="claim-evidence__facts">{row.snippet}</span>
          <span className="claim-evidence__host">{row.host}</span>
        </button>
      ))}
    </div>
  );
}

function ResponseProvenance({ details, onOpenDetails }) {
  const account = responseDetails(details || {});
  if (!account.completion) return null;



  const summary = account.completion;

  if (!account.hasDetails) {
    return (
      <p className={`response-provenance response-provenance--${account.state}`}>
        {summary}
      </p>
    );
  }
  return (
    <button
      type="button"
      className={`response-provenance response-provenance--${account.state} response-provenance--open`}
      onClick={() => onOpenDetails?.(details)}
    >
      {summary}
      <SourceMarks details={details} />
      <ChevronRight aria-hidden="true" />
    </button>
  );
}

function ResponseMetadata({ details, duration }) {
  const tokens = visibleOutputTokens(details);
  const entries = [



    Number(tokens) > 0 ? finiteCount(tokens, "output tokens") : null,
    finiteRate(details?.decode_tokens_per_second || details?.tokens_per_second),
    details?.execution_device || details?.device || null,
  ].filter(Boolean);
  if (!entries.length) return null;
  return (
    <footer className="message__metadata" aria-label="Response metadata">
      {entries.map((entry) => <span key={entry}>{entry}</span>)}
    </footer>
  );
}

function TechnicalDetails({ details }) {
  const groups = [
    ["Identity", [
      ["Model name", details.model_name],
      ["Saved version label", details.saved_version_label],
      ["Saved version role", details.role],
      ["Saved version ID", details.saved_version_id || details.checkpoint_id, "mono"],
      ["Checkpoint ID", details.checkpoint_id, "mono"],
      ["Runtime ID", details.runtime_id, "mono"],
      [
        "Active and loaded",
        details.runtime_id && details.checkpoint_id
          ? "Confirmed for this response"
          : "Unavailable",
      ],
    ]],
    ["Request ownership", [
      ["Conversation ID", details.conversation_id, "mono"],
      ["User message ID", details.user_message_id, "mono"],
      [
        "Generation ID",
        details.generation_id || details.generation_operation_id,
        "mono",
      ],
      ["Active version ID", details.active_version_id, "mono"],
      ["Template version", details.template_version, "mono"],
      ["Cancellation token", details.cancellation_token, "mono"],
      ["Cancellation state", details.cancellation_state],
    ]],
    ["Training origin", [
      ["Dataset lineage", details.lineage_id, "mono"],
      ["Parent version", details.parent_version_id, "mono"],
      ["Steps added", formatNumber(details.additional_steps)],
      [
        "Cumulative trained steps",
        details.total_trained_steps === null || details.total_trained_steps === undefined
          ? "Unknown at import"
          : formatNumber(details.total_trained_steps),
      ],
      ["Evaluation", details.evaluation_state],
    ]],
    ["Context and generation", [
      ["Input context tokens", formatNumber(details.input_context_tokens)],





      ["Answer output tokens", formatNumber(visibleOutputTokens(details))],
      ["Routing generation tokens", formatNumber(details.generated_output_tokens)],
      ["Total processed tokens", formatNumber(details.total_processed_tokens)],
      ["Architectural context limit", formatNumber(details.architectural_context_limit)],
      ["Reserved output tokens", formatNumber(details.reserved_output_tokens)],
      ["Omitted input tokens", formatNumber(details.omitted_input_tokens)],
      ["Stopped state", details.generation_state || "Not reported"],
    ]],
    ["Timing", [
      [
        "Generation duration",
        details.generation_duration_ms !== undefined
          ? formatDuration(Number(details.generation_duration_ms) / 1000)
          : "Not reported",
      ],
      [
        "Generation speed",
        details.tokens_per_second
          ? `${formatNumber(details.tokens_per_second)} tokens/sec`
          : "Not reported",
      ],
    ]],
    ["Performance", [
      ["Execution device", details.execution_device || details.device],
      ["Precision", details.precision || details.execution_dtype],
      ["Attention backend", details.attention_backend],
      ["Compile mode", details.compile_mode],
      ["KV cache", details.kv_cache_state],
      ["Cold / warm", details.cold_warm_classification],
      [
        "Queue time",
        secondsOrUnavailable(details.queue_time_seconds),
      ],
      [
        "Tokenisation",
        secondsOrUnavailable(details.tokenisation_duration_seconds),
      ],
      [
        "Prefill",
        secondsOrUnavailable(details.prefill_duration_seconds),
      ],
      [
        "First token",
        secondsOrUnavailable(details.first_token_latency_seconds),
      ],
      [
        "Decode duration",
        secondsOrUnavailable(details.decode_duration_seconds),
      ],
      [
        "Decode speed",
        details.decode_tokens_per_second
          ? `${formatNumber(details.decode_tokens_per_second)} tokens/sec`
          : "Not reported",
      ],
      [
        "Total duration",
        secondsOrUnavailable(details.generation_duration_seconds),
      ],
    ]],
  ];

  return (
    <div className="message-details" aria-label="Technical details">
      <div className="message-details__groups">
        {groups.map(([title, fields]) => (
          <section className="message-details__group" key={title}>
            <h3>{title}</h3>
            <dl>
              {fields.map(([label, value, className]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd className={className}>{value ?? "Not reported"}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </div>
  );
}

function secondsOrUnavailable(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0
    ? formatDuration(seconds)
    : "Not reported";
}

function finiteCount(value, unit) {
  const count = Number(value);
  return Number.isFinite(count) && count >= 0
    ? `${formatNumber(count)} ${unit}`
    : null;
}

function finiteRate(value) {
  const rate = Number(value);
  return Number.isFinite(rate) && rate > 0
    ? `${formatNumber(rate)} tokens/sec`
    : null;
}

function messageTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function looksDegradedOutput(value) {
  const content = String(value || "").trim();
  if (!content) return true;
  const uuidLike = content.match(
    /\b[0-9a-f]{1,8}(?:-[0-9a-f]{1,12}){2,}\b/gi,
  ) || [];
  if (!uuidLike.length) return false;
  const remainder = content
    .replace(/\b[0-9a-f]{1,8}(?:-[0-9a-f]{1,12}){2,}\b/gi, "")
    .replace(/[\s,.;:()[\]{}]+/g, "");
  return remainder.length < 24;
}

function thoughtDuration(details) {
  const milliseconds = Number(details?.generation_duration_ms);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return null;
  if (milliseconds < 1000) {
    return `${Math.max(0.1, milliseconds / 1000).toFixed(1)}s`;
  }
  const seconds = milliseconds / 1000;
  return `${seconds >= 10 ? Math.round(seconds) : seconds.toFixed(1).replace(/\.0$/, "")}s`;
}
