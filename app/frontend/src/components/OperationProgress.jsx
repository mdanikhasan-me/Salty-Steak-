import { Clock3, Square } from "lucide-react";
import { useState } from "react";
import { useAppState } from "../state/AppState.jsx";
import {
  isActive,
  isTerminal,
  measurableProgress,
  operationState,
  phaseLabel,
} from "../workflows/operations.mjs";
import { trainingCompletionEvidence } from "../workflows/postTraining.mjs";
import { errorMessage, formatDuration, formatNumber, formatRate } from "../workflows/formatters.js";
import {
  Button,
  DefinitionList,
  Disclosure,
  InlineNotice,
  Status,
} from "./Primitives.jsx";

export function OperationProgress({
  operation,
  phases = [],
  allowStop = false,
  metrics,
  title,
  compact = false,
}) {
  const { stopOperation, reportError } = useAppState();
  const [stopping, setStopping] = useState(false);
  if (!operation) return null;

  const state = operationState(operation);
  const progress = measurableProgress(operation);
  const phase = phaseLabel(operation);
  const stopRequested = state === "stop_requested";
  const terminal = isTerminal(operation);
  const detailItems = metrics ? metrics(operation) : defaultMetrics(operation);
  const completedMeasuredWork =
    state === "failed" &&
    Number(operation.current_progress) > 0 &&
    operation.total_progress !== null &&
    operation.total_progress !== undefined &&
    Number(operation.current_progress) >= Number(operation.total_progress);
  const recovered =
    operation.outcome === "recovered_and_completed" ||
    operation.result?.recovered_after_restart;
  const needsAttention =
    operation.outcome === "needs_attention" ||
    operation.result?.completion_outcome === "needs_attention";
  const outcomeTitle = recovered
    ? "Recovered and completed"
    : needsAttention
    ? "Saved version needs attention"
    : completedMeasuredWork
    ? "Training steps completed; version finalisation failed"
    : state === "failed"
      ? "Training did not complete"
      : phase;

  async function handleStop() {
    setStopping(true);
    try {
      await stopOperation(operation.id);
    } catch (error) {
      reportError(error, `stop:${operation.id}`);
    } finally {
      setStopping(false);
    }
  }

  return (
    <section className={`operation-progress ${compact ? "operation-progress--compact" : ""}`} aria-live="polite">
      <div className="operation-progress__heading">
        <div>
          <span className="eyebrow">{title || "Current operation"}</span>
          <h2 className={isActive(operation) ? "activity-phrase activity-phrase--headline" : undefined}>
            {outcomeTitle}
          </h2>
        </div>
        <Status
          value={recovered || needsAttention ? "warning" : state}
          label={
            recovered
              ? "Recovered after restart"
              : needsAttention
                ? "Needs attention"
              : state === "failed"
                ? "Needs attention"
                : undefined
          }
        />
      </div>

      {progress ? (
        <div className="progress-block">
          <div
            className={`progress-track ${completedMeasuredWork ? "progress-track--attention" : ""}`}
            aria-label={
              completedMeasuredWork
                ? "Training steps complete; version finalisation incomplete"
                : `${Math.round(progress.percentage)}% complete`
            }
          >
            <span style={{ width: `${progress.percentage}%` }} />
          </div>
          <div className="progress-labels">
            <span>
              {formatNumber(progress.current)} of {formatNumber(progress.total)}
            </span>
            <span>
              {completedMeasuredWork
                ? "Training steps complete · finalisation incomplete"
                : recovered
                  ? "Training steps complete · finalisation recovered"
                  : `${Math.round(progress.percentage)}%`}
            </span>
          </div>
        </div>
      ) : isActive(operation) ? (
        <div className="indeterminate-track" aria-label={`${phase}, progress is being measured`}>
          <span />
        </div>
      ) : null}

      {phases.length ? (
        <PhaseSequence
          phases={phases}
          current={operation.phase}
          state={state}
          operation={operation}
        />
      ) : null}

      {detailItems.length ? <DefinitionList items={detailItems} compact /> : null}

      {state === "queued" && operation.result?.queue_reason ? (
        <InlineNotice kind="information" title="Queued">
          {operation.result.queue_reason}
        </InlineNotice>
      ) : null}

      {state === "failed" ? (
        <>
          <InlineNotice
            kind={needsAttention ? "warning" : "error"}
            title={
              needsAttention
                ? "Post-training workflow needs attention"
                : operation.error?.failed_phase || "Version finalisation"
            }
          >
            {errorMessage(operation.error, "The operation could not complete.")}
          </InlineNotice>
          {operation.error?.technical_details ? (
            <Disclosure summary="Technical details">
              <p className="mono">{operation.error.technical_details}</p>
            </Disclosure>
          ) : null}
        </>
      ) : null}

      {stopRequested ? (
        <InlineNotice kind="information" title="Stop requested">
          Salty Steak will stop at the next safe boundary. The request has been saved.
        </InlineNotice>
      ) : null}

      {allowStop && !terminal ? (
        <div className="operation-progress__actions">
          <Button
            variant="danger-quiet"
            icon={Square}
            busy={stopping}
            disabled={stopRequested}
            onClick={handleStop}
          >
            {stopRequested ? "Stop requested" : "Stop safely"}
          </Button>
        </div>
      ) : null}
    </section>
  );
}

function PhaseSequence({ phases, current, state, operation }) {
  const currentToken = String(current || "").toLowerCase().replaceAll(" ", "_");
  const currentIndex = phases.findIndex((item) => item.value === currentToken);
  const isTrainingLifecycle = phases.some((item) => item.value === "training");
  const completedTrainingPhases =
    state === "completed" && isTrainingLifecycle
      ? trainingCompletionEvidence(operation)
      : null;
  return (
    <ol className="phase-sequence" aria-label="Operation phases">
      {phases.map((item, index) => {
        const done = completedTrainingPhases
          ? completedTrainingPhases.has(item.value)
          : state === "completed" || (currentIndex >= 0 && index < currentIndex);
        const active = index === currentIndex && isActive(operation);
        return (
          <li className={done ? "phase--done" : active ? "phase--active" : ""} key={item.value}>
            <span aria-hidden="true">{done ? "✓" : index + 1}</span>
            <span>{item.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function defaultMetrics(operation) {
  const result = operation.result || {};
  return [
    {
      label: "Elapsed time",
      value: formatDuration(operation.elapsed_seconds ?? result.elapsed_seconds),
    },
    {
      label: "Remaining time",
      value:
        operation.remaining_seconds === undefined
          ? "Not reliable yet"
          : formatDuration(operation.remaining_seconds),
    },
    {
      label: "Worker",
      value: operation.worker_pid ? `Local process ${operation.worker_pid}` : "Waiting",
    },
  ];
}
