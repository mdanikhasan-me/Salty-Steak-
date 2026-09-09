import { Field, FormActions } from "../../components/Forms.jsx";
import {
  Button,
  DefinitionList,
  InlineNotice,
  Status,
} from "../../components/Primitives.jsx";
import { formatBytes, formatNumber } from "../../workflows/formatters.js";
import { SUPPORTED_EXTENSIONS } from "./constants.js";
import { mappingName } from "./importDatasetUtils.js";

export function ChooseFileStep({
  file,
  setFile,
  localPath,
  setLocalPath,
  setError,
  busy,
  onInspect,
  onClose,
}) {
  return (
    <div className="wizard-panel">
      <h3>Choose a supported local file</h3>
      <p>
        Supported formats: plain text, JSONL, JSON, CSV, Parquet, Gzip,
        Zstandard, and a ZIP containing one supported dataset file.
      </p>
      <Field label="Local file">
        <input
          type="file"
          accept={SUPPORTED_EXTENSIONS}
          onChange={(event) => {
            setFile(event.target.files?.[0] || null);
            setError("");
          }}
        />
      </Field>
      <div className="or-separator">
        <span>or enter an exact path</span>
      </div>
      <Field
        label="Source path"
        hint="Useful when the desktop file picker is unavailable."
      >
        <input
          type="text"
          value={localPath}
          placeholder={"P:\\Training material\\examples.jsonl"}
          onChange={(event) => {
            setLocalPath(event.target.value);
            setError("");
          }}
        />
      </Field>
      <FormActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="primary" busy={busy} onClick={onInspect}>
          Inspect file
        </Button>
      </FormActions>
    </div>
  );
}

export function InspectSourceStep({ inspection, fields, onBack, onContinue }) {
  return (
    <div className="wizard-panel">
      <div className="wizard-heading">
        <div>
          <h3>Inspect the source</h3>
          <p>These details and samples come directly from the selected file.</p>
        </div>
        <Status value="completed" label="Inspection complete" />
      </div>
      <DefinitionList
        items={[
          {
            label: "Detected format",
            value: String(inspection.format || "Not reported").toUpperCase(),
          },
          {
            label: "Compression",
            value: String(inspection.compression || "none").toUpperCase(),
          },
          { label: "Encoding", value: inspection.encoding || "Not reported" },
          { label: "File size", value: formatBytes(inspection.size_bytes) },
          {
            label: "Record estimate",
            value: formatNumber(inspection.record_estimate),
          },
          {
            label: "Detected fields",
            value: fields.length ? fields.join(", ") : "Plain text",
          },
        ]}
      />
      <section className="sample-records">
        <h4>Real sample records</h4>
        {inspection.samples?.length ? (
          inspection.samples.map((sample, index) => (
            <pre key={index}>{typeof sample === "string" ? sample : JSON.stringify(sample, null, 2)}</pre>
          ))
        ) : (
          <p>No sample records were returned.</p>
        )}
      </section>
      <FormActions>
        <Button onClick={onBack}>Back</Button>
        <Button variant="primary" onClick={onContinue}>
          Map fields
        </Button>
      </FormActions>
    </div>
  );
}

export function MapFieldsStep({
  mapping,
  setMapping,
  fields,
  mappingValid,
  preview,
  previewBusy,
  onPreview,
  onBack,
  onContinue,
}) {
  return (
    <div className="wizard-panel">
      <h3>Map fields</h3>
      <p>Tell Salty Steak how the source becomes a training example.</p>
      <Field label="Example structure">
        <select
          value={mapping.type}
          onChange={(event) => setMapping({ type: event.target.value })}
        >
          <option value="plain_text">Plain text</option>
          <option value="instruction">Instruction and response</option>
          <option value="conversation">Conversation messages</option>
          <option value="oasst2">OASST2 conversation trees</option>
        </select>
      </Field>
      <MappingFields
        type={mapping.type}
        fields={fields}
        value={mapping}
        onChange={setMapping}
      />
      <section className="formatted-example">
        <div className="wizard-heading">
          <div>
            <h4>Training preview</h4>
            <p>
              Check a real example using the same formatting and token limits
              as dataset preparation.
            </p>
          </div>
          <Button
            variant="secondary"
            busy={previewBusy}
            disabled={!mappingValid}
            onClick={onPreview}
          >
            Generate exact preview
          </Button>
        </div>
        {preview?.formatted_samples?.[0] ? (
          <div className="preview-contract">
            <h5>Rendered training text</h5>
            <pre>
              {preview.formatted_samples[0].rendered_training_text ||
                preview.formatted_samples[0].formatted_example}
            </pre>
            <DefinitionList
              items={[
                {
                  label: "Template",
                  value: preview.preview_contract?.template,
                },
                {
                  label: "Label mask",
                  value: preview.formatted_samples[0].label_mask,
                },
                {
                  label: "Full token count",
                  value: formatNumber(
                    preview.formatted_samples[0].full_token_count,
                  ),
                },
                {
                  label: "Prepared sequence length",
                  value: formatNumber(
                    preview.formatted_samples[0].token_ids?.length,
                  ),
                },
              ]}
            />
            {preview.formatted_samples[0].fit_diagnostic ? (
              <InlineNotice
                kind="warning"
                title="This target does not fit the preview sequence"
              >
                The full assistant target would require{" "}
                {formatNumber(
                  preview.formatted_samples[0].fit_diagnostic
                    .required_minimum_tokens,
                )}{" "}
                tokens. Preparation rejects this target window instead of
                truncating its answer.
              </InlineNotice>
            ) : null}
            <h5>Token IDs</h5>
            <pre>
              {JSON.stringify(
                preview.formatted_samples[0].token_ids,
                null,
                2,
              )}
            </pre>
            <h5>Decoded token round-trip</h5>
            <pre>
              {preview.formatted_samples[0].decoded_token_round_trip}
            </pre>
            <h5>Target mask (target or masked per token)</h5>
            <pre>
              {JSON.stringify(
                preview.formatted_samples[0].target_mask,
                null,
                2,
              )}
            </pre>
            <h5>Sequence boundaries</h5>
            <pre>
              {JSON.stringify(
                preview.formatted_samples[0].sequence_boundaries,
                null,
                2,
              )}
            </pre>
          </div>
        ) : (
          <InlineNotice kind="information">
            Generate this preview before continuing. No dataset is changed.
          </InlineNotice>
        )}
      </section>
      <FormActions>
        <Button onClick={onBack}>Back</Button>
        <Button
          variant="primary"
          disabled={!mappingValid || !preview?.formatted_samples?.length}
          onClick={onContinue}
        >
          Continue
        </Button>
      </FormActions>
    </div>
  );
}

export function DescribeDatasetStep({
  metadata,
  setMetadata,
  onBack,
  onContinue,
}) {
  const complete =
    metadata.name.trim() &&
    metadata.language.trim() &&
    metadata.purpose.trim();

  return (
    <div className="wizard-panel">
      <h3>Describe this dataset</h3>
      <div className="form-grid form-grid--two">
        <Field label="Dataset name">
          <input
            type="text"
            required
            value={metadata.name}
            onChange={(event) =>
              setMetadata((value) => ({ ...value, name: event.target.value }))
            }
          />
        </Field>
        <Field label="Language">
          <input
            type="text"
            required
            placeholder="English"
            value={metadata.language}
            onChange={(event) =>
              setMetadata((value) => ({
                ...value,
                language: event.target.value,
              }))
            }
          />
        </Field>
        <Field label="Purpose" className="field--wide">
          <input
            type="text"
            required
            placeholder="Instruction following, domain writing, conversation"
            value={metadata.purpose}
            onChange={(event) =>
              setMetadata((value) => ({
                ...value,
                purpose: event.target.value,
              }))
            }
          />
        </Field>
        <Field label="Description (optional)" className="field--wide">
          <textarea
            rows="3"
            value={metadata.description}
            onChange={(event) =>
              setMetadata((value) => ({
                ...value,
                description: event.target.value,
              }))
            }
          />
        </Field>
      </div>
      <FormActions>
        <Button onClick={onBack}>Back</Button>
        <Button variant="primary" disabled={!complete} onClick={onContinue}>
          Review
        </Button>
      </FormActions>
    </div>
  );
}

export function ReviewDatasetStep({
  metadata,
  inspection,
  mapping,
  busy,
  onBack,
  onAdd,
}) {
  return (
    <div className="wizard-panel">
      <h3>Review and add</h3>
      <p className="import-review-note">
        Add this source to your library. You can validate and prepare it there
        before starting any training.
      </p>
      <DefinitionList
        items={[
          { label: "Dataset name", value: metadata.name },
          { label: "Source file", value: inspection?.source_filename },
          { label: "Source path", value: inspection?.path, mono: true },
          {
            label: "Format",
            value: String(inspection?.format || "").toUpperCase(),
          },
          { label: "Language", value: metadata.language },
          { label: "Purpose", value: metadata.purpose },
          { label: "Mapping", value: mappingName(mapping.type) },
        ]}
      />
      <FormActions>
        <Button onClick={onBack}>Back</Button>
        <Button variant="primary" busy={busy} onClick={onAdd}>
          Add dataset
        </Button>
      </FormActions>
    </div>
  );
}

function MappingFields({ type, fields, value, onChange }) {
  const options = fields.length ? fields : ["text"];
  const select = (key, label, optional = false) => (
    <Field label={`${label}${optional ? " (optional)" : ""}`}>
      <select
        value={value[key] || ""}
        onChange={(event) =>
          onChange({ ...value, [key]: event.target.value })
        }
      >
        <option value="">{optional ? "Not mapped" : "Choose a field"}</option>
        {options.map((field) => (
          <option value={field} key={field}>
            {field}
          </option>
        ))}
      </select>
    </Field>
  );

  if (type === "plain_text") return select("plain_text", "Plain text");

  if (type === "oasst2") {
    return (
      <div className="form-grid form-grid--two">
        <Field
          label="Languages (optional)"
          hint="Comma-separated OASST language codes such as en, es. Leave empty to keep all languages."
        >
          <input
            type="text"
            value={value.languages || ""}
            placeholder="en"
            onChange={(event) =>
              onChange({ ...value, languages: event.target.value })
            }
          />
        </Field>
        <Field
          label="Branch selection"
          hint="One deterministic branch per tree prevents shared-prefix duplication."
        >
          <select
            value={value.branch_policy || "best_ranked_leaf"}
            onChange={(event) =>
              onChange({ ...value, branch_policy: event.target.value })
            }
          >
            <option value="best_ranked_leaf">Best ranked assistant-ending branch</option>
          </select>
        </Field>
        <InlineNotice kind="information" title="Assistant targets only">
          Deleted and negative-review branches are rejected. User text and role
          markers provide context but do not contribute to instruction-tuning loss.
        </InlineNotice>
      </div>
    );
  }

  if (type === "conversation") {
    return (
      <div className="form-grid form-grid--two">
        {select("conversation_messages", "Conversation messages")}
        {select("system_content", "System content", true)}
      </div>
    );
  }

  return (
    <div className="form-grid form-grid--two">
      {select("instruction", "Instruction")}
      {select("user_input", "User input", true)}
      {select("assistant_response", "Assistant response")}
      {select("system_content", "System content", true)}
    </div>
  );
}
