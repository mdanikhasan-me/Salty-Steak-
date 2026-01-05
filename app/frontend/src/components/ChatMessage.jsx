import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  FileCode2,
  RotateCcw,
} from "lucide-react";
import { formatDuration, formatNumber } from "../workflows/formatters.js";
import { presentAssistantContent } from "../workflows/chatContent.mjs";
import { generatedImageForMessage } from "../workflows/generatedImages.mjs";
import {
  hostActionProposalForMessage,
  hostSafeAssistantContent,
} from "../workflows/hostActions.mjs";
import { CookingStatus } from "./CookingStatus.jsx";
import { HostActionProposal } from "./HostActionProposal.jsx";
import { RichText } from "./RichText.jsx";

export function ChatMessage({
  message,
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
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [actionsVisible, setActionsVisible] = useState(false);
  const hideTimerRef = useRef(null);
  const assistant = message.role === "assistant";
  const details = message.technical_details || message.details;
  const duration = assistant ? thoughtDuration(details) : null;
  const reasoningMode = assistant ? messageReasoningMode(details) : "";
  const actionProposal = assistant ? hostActionProposalForMessage(message) : null;
  const generatedImage = assistant ? generatedImageForMessage(message) : null;
  const [actionDismissed, setActionDismissed] = useState(false);


  const visibleContent = assistant
    ? presentAssistantContent(
      hostSafeAssistantContent(message, actionProposal),
      reasoningMode,
    )
    : { answer: String(message.content || "").trim(), reasoning: "", cookingTurn: false };
  const cookingTurn = assistant
    && visibleContent.cookingTurn
    && !actionProposal
    && !generatedImage;


  const degradedOutput = assistant
    && !actionProposal
    && looksDegradedOutput(visibleContent.answer);

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
        <div className="message__surface">
          {cookingTurn ? (
            <CookingStatus
              label={visibleContent.reasoningIncomplete
                ? "Cooking paused before the final answer"
                : "Cooked"}
              active={cookingOpen}
              complete={!visibleContent.reasoningIncomplete}
              controls="cooking-activity-panel"
              onClick={onOpenCooking}
            />
          ) : null}
          {generatedImage ? (
            <figure className="message-generated-image">
              <img
                src={generatedImage.url}
                alt={generatedImage.prompt || "Generated image"}
                width={generatedImage.width}
                height={generatedImage.height}
                loading="lazy"
                decoding="async"
              />
              <figcaption>
                <span>{generatedImage.modelName}</span>
                <a href={generatedImage.url} download={`salty-steak-${generatedImage.id}.png`}>
                  Save image
                </a>
              </figcaption>
            </figure>
          ) : null}
          {visibleContent.answer && !generatedImage ? (
            <RichText className="message__content">{visibleContent.answer}</RichText>
          ) : (!actionProposal && !generatedImage ? (
            <p className="message__content">No response text was returned.</p>
          ) : null)}
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
          {message.context_omitted || details?.context_omitted ? (
            <p className="context-omission">
              Older conversation content was omitted to stay within this version’s token limit.
            </p>
          ) : null}
          {assistant ? <ResponseMetadata details={details} duration={duration} /> : null}
          {assistant && detailsOpen ? <TechnicalDetails details={details || {}} /> : null}
        </div>
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
    </article>
  );
}

function messageReasoningMode(details) {
  const value = String(
    details?.reasoning_mode_effective ?? details?.reasoning_mode ?? "",
  ).trim().toLowerCase();
  if (value === "cooking" || value === "instant") return value;
  return "";
}

function ResponseMetadata({ details, duration }) {
  const entries = [
    duration ? `Generated in ${duration}` : null,
    finiteCount(details?.generated_output_tokens, "output tokens"),
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
      ["Generated output tokens", formatNumber(details.generated_output_tokens)],
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
