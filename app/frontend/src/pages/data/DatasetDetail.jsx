import { Check, Database, Eye } from "lucide-react";
import {
  formatBytes,
  formatDate,
  formatNumber,
} from "../../workflows/formatters.js";
import { isActive, operationState } from "../../workflows/operations.mjs";
import { OperationProgress } from "../../components/OperationProgress.jsx";
import {
  Button,
  DefinitionList,
  InlineNotice,
  Status,
} from "../../components/Primitives.jsx";
import { PREPARATION_PHASES } from "./constants.js";
import { ValidationReport } from "./ValidationReport.jsx";

export function DatasetDetail({
  dataset,
  validationOperation,
  preparationOperation,
  starting,
  onValidate,
  onPrepare,
  learningPreview,
  previewBusy,
  onLoadPreview,
}) {
  const legacy = dataset.prepared_policy?.classification === "blocked_legacy";
  const rawSource = dataset.object_kind === "raw_source";
  const preparedMetadata = dataset.prepared_policy?.metadata || {};
  const validationActive = validationOperation && isActive(validationOperation);
  const preparationActive = preparationOperation && isActive(preparationOperation);
  const validationResult =
    operationState(validationOperation) === "completed" ? validationOperation.result : null;
  const validated = ["valid", "completed", "passed"].includes(
    String(dataset.validation_status || "").toLowerCase(),
  );
  const readiness = legacy
    ? { value: "blocked", label: "Legacy artifact blocked" }
    : rawSource
      ? { value: "information", label: "Raw source" }
      : dataset.source_changed
    ? { value: "source_changed", label: "Source changed" }
    : dataset.training_ready
      ? { value: "ready", label: "Ready for training" }
      : validated
        ? { value: "needs_preparation", label: "Ready to prepare" }
        : { value: "needs_validation", label: "Needs validation" };

  return (
    <article className="dataset-detail">
      <header className="dataset-detail__header">
        <div>
          <span className="eyebrow">
            {String(dataset.format || "file").toUpperCase()}
          </span>
          <h2>{dataset.display_name || dataset.name}</h2>
          <p>{dataset.description || "Training material stored on this computer."}</p>
        </div>
        <Status value={readiness.value} label={readiness.label} />
      </header>

      {dataset.source_changed ? (
        <InlineNotice kind="warning" title="The source file changed">
          Validate and prepare this dataset again before using it for training.
        </InlineNotice>
      ) : null}
      {legacy ? (
        <InlineNotice kind="information" title="Raw source preserved safely">
          The source file is intact. Its old prepared artifact used tree identifiers
          instead of assistant text and is permanently blocked. Use Corrected English
          OASST2 for training or evaluation.
        </InlineNotice>
      ) : null}
      {rawSource && !legacy ? (
        <InlineNotice kind="information" title="Raw source">
          This file is preserved unchanged. Validate its mapping before creating a
          separate prepared training artifact.
        </InlineNotice>
      ) : null}

      <div className="dataset-metric-strip" aria-label="Dataset metrics">
        <DatasetMetric label="Source records" value={formatNumber(dataset.record_count)} />
        <DatasetMetric
          label="Prepared sequences"
          value={formatNumber(dataset.prepared_sequence_count || preparedMetadata.prepared_sequence_count)}
        />
        <DatasetMetric
          label="Prepared tokens"
          value={formatNumber(dataset.prepared_token_count)}
        />
        <DatasetMetric
          label="Assistant targets"
          value={formatNumber(preparedMetadata.accepted_target_windows)}
        />
      </div>

      <DefinitionList
        items={[
          { label: "Source file", value: dataset.source_filename, truncate: true },
          {
            label: "Source path",
            value: dataset.source_path,
            mono: true,
            truncate: true,
          },
          {
            label: "Format",
            value: String(dataset.format || "Not reported").toUpperCase(),
          },
          { label: "Size", value: formatBytes(dataset.size_bytes) },
          { label: "Records", value: formatNumber(dataset.record_count) },
          {
            label: "Tokens",
            value: dataset.token_count
              ? formatNumber(dataset.token_count)
              : "Available after preparation",
          },
          { label: "Last used", value: formatDate(dataset.last_used_at) },
          {
            label: "Accepted assistant targets",
            value: preparedMetadata.accepted_target_windows === undefined
              ? undefined
              : formatNumber(preparedMetadata.accepted_target_windows),
          },
          {
            label: "Valid assistant-target tokens",
            value: preparedMetadata.valid_assistant_target_tokens === undefined
              ? undefined
              : formatNumber(preparedMetadata.valid_assistant_target_tokens),
          },
          {
            label: "Intact overlong targets excluded",
            value: preparedMetadata.rejected_intact_overlong_targets === undefined
              ? undefined
              : formatNumber(preparedMetadata.rejected_intact_overlong_targets),
          },
        ]}
      />

      {!legacy && !rawSource ? (
        <section className="workflow-section dataset-learning-preview">
          <div className="workflow-section__heading">
            <div>
              <span className="step-number"><Eye aria-hidden="true" /></span>
              <div>
                <h3>Learning preview</h3>
                <p>
                  Inspect a rendered example and its assistant-only learning target.
                  The source is read only when you request this preview.
                </p>
              </div>
            </div>
            <Button busy={previewBusy} onClick={onLoadPreview}>
              {learningPreview ? "Refresh preview" : "Show learning preview"}
            </Button>
          </div>
          {learningPreview?.formatted_samples?.[0] ? (
            <LearningPreview sample={learningPreview.formatted_samples[0]} />
          ) : null}
        </section>
      ) : null}

      {dataset.training_ready && !legacy ? (
        <section className="workflow-section workflow-section--complete">
          <div className="workflow-section__heading">
            <div>
              <span className="step-number"><Check aria-hidden="true" /></span>
              <div>
                <h3>Preparation verified</h3>
                <p>
                  This selectable artifact passed its stored contract and integrity
                  checks. It is ready for an explicitly started training run.
                </p>
              </div>
            </div>
            <Status value="ready" label="Ready for training" />
          </div>
        </section>
      ) : null}

      {!dataset.training_ready && !legacy && !rawSource ? (
        <>
      <section className="workflow-section">
        <div className="workflow-section__heading">
          <div>
            <span className="step-number">1</span>
            <div>
              <h3>Validate dataset</h3>
              <p>
                Check the source, field mapping, examples, and tokenizer without
                changing the file.
              </p>
            </div>
          </div>
          <Button
            variant="secondary"
            icon={Check}
            busy={starting === "validation"}
            disabled={validationActive || preparationActive}
            onClick={onValidate}
          >
            Validate dataset
          </Button>
        </div>
        {validationOperation ? (
          <OperationProgress operation={validationOperation} compact />
        ) : null}
        {validationResult ? <ValidationReport result={validationResult} /> : null}
      </section>

      <section className="workflow-section">
        <div className="workflow-section__heading">
          <div>
            <span className="step-number">2</span>
            <div>
              <h3>Prepare for training</h3>
              <p>
                {validated && !dataset.source_changed
                  ? "Tokenise, split, save, and verify a selectable prepared dataset."
                  : dataset.source_changed
                    ? "The source file changed since it was validated. Validate it again before preparing."
                    : "Validate the dataset first; preparation runs on a checked source."}
              </p>
            </div>
          </div>
          {

                                                                           }
          <Button
            variant="primary"
            icon={Database}
            disabled={
              !validated
              || Boolean(dataset.source_changed)
              || validationActive
              || preparationActive
            }
            onClick={onPrepare}
          >
            Prepare for training
          </Button>
        </div>
        {preparationOperation ? (
          <OperationProgress
            operation={preparationOperation}
            phases={PREPARATION_PHASES}
          />
        ) : null}
      </section>
        </>
      ) : null}
    </article>
  );
}

function LearningPreview({ sample }) {
  const rendered =
    sample.rendered_training_text ||
    sample.formatted_example ||
    sample.formatted_text;
  return (
    <div className="learning-preview">
      <div>
        <strong>Rendered input</strong>
        <pre>{rendered || "No rendered sample was returned."}</pre>
      </div>
      <DefinitionList
        compact
        items={[
          { label: "Assistant target", value: sample.assistant_target || sample.target_text },
          { label: "Prompt mask", value: sample.label_mask || sample.target_mask },
          { label: "Token count", value: formatNumber(sample.full_token_count || sample.token_count) },
          { label: "Sequence boundaries", value: describeValue(sample.sequence_boundaries) },
          { label: "Fit policy", value: describeValue(sample.fit_diagnostic) },
          { label: "Source branch", value: sample.branch_id || sample.source_branch },
        ]}
      />
    </div>
  );
}

function describeValue(value) {
  if (value === null || value === undefined || value === "") return undefined;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function DatasetMetric({ label, value }) {
  return (
    <div>
      <span>{label}</span>
      <strong>{value || "Not measured"}</strong>
    </div>
  );
}
