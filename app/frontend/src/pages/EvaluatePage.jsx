import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  BarChart3,
  ChevronDown,
  ChevronUp,
  FileCheck2,
  Play,
  X,
} from "lucide-react";
import { api } from "../api/client.js";
import { Field, FormActions } from "../components/Forms.jsx";
import { OperationProgress } from "../components/OperationProgress.jsx";
import {
  Button,
  DefinitionList,
  EmptyState,
  InlineNotice,
  PageHeader,
  Status,
} from "../components/Primitives.jsx";
import { useAppState } from "../state/AppState.jsx";
import {
  formatDate,
  formatDuration,
  formatNumber,
  formatRate,
  versionLabel,
} from "../workflows/formatters.js";
import {
  isActive,
  operationMatches,
  operationState,
} from "../workflows/operations.mjs";
import { sameSelectionId } from "../workflows/selection.mjs";

const EVALUATION_PHASES = [
  { value: "queued", label: "Queued" },
  { value: "preparing_evaluation", label: "Preparing evaluation" },
  { value: "loading_saved_version_for_evaluation", label: "Loading saved version for evaluation" },
  { value: "evaluating_validation_records", label: "Evaluating validation records" },
  { value: "finalising_evaluation", label: "Finalising evaluation" },
  { value: "saving_evaluation_report", label: "Saving evaluation report" },
  { value: "completed", label: "Completed" },
];

export function EvaluatePage({ onNavigate }) {
  const {
    versions,
    evaluations,
    operations,
    refreshDomain,
    startOperation,
    reportError,
  } = useAppState();
  const requestedVersionId = getHashParameter("version");
  const [versionId, setVersionId] = useState(requestedVersionId || versions[0]?.id || "");
  const [starting, setStarting] = useState(false);
  const [trendMetric, setTrendMetric] = useState("loss");
  const [expandedEvaluationId, setExpandedEvaluationId] = useState(null);
  const primaryEvaluations = useMemo(
    () =>
      evaluations.filter(
        (evaluation) => evaluation.primary_presentation !== false,
      ),
    [evaluations],
  );

  useEffect(() => {
    void Promise.all([
      refreshDomain("versions", { quiet: true }),
      refreshDomain("evaluations", { quiet: true }),
    ]).catch((error) => reportError(error, "open-evaluate"));
  }, [refreshDomain, reportError]);

  useEffect(() => {
    if (!expandedEvaluationId) return undefined;
    function closeOnEscape(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        setExpandedEvaluationId(null);
      }
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [expandedEvaluationId]);

  useEffect(() => {
    const requested = requestedVersionId
      ? versions.find((version) => sameSelectionId(version.id, requestedVersionId))
      : null;
    if (requested) {
      setVersionId(requested.id);
    } else if (!versionId && versions[0]) {
      setVersionId(versions[0].id);
    } else if (versionId && !versions.some((version) => sameSelectionId(version.id, versionId))) {
      setVersionId(versions[0]?.id || "");
    }
  }, [requestedVersionId, versionId, versions]);

  const selectedVersion = versions.find((version) => sameSelectionId(version.id, versionId));
  const evaluationUnavailable =
    selectedVersion?.available_actions?.evaluate?.enabled === false;
  const evaluationOperation = useMemo(
    () =>
      Object.values(operations)
        .filter(
          (operation) =>
            operationMatches(operation, "evaluation") &&
            (!versionId ||
              String(operation.target_id || operation.targetId) === String(versionId)),
        )
        .sort((a, b) => Date.parse(b.updated_at || 0) - Date.parse(a.updated_at || 0))[0],
    [operations, versionId],
  );
  const active = evaluationOperation && isActive(evaluationOperation);

  async function startEvaluation() {
    if (!versionId) return;
    setStarting(true);
    try {
      await startOperation({
        type: "evaluation",
        targetId: versionId,
        launch: (requestKey) =>
          api.startEvaluation({ saved_version_id: versionId }, requestKey),
      });
    } catch (error) {
      reportError(error, `evaluate:${versionId}`);
    } finally {
      setStarting(false);
    }
  }

  const completedResult =
    operationState(evaluationOperation) === "completed" ? evaluationOperation.result : null;
  const identityVerified = completedResult
    ? verifyEvaluationCompletion(evaluationOperation, selectedVersion)
    : false;

  return (
    <div className="page page--evaluate">
      <PageHeader title="Evaluate" />

      {!versions.length ? (
        <EmptyState
          icon={BarChart3}
          title="A saved version is needed"
          description="Complete training and verify a saved version before running an evaluation."
          action={
            <Button variant="primary" icon={ArrowRight} onClick={() => onNavigate("train")}>
              Go to Train
            </Button>
          }
        />
      ) : (
        <>
          <section className="evaluation-setup">
            <div className="section-heading">
              <div>
                <span className="eyebrow">New evaluation</span>
                <h2>Choose what to measure</h2>
              </div>
              <Status
                value={active ? evaluationOperation.state : "not_started"}
                label={active ? "Evaluation in progress" : "Not started"}
              />
            </div>
            <div className="evaluation-form">
              <Field label="Saved version">
                <select
                  value={versionId}
                  disabled={active}
                  onChange={(event) => setVersionId(event.target.value)}
                >
                  {versions.map((version) => (
                    <option value={version.id} key={version.id}>
                      {versionLabel(version)} · {version.dataset_name || "Dataset unknown"} ·{" "}
                      {String(version.id).slice(-6)}
                    </option>
                  ))}
                </select>
              </Field>
              {selectedVersion ? (
                <DefinitionList
                  compact
                  items={[
                    { label: "Saved", value: formatDate(selectedVersion.saved_at) },
                    {
                      label: "Context capability",
                      value:
                        selectedVersion.context_limit_tokens === null ||
                        selectedVersion.context_limit_tokens === undefined
                          ? "Not reported"
                          : `${formatNumber(selectedVersion.context_limit_tokens)} tokens`,
                    },
                    { label: "Integrity", value: <Status value={selectedVersion.integrity} size="small" /> },
                  ]}
                />
              ) : null}
              {evaluationUnavailable ? (
                <InlineNotice kind="warning" title="Evaluation unavailable">
                  {selectedVersion.available_actions?.evaluate?.reason ||
                    "This saved version has no verified validation split."}
                </InlineNotice>
              ) : null}
              <FormActions>
                <Button
                  variant="primary"
                  icon={Play}
                  busy={starting}
                  disabled={active || !versionId || evaluationUnavailable}
                  onClick={startEvaluation}
                >
                  Start evaluation
                </Button>
              </FormActions>
            </div>
          </section>

          {evaluationOperation ? (
            <OperationProgress
              operation={evaluationOperation}
              phases={EVALUATION_PHASES}
              title="Evaluation progress"
              metrics={evaluationMetrics}
            />
          ) : (
            <div className="quiet-placeholder">
              <FileCheck2 aria-hidden="true" />
              <div>
                <h2>No evaluation started</h2>
                <p>Choose a saved version above, then start when you are ready.</p>
              </div>
            </div>
          )}

          {completedResult ? (
            identityVerified ? (
              <EvaluationResult
                result={completedResult}
                version={selectedVersion}
                versions={versions}
                evaluations={primaryEvaluations}
                trendMetric={trendMetric}
                onTrendMetricChange={setTrendMetric}
              />
            ) : (
              <InlineNotice kind="error" title="Result identity could not be verified">
                The result is hidden because its operation ID, saved version ID, terminal state,
                saved result, or readable report proof does not match this evaluation.
              </InlineNotice>
            )
          ) : null}
        </>
      )}

      {primaryEvaluations.length ? (
        <section className="evaluation-history">
          <div className="section-heading">
            <div>
              <span className="eyebrow">New application records</span>
              <h2>Recent evaluations</h2>
            </div>
          </div>
	          <div className="record-table" role="table" aria-label="Recent evaluations">
	            <div className="record-table__header" role="row">
	              <span role="columnheader">Saved version</span>
	              <span role="columnheader">Result</span>
	              <span role="columnheader">Completed</span>
	              <span role="columnheader">Details</span>
	            </div>
	            {primaryEvaluations.map((evaluation) => {
	              const expanded = expandedEvaluationId === evaluation.id;
	              const metrics = evaluation.metrics || {};
	              return (
	                <div className="record-table__group" key={evaluation.id}>
	                  <div className="record-table__row" role="row">
	                    <strong role="cell">
	                      {evaluation.version_label || evaluation.saved_version_id}
	                    </strong>
	                    <span role="cell" className="evaluation-history__result">
	                      <Status
	                        value={evaluation.state || "completed"}
	                        label={
	                          evaluation.state === "completed"
	                            ? `Loss ${displayMetric(metrics.loss, 4)} · PPL ${displayMetric(metrics.perplexity, 3)}`
	                            : evaluation.summary || undefined
	                        }
	                        size="small"
	                      />
	                    </span>
	                    <span role="cell">{formatDate(evaluation.completed_at)}</span>
	                    <span role="cell" className="record-table__detail-action">
	                      <button
	                        type="button"
	                        className="icon-button icon-button--quiet"
	                        aria-label={
	                          expanded
	                            ? `Close details for evaluation ${evaluation.id}`
	                            : `Open details for evaluation ${evaluation.id}`
	                        }
	                        aria-expanded={expanded}
	                        aria-controls={`evaluation-details-${evaluation.id}`}
	                        onClick={() =>
	                          setExpandedEvaluationId(expanded ? null : evaluation.id)
	                        }
	                        onKeyDown={(event) => {
	                          if (event.key === "Escape" && expanded) {
	                            event.preventDefault();
	                            setExpandedEvaluationId(null);
	                            event.currentTarget.focus();
	                          }
	                        }}
	                      >
	                        {expanded ? (
	                          <ChevronUp aria-hidden="true" />
	                        ) : (
	                          <ChevronDown aria-hidden="true" />
	                        )}
	                      </button>
	                    </span>
	                  </div>
	                  {expanded ? (
	                    <div
	                      id={`evaluation-details-${evaluation.id}`}
	                      className="evaluation-history__details"
	                      role="region"
	                      aria-label={`Details for evaluation ${evaluation.id}`}
	                    >
	                      <DefinitionList
	                        compact
	                        items={[
	                          { label: "Evaluation ID", value: evaluation.id, mono: true },
	                          {
	                            label: "Operation ID",
	                            value: evaluation.operation_id || "Not reported",
	                            mono: true,
	                          },
	                          {
	                            label: "Saved-version ID",
	                            value: evaluation.saved_version_id || "Not reported",
	                            mono: true,
	                          },
	                          {
	                            label: "Prepared dataset ID",
	                            value:
	                              metrics.prepared_dataset_id ||
	                              evaluation.prepared_dataset_id ||
	                              "Not reported",
	                            mono: true,
	                          },
	                          { label: "Loss", value: displayMetric(metrics.loss, 4) },
	                          {
	                            label: "Perplexity",
	                            value: displayMetric(metrics.perplexity, 3),
	                          },
	                          {
	                            label: "Records / tokens",
	                            value: `${formatNumber(
	                              metrics.records_evaluated ?? evaluation.records_completed,
	                            )} / ${formatNumber(metrics.tokens_evaluated)}`,
	                          },
	                          {
	                            label: "Split / context",
	                            value: `${metrics.evaluation_split || "Not reported"} · ${
	                              metrics.context_length
	                                ? `${formatNumber(metrics.context_length)} tokens`
	                                : "Not reported"
	                            }`,
	                          },
	                          {
	                            label: "Duration / speed",
	                            value: `${formatDuration(metrics.duration_seconds)} · ${formatRate(
	                              metrics.tokens_per_second,
	                              "tokens/sec",
	                            )}`,
	                          },
	                          {
	                            label: "Training loss",
	                            value: displayMetric(
	                              metrics.training_loss_at_checkpoint,
	                              4,
	                            ),
	                          },
	                          {
	                            label: "Report",
	                            value: evaluation.report_path || "Not reported",
	                            mono: true,
	                          },
	                          {
	                            label: "Integrity / contract",
	                            value: `${evaluation.integrity_state || "Unavailable"} · ${
	                              evaluation.contract_version || "Not reported"
	                            }`,
	                          },
	                        ]}
	                      />
	                      <button
	                        type="button"
	                        className="button button--quiet evaluation-history__close"
	                        onClick={() => setExpandedEvaluationId(null)}
	                      >
	                        <X aria-hidden="true" />
	                        Close details
	                      </button>
	                    </div>
	                  ) : null}
	                </div>
	              );
	            })}
	          </div>
        </section>
      ) : null}
    </div>
  );
}

function EvaluationResult({
  result,
  version,
  versions,
  evaluations,
  trendMetric,
  onTrendMetricChange,
}) {
  const metrics = result.metrics || result;
  const comparable = evaluations
    .filter((evaluation) => {
      const candidate = evaluation.metrics || {};
      return (
        metrics.evaluation_identity &&
        candidate.evaluation_identity === metrics.evaluation_identity &&
        versions.some(
          (item) =>
            sameSelectionId(item.id, evaluation.saved_version_id) &&
            item.lineage_id === version?.lineage_id,
        )
      );
    })
    .map((evaluation) => ({
      evaluation,
      version: versions.find((item) => sameSelectionId(item.id, evaluation.saved_version_id)),
      metrics: evaluation.metrics || {},
    }))
    .filter((item) => item.version)
    .sort(
      (a, b) =>
        Date.parse(a.version.saved_at || 0) - Date.parse(b.version.saved_at || 0),
    );
  const parent = comparable.find(
    (item) => sameSelectionId(item.version.id, version?.parent_version_id),
  );
  const baseline = comparable.find(
    (item) => sameSelectionId(item.version.id, version?.baseline_version_id),
  );
  const loss = Number(metrics.loss);
  const perplexity = Number(metrics.perplexity);
  const compatibleLoss = metrics.loss_contract === "token_natural_log_cross_entropy";
  return (
    <section className="evaluation-result">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Saved result</span>
          <h2>Evaluation completed</h2>
          <p>{versionLabel(version)}</p>
        </div>
        <Status value="completed" />
      </div>
      <DefinitionList
        items={[
          { label: "Evaluation loss", value: displayMetric(metrics.loss, 4) },
          {
            label: "Perplexity",
            value:
              compatibleLoss && Number.isFinite(perplexity)
                ? displayMetric(perplexity, 3)
                : "Unavailable for this loss contract",
          },
          {
            label: "Change from parent",
            value:
              parent && Number.isFinite(loss)
                ? signedDifference(loss, Number(parent.metrics.loss), 4)
                : "Not comparable",
          },
          {
            label: "Change from lineage baseline",
            value:
              baseline && Number.isFinite(loss)
                ? signedDifference(loss, Number(baseline.metrics.loss), 4)
                : "Not comparable",
          },
          { label: "Records evaluated", value: formatNumber(metrics.records_evaluated) },
          { label: "Tokens evaluated", value: formatNumber(metrics.tokens_evaluated) },
          { label: "Dataset", value: metrics.dataset_id || "Not reported", mono: true },
          { label: "Split", value: metrics.evaluation_split || "Not reported" },
          {
            label: "Context length",
            value: metrics.context_length
              ? `${formatNumber(metrics.context_length)} tokens`
              : "Not reported",
          },
          {
            label: "Duration",
            value: formatDuration(metrics.duration_seconds),
          },
          {
            label: "Speed",
            value: formatRate(metrics.tokens_per_second, "tokens/sec"),
          },
          {
            label: "Training loss at checkpoint",
            value: displayMetric(metrics.training_loss_at_checkpoint, 4),
          },
          { label: "Report", value: result.report_path, mono: true },
        ]}
      />
      <InlineNotice kind="information" title="How to read these metrics">
        Lower validation loss is generally better on the same evaluation setup. Lower
        perplexity is also better, but perplexity is the exponential of token-level
        natural-log loss, so the two are mathematically related rather than independent
        evidence. Results are not compared across different data, tokenizers, splits,
        context settings, or loss contracts.
      </InlineNotice>
      {comparable.length >= 2 ? (
        <div className="evaluation-trend">
          <div className="evaluation-trend__heading">
            <h3>Retained lineage trend</h3>
            <select
              aria-label="Trend metric"
              value={trendMetric}
              onChange={(event) => onTrendMetricChange(event.target.value)}
            >
              <option value="loss">Validation loss</option>
              <option value="perplexity">Perplexity</option>
            </select>
          </div>
          <div className="evaluation-trend__rows">
            {comparable.slice(-3).map((item) => (
              <div key={item.version.id}>
                <span>{versionLabel(item.version)}</span>
                <strong>
                  {displayMetric(item.metrics[trendMetric], trendMetric === "loss" ? 4 : 3)}
                </strong>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <p className="evaluation-comparison-note">
          No compatible retained lineage comparison is available.
        </p>
      )}
    </section>
  );
}

function verifyEvaluationCompletion(operation, version) {
  const result = operation?.result;
  if (!operation?.id || operationState(operation) !== "completed" || !result) return false;
  const operationId = result.operation_id || operation.id;
  const savedVersionId = result.saved_version_id || operation.target_id;
  const readable = result.report_readable ?? result.result_file_readable ?? Boolean(result.report_path);
  const saved = result.saved ?? result.result_saved ?? Boolean(result.report_path);
  return (
    String(operationId) === String(operation.id) &&
    String(savedVersionId) === String(version?.id) &&
    Boolean(readable) &&
    Boolean(saved)
  );
}

function evaluationMetrics(operation) {
  const progress = operation.progress || operation.result || {};
  const terminal = ["completed", "failed", "interrupted", "cancelled"].includes(
    operationState(operation),
  );
  const remaining = operation.remaining_seconds ?? progress.remaining_seconds;
  return [
    {
      label: "Records completed",
      value: formatNumber(operation.current_progress ?? progress.records_completed),
    },
    {
      label: "Total records",
      value: formatNumber(operation.total_progress ?? progress.total_records),
    },
    {
      label: "Speed",
      value: formatRate(operation.records_per_second ?? progress.records_per_second, "records/sec"),
    },
    {
      label: "Elapsed time",
      value: formatDuration(operation.elapsed_seconds ?? progress.elapsed_seconds),
    },
    {
      label: "Remaining time",
      value: terminal
        ? "Not applicable after completion"
        : remaining !== null && remaining !== undefined
          ? formatDuration(remaining)
          : "Not reliable yet",
    },
  ];
}

function displayMetric(value, digits) {
  if (value === null || value === undefined || value === "") {
    return "Not reported";
  }
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "Not reported";
}

function signedDifference(current, reference, digits) {
  if (!Number.isFinite(current) || !Number.isFinite(reference)) return "Not comparable";
  const difference = current - reference;
  return `${difference > 0 ? "+" : ""}${difference.toFixed(digits)}`;
}

function getHashParameter(name) {
  const query = window.location.hash.split("?")[1] || "";
  return new URLSearchParams(query).get(name);
}
