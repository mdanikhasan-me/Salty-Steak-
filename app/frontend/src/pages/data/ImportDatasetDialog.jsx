import { useEffect, useState } from "react";
import { api } from "../../api/client.js";
import { Dialog } from "../../components/Dialog.jsx";
import { InlineNotice } from "../../components/Primitives.jsx";
import { useAppState } from "../../state/AppState.jsx";
import { errorMessage } from "../../workflows/formatters.js";
import {
  ChooseFileStep,
  DescribeDatasetStep,
  InspectSourceStep,
  MapFieldsStep,
  ReviewDatasetStep,
} from "./ImportDatasetSteps.jsx";
import {
  mappingIsValid,
  stripExtension,
} from "./importDatasetUtils.js";

const EMPTY_MAPPING = { type: "plain_text", plain_text: "" };
const EMPTY_METADATA = {
  name: "",
  language: "",
  purpose: "",
  description: "",
};

export function ImportDatasetDialog({ open, onClose, onAdded }) {
  const { reportError } = useAppState();
  const [step, setStep] = useState(1);
  const [file, setFile] = useState(null);
  const [localPath, setLocalPath] = useState("");
  const [inspection, setInspection] = useState(null);
  const [mapping, setMapping] = useState(EMPTY_MAPPING);
  const [metadata, setMetadata] = useState(EMPTY_METADATA);
  const [busy, setBusy] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setStep(1);
      setFile(null);
      setLocalPath("");
      setInspection(null);
      setMapping(EMPTY_MAPPING);
      setMetadata(EMPTY_METADATA);
      setError("");
      setBusy(false);
      setPreviewBusy(false);
      setPreview(null);
    }
  }, [open]);

  const fields = inspection?.detected_fields || [];

  async function inspect() {
    if (!file && !localPath.trim()) {
      setError("Choose a supported local file.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = file
        ? await api.inspectDatasetFile(file)
        : await api.inspectDatasetPath(localPath.trim());
      setInspection(result);
      setPreview(null);
      const detectedFields = result.detected_fields || [];
      const looksLikeOasst2 =
        detectedFields.includes("message_tree_id") &&
        (detectedFields.includes("prompt") || detectedFields.includes("parent_id"));
      if (looksLikeOasst2) {
        setMapping({
          type: "oasst2",
          branch_policy: "best_ranked_leaf",
          include_negative_reviews: "false",
          languages: "",
        });
      } else {
        const firstField = detectedFields[0] || "";
        const plain = result.format === "txt" ? "text" : firstField;
        setMapping({ type: "plain_text", plain_text: plain });
      }
      setMetadata((value) => ({
        ...value,
        name:
          value.name ||
          stripExtension(result.source_filename || file?.name || "Dataset"),
      }));
      setStep(2);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setBusy(false);
    }
  }

  async function generatePreview() {
    if (!inspection?.path || !mappingIsValid(mapping)) return;
    setPreviewBusy(true);
    setError("");
    try {
      setPreview(await api.previewDataset(inspection.path, mapping));
    } catch (requestError) {
      setPreview(null);
      setError(errorMessage(requestError));
    } finally {
      setPreviewBusy(false);
    }
  }

  async function addDataset() {
    if (
      !metadata.name.trim() ||
      !metadata.language.trim() ||
      !metadata.purpose.trim()
    ) {
      setError("Enter a dataset name, language, and purpose.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const dataset = await api.addDataset({
        path: inspection.path,
        upload_token: inspection.upload_token,
        name: metadata.name.trim(),
        language: metadata.language.trim(),
        purpose: metadata.purpose.trim(),
        description: metadata.description.trim() || undefined,
        mapping,
      });
      await onAdded(dataset?.dataset || dataset);
    } catch (requestError) {
      setError(errorMessage(requestError));
      reportError(requestError, `add-dataset:${Date.now()}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      wide
      title="Add dataset"
      description="Add training material in five deliberate steps. Nothing starts automatically."
      onClose={onClose}
    >
      <ol className="wizard-steps" aria-label="Import progress">
        {["Choose file", "Inspect", "Map fields", "Describe", "Review"].map(
          (label, index) => (
            <li
              key={label}
              className={
                step === index + 1
                  ? "wizard-step--active"
                  : step > index + 1
                    ? "wizard-step--done"
                    : ""
              }
            >
              <span>{step > index + 1 ? "✓" : index + 1}</span>
              <span>{label}</span>
            </li>
          ),
        )}
      </ol>

      {error ? (
        <InlineNotice kind="error" title="Cannot continue">
          {error}
        </InlineNotice>
      ) : null}

      {step === 1 ? (
        <ChooseFileStep
          file={file}
          setFile={setFile}
          localPath={localPath}
          setLocalPath={setLocalPath}
          setError={setError}
          busy={busy}
          onInspect={inspect}
          onClose={onClose}
        />
      ) : null}

      {step === 2 && inspection ? (
        <InspectSourceStep
          inspection={inspection}
          fields={fields}
          onBack={() => setStep(1)}
          onContinue={() => setStep(3)}
        />
      ) : null}

      {step === 3 ? (
        <MapFieldsStep
          mapping={mapping}
          setMapping={(value) => {
            setMapping(value);
            setPreview(null);
          }}
          fields={fields}
          mappingValid={mappingIsValid(mapping)}
          preview={preview}
          previewBusy={previewBusy}
          onPreview={generatePreview}
          onBack={() => setStep(2)}
          onContinue={() => setStep(4)}
        />
      ) : null}

      {step === 4 ? (
        <DescribeDatasetStep
          metadata={metadata}
          setMetadata={setMetadata}
          onBack={() => setStep(3)}
          onContinue={() => setStep(5)}
        />
      ) : null}

      {step === 5 ? (
        <ReviewDatasetStep
          metadata={metadata}
          inspection={inspection}
          mapping={mapping}
          busy={busy}
          onBack={() => setStep(4)}
          onAdd={addDataset}
        />
      ) : null}
    </Dialog>
  );
}
