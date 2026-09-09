import { Dialog } from "../../components/Dialog.jsx";
import { Checkbox, Field, FormActions } from "../../components/Forms.jsx";
import {
  Button,
  DefinitionList,
  Disclosure,
  InlineNotice,
} from "../../components/Primitives.jsx";
import { formatNumber } from "../../workflows/formatters.js";

export function PreparationDialog({
  open,
  datasetName,
  mappingType,
  preparation,
  setPreparation,
  preflight,
  preflightBusy,
  starting,
  onClose,
  onSubmit,
  onAnalyze,
}) {
  const semantic = ["instruction", "conversation", "oasst2"].includes(
    mappingType,
  );
  const preflightMatches =
    Number(preflight?.settings?.sequence_length) ===
      Number(preparation.sequence_length) &&
    Number(preflight?.settings?.training_split) ===
      Number(preparation.training_split) &&
    Number(preflight?.settings?.validation_split) ===
      Number(preparation.validation_split);
  const coverage = Number(preflight?.target_token_coverage || 0);

  return (
    <Dialog
      className="workbench-dialog"
      open={open}
      title="Prepare for training"
      description={`Create a training copy of ${datasetName || "this dataset"}. Your original file stays unchanged.`}
      onClose={onClose}
    >
      <form onSubmit={onSubmit} className="stack-form">
        <div className="form-grid form-grid--two">
          <Field
            label="Sequence length"
            hint="Training sequence length, independent of the model's context limit."
          >
            <input
              type="number"
              min="32"
              max="8192"
              step="32"
              required
              value={preparation.sequence_length}
              onChange={(event) =>
                setPreparation((value) => ({
                  ...value,
                  sequence_length: Number(event.target.value),
                }))
              }
            />
          </Field>
          <Field label="Training split">
            <input
              type="number"
              min="0.5"
              max="0.99"
              step="0.01"
              required
              value={preparation.training_split}
              onChange={(event) => {
                const training = Number(event.target.value);
                setPreparation((value) => ({
                  ...value,
                  training_split: training,
                  validation_split: Number((1 - training).toFixed(2)),
                }));
              }}
            />
          </Field>
          <Field label="Validation split">
            <input
              type="number"
              readOnly
              value={preparation.validation_split}
            />
          </Field>
        </div>
        <Disclosure summary="Advanced settings">
          <div className="form-grid form-grid--two">
            <Checkbox
              label="Pack examples"
              checked={preparation.packing}
              disabled={semantic}
              onChange={(event) =>
                setPreparation((value) => ({
                  ...value,
                  packing: event.target.checked,
                }))
              }
            />
            <Checkbox
              label="Add BOS token"
              checked={preparation.bos}
              onChange={(event) =>
                setPreparation((value) => ({
                  ...value,
                  bos: event.target.checked,
                }))
              }
            />
            <Checkbox
              label="Add EOS token"
              checked={preparation.eos}
              onChange={(event) =>
                setPreparation((value) => ({
                  ...value,
                  eos: event.target.checked,
                }))
              }
            />
            <Checkbox
              label="Pad sequences"
              checked={preparation.padding}
              onChange={(event) =>
                setPreparation((value) => ({
                  ...value,
                  padding: event.target.checked,
                }))
              }
            />
            <Field label="Truncation">
              <select
                value={preparation.truncation}
                disabled={semantic}
                onChange={(event) =>
                  setPreparation((value) => ({
                    ...value,
                    truncation: event.target.value,
                  }))
                }
              >
                <option value="right">From the right</option>
                <option value="left">From the left</option>
              </select>
            </Field>
            <Field label="Random seed">
              <input
                type="number"
                value={preparation.random_seed}
                onChange={(event) =>
                  setPreparation((value) => ({
                    ...value,
                    random_seed: Number(event.target.value),
                  }))
                }
              />
            </Field>
          </div>
        </Disclosure>
        {semantic ? (
          <InlineNotice kind="information" title="Semantic examples stay isolated">
            Conversation and instruction records are never packed together.
            System, user, and assistant boundaries use the canonical Chat
            template. Assistant targets are kept whole or the target window is
            rejected and counted.
          </InlineNotice>
        ) : null}
        <section className="workflow-section">
          <div className="workflow-section__heading">
            <div>
              <h3>Read-only curriculum preflight</h3>
              <p>
                Measure the exact records, target tokens, truncation, and
                rejection coverage for these settings before writing prepared
                data.
              </p>
            </div>
            <Button
              variant="secondary"
              busy={preflightBusy}
              onClick={onAnalyze}
            >
              Analyze chosen settings
            </Button>
          </div>
          {preflightMatches ? (
            <>
              <DefinitionList
                items={[
                  {
                    label: "Generated examples",
                    value: formatNumber(preflight.generated_record_count),
                  },
                  {
                    label: "Accepted examples",
                    value: formatNumber(preflight.accepted_record_count),
                  },
                  {
                    label: "Rejected examples",
                    value: formatNumber(preflight.rejected_record_count),
                  },
                  {
                    label: "Accepted target tokens",
                    value: formatNumber(preflight.accepted_target_tokens),
                  },
                  {
                    label: "Rejected target tokens",
                    value: formatNumber(preflight.rejected_target_tokens),
                  },
                  {
                    label: "Target-token coverage",
                    value: `${(coverage * 100).toFixed(4)}%`,
                  },
                  {
                    label: "Truncated prompts",
                    value: formatNumber(preflight.truncated_record_count),
                  },
                ]}
              />
              {coverage < 0.99 ? (
                <InlineNotice kind="warning" title="Material target loss">
                  These settings preserve only {(coverage * 100).toFixed(2)}%
                  of generated target tokens. Increase sequence length or
                  intentionally accept this curriculum change.
                </InlineNotice>
              ) : null}
            </>
          ) : (
            <InlineNotice kind="warning" title="Preflight required">
              Analyze the current settings before preparation. Changing a
              sequence or split setting invalidates the previous result.
            </InlineNotice>
          )}
        </section>
        <FormActions>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            type="submit"
            busy={starting === "preparation"}
            disabled={!preflightMatches}
          >
            Prepare for training
          </Button>
        </FormActions>
      </form>
    </Dialog>
  );
}
