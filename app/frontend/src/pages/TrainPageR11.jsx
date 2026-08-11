import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  Clipboard,
  Fingerprint,
  GraduationCap,
  Pause,
  Play,
  Search,
} from "lucide-react";
import { api, asList } from "../api/client.js";
import { Field } from "../components/Forms.jsx";
import { OperationProgress } from "../components/OperationProgress.jsx";
import {
  Button,
  DefinitionList,
  Disclosure,
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
  latestTrainingOperation,
  normaliseToken,
  trainingHeaderState,
} from "../workflows/operations.mjs";
import { phasesForPolicy } from "../workflows/postTraining.mjs";
import { sameSelectionId } from "../workflows/selection.mjs";
import "./TrainPageR11.css";



const POST_TRAINING_POLICY = "evaluate";
const MAX_LOG_ROWS = 300;
const IDENTITY_PHASES = [
  { value: "collecting_baseline_behavior", label: "Capture baseline behavior" },
  { value: "tracing_exact_native_weights", label: "Trace exact model weights" },
  { value: "training_learned_identity_adapter", label: "Train learned adapter" },
  {
    value: "checking_unseen_generations_and_retention",
    label: "Evaluate unseen prompts and retention",
  },
  { value: "promoting_evaluated_identity_adapter", label: "Promote verified adapter" },
  { value: "learned_identity_active", label: "Verify private Chat runtime" },
];

const DEFAULT_SETTINGS = {
  sequence_length: 512,
  micro_batch_size: 16,
  gradient_accumulation: 1,
  learning_rate: 0.0003,
  scheduler: "cosine",
  warmup_steps: 20,
  weight_decay: 0.1,
  gradient_clip: 1,
  precision: "bf16",
  validation_interval: 50,
  seed: 1337,
  device: "cuda",
};

export function TrainPageR11({ onNavigate }) {
  const {
    datasets,
    versions,
    operations,
    trainingSetup,
    trainingStatus,
    refreshDomain,
    reportError,
    startOperation,
  } = useAppState();
  const [purpose, setPurpose] = useState("instruction_following");
  const [datasetId, setDatasetId] = useState("");
  const [versionId, setVersionId] = useState("");
  const [tokenBudget, setTokenBudget] = useState(100_000);
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [safetyConfirmed, setSafetyConfirmed] = useState(false);
  const [starting, setStarting] = useState(false);
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [setupLoading, setSetupLoading] = useState(true);
  const [identityConfirmed, setIdentityConfirmed] = useState(false);
  const [identityStarting, setIdentityStarting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setSetupLoading(true);
    Promise.all([
      refreshDomain("datasets", { quiet: true }),
      refreshDomain("versions", { quiet: true }),
      refreshDomain("training", { quiet: true }),
    ])
      .catch((error) => reportError(error, "open-training-r11"))
      .finally(() => {
        if (!cancelled) setSetupLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshDomain, reportError]);

  useEffect(() => {
    const defaults = trainingSetup?.defaults || trainingSetup?.settings;
    if (defaults) setSettings((previous) => ({ ...previous, ...defaults }));
  }, [trainingSetup]);

  const readyDatasets = useMemo(
    () =>
      datasets.filter(
        (dataset) =>
          dataset.training_ready &&
          dataset.prepared_policy?.classification !== "blocked_legacy",
      ),
    [datasets],
  );
  const eligibleVersions = useMemo(() => {
    const setupVersions = trainingSetup?.starting_versions;
    const candidates =
      Array.isArray(setupVersions) && setupVersions.length ? setupVersions : versions;
    return candidates.filter(
      (version) =>
        version.integrity === "verified" &&
        version.production_policy?.continuation_allowed !== false,
    );
  }, [trainingSetup?.starting_versions, versions]);
  const recommendedVersion =
    eligibleVersions.find((version) => version.production_policy?.recommended) ||
    eligibleVersions.find(
      (version) =>
        version.production_policy?.classification === "recovered_candidate",
    ) ||
    eligibleVersions[0];

  useEffect(() => {
    if (!datasetId && readyDatasets[0]) setDatasetId(readyDatasets[0].id);
    if (datasetId && !readyDatasets.some((item) => sameSelectionId(item.id, datasetId))) {
      setDatasetId(readyDatasets[0]?.id || "");
    }
  }, [datasetId, readyDatasets]);

  useEffect(() => {
    const requestedVersion = getHashParameter("version");
    const requested = eligibleVersions.find((item) => sameSelectionId(item.id, requestedVersion));
    if (requested) {
      setVersionId(requested.id);
    } else if (!versionId && recommendedVersion) {
      setVersionId(recommendedVersion.id);
    } else if (
      versionId &&
      !eligibleVersions.some((item) => sameSelectionId(item.id, versionId))
    ) {
      setVersionId(recommendedVersion?.id || "");
    }
  }, [eligibleVersions, recommendedVersion, versionId]);

  const selectedDataset = readyDatasets.find((item) => sameSelectionId(item.id, datasetId));
  const selectedVersion = eligibleVersions.find((item) => sameSelectionId(item.id, versionId));
  const effectiveBatch =
    positiveNumber(settings.micro_batch_size, 1) *
    positiveNumber(settings.gradient_accumulation, 1);
  const estimatedTokensPerStep = estimateTokensPerStep(
    selectedDataset,
    effectiveBatch,
    settings.sequence_length,
  );
  const additionalSteps = Math.max(
    1,
    Math.ceil(positiveNumber(tokenBudget, 1) / estimatedTokensPerStep),
  );
  const budgetPasses = selectedDataset?.prepared_token_count
    ? Number(tokenBudget) / Number(selectedDataset.prepared_token_count)
    : null;

  useEffect(() => {
    const sequenceLength = Number(selectedDataset?.prepared_settings?.sequence_length);
    if (sequenceLength > 0) {
      setSettings((previous) => ({
        ...previous,
        sequence_length: sequenceLength,
      }));
    }
  }, [selectedDataset?.prepared_settings?.sequence_length]);

  const trainingOperation = useMemo(
    () => latestTrainingOperation(operations, trainingStatus),
    [operations, trainingStatus],
  );
  const trainingActive = Boolean(trainingOperation && isActive(trainingOperation));
  const identitySetup = trainingSetup?.identity_post_training || null;
  const identityOperation = useMemo(
    () =>
      [...Object.values(operations || {}), identitySetup?.latest_operation]
        .filter(Boolean)
        .filter((operation) => normaliseToken(operation.type) === "training_identity")
        .sort(
          (left, right) =>
            Date.parse(right.updated_at || right.updatedAt || 0) -
            Date.parse(left.updated_at || left.updatedAt || 0),
        )[0] || null,
    [identitySetup?.latest_operation, operations],
  );
  const identityActive = Boolean(identityOperation && isActive(identityOperation));
  const header = trainingHeaderState(trainingOperation);
  const canStart =
    Boolean(selectedDataset) &&
    Boolean(selectedVersion) &&
    positiveIntegerOrNull(tokenBudget) !== null &&
    safetyConfirmed &&
    !trainingActive;

  useEffect(() => {
    let cancelled = false;
    setHistoryLoading(true);
    const loader =
      typeof api.getTrainingHistory === "function"
        ? api.getTrainingHistory(30)
        : api.listOperations({ type: "training", limit: 30 });
    Promise.resolve(loader)
      .then((payload) => {
        if (!cancelled) setHistory(asList(payload, ["history", "operations"]));
      })
      .catch((error) => {
        if (!cancelled) reportError(error, "training-history-r11");
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reportError, trainingOperation?.id, trainingOperation?.state]);

  async function startTraining() {
    if (!canStart) return;
    setStarting(true);
    try {
      await startOperation({
        type: "training",
        targetId: selectedDataset.id,
        launch: (requestKey) =>
          api.startTraining(
            {
              prepared_dataset_id:
                selectedDataset.prepared_dataset_id ||
                selectedDataset.prepared_id ||
                selectedDataset.id,
              starting_version_id: selectedVersion.id,
              start_from_initial: false,
              additional_steps: additionalSteps,
              post_training_policy: POST_TRAINING_POLICY,
              settings: {
                ...settings,
                documented_objective: purpose,
              },
            },
            requestKey,
          ),
      });
      setSafetyConfirmed(false);
      await refreshDomain("training", { quiet: true });
    } catch (error) {
      reportError(error, "start-training-r11");
    } finally {
      setStarting(false);
    }
  }

  async function startIdentityPostTraining() {
    if (!identityConfirmed || identityActive || !identitySetup?.available) return;
    setIdentityStarting(true);
    try {
      await startOperation({
        type: "training_identity",
        targetId: identitySetup.model_id,
        launch: (requestKey) => api.startIdentityPostTraining(requestKey),
      });
      setIdentityConfirmed(false);
      await refreshDomain("training", { quiet: true });
    } catch (error) {
      reportError(error, "start-identity-post-training");
    } finally {
      setIdentityStarting(false);
    }
  }

  if (setupLoading) {
    return (
      <div className="page page--train-r11">
        <PageHeader title="Train" />
        <EmptyState
          icon={GraduationCap}
          title="Loading verified training setup"
          description="Checking the prepared data and eligible starting versions."
        />
      </div>
    );
  }

  return (
    <div className="page page--train-r11">
      <PageHeader title="Train">
        <div className="page-status-line">
          <Status value={header.value} label={header.label} />
        </div>
      </PageHeader>

      <section className="r11-identity-card" aria-labelledby="r11-identity-title">
        <header className="r11-identity-card__header">
          <div className="r11-identity-card__title">
            <span className="r11-identity-card__icon" aria-hidden="true">
              <Fingerprint size={20} />
            </span>
            <div>
              <span className="eyebrow">Native learned identity</span>
              <h2 id="r11-identity-title">Base Steak 2.0 identity post-training</h2>
            </div>
          </div>
          <Status
            value={
              identityActive
                ? identityOperation.state
                : identitySetup?.evidence_complete
                  ? "completed"
                  : identitySetup?.available
                    ? "ready"
                    : "warning"
            }
            label={
              identityActive
                ? "Running"
                : identitySetup?.evidence_complete
                  ? "Learned and verified"
                  : identitySetup?.available
                    ? "Ready"
                    : "Unavailable"
            }
          />
        </header>

        <p className="r11-identity-card__lead">
          Trains one rank-512 GGUF identity extension against the exact local weights. No system-prompt
          identity or hardcoded answer is used. A routing-only learned adapter selects the
          identity lane, then activates the learned identity adapter only for that answer.
          Promotion requires every unseen identity prompt and byte-equivalent capability
          retention to pass. The rank-32 routing-only adapter distinguishes response, research, image,
          agent, and identity work, then switches off before the selected work runs.
        </p>

        <div className="r11-identity-evidence" aria-label="Identity training evidence">
          <SummaryFact label="Public identity" value={identitySetup?.model_name || "Base Steak 2.0"} />
          <SummaryFact label="Trainer" value={identitySetup?.trainer || "MD Anik Hasan (Sawlper)"} />
          <SummaryFact label="Training conversations" value={formatNumber(identitySetup?.training_examples || 682)} />
          <SummaryFact label="Unseen prompts" value={formatNumber(identitySetup?.unseen_identity_prompts || 50)} />
          <SummaryFact label="Retention paths" value={formatNumber(identitySetup?.capability_retention_prompts || 40)} />
          <SummaryFact
            label="Adapter scale"
            value={identitySetup?.adapter?.scale === undefined ? "Pending" : String(identitySetup.adapter.scale)}
          />
          <SummaryFact
            label="Activation"
            value={identitySetup?.adapter?.activation === "identity_intent" ? "Model-routed" : "Pending"}
          />
          <SummaryFact
            label="Routing training"
            value={formatNumber(identitySetup?.routing?.training_examples || 0)}
          />
          <SummaryFact
            label="Routing holdout"
            value={
              identitySetup?.routing?.holdout_count
                ? `${formatNumber(identitySetup.routing.holdout_pass_count)}/${formatNumber(identitySetup.routing.holdout_count)}`
                : "Pending"
            }
          />
          <SummaryFact
            label="Routing activation"
            value={identitySetup?.routing_adapter?.activation === "routing_intent" ? "Decision only" : "Pending"}
          />
        </div>

        {identityOperation ? (
          <OperationProgress
            operation={identityOperation}
            phases={IDENTITY_PHASES}
            allowStop
            title="Identity workflow"
            metrics={identityMetrics}
          />
        ) : null}

        {!identityActive ? (
          <div className="r11-identity-card__actions">
            <label className="r11-safety-check">
              <input
                type="checkbox"
                checked={identityConfirmed}
                disabled={!identitySetup?.available}
                onChange={(event) => setIdentityConfirmed(event.target.checked)}
              />
              <span>
                <strong>Run the fixed learned-identity recipe and all gates.</strong>
                <small>
                  Chat keeps its current adapter unless training, unseen generation,
                  retention, checksum, promotion, and private-runtime checks all pass.
                </small>
              </span>
            </label>
            <Button
              variant="primary"
              icon={Fingerprint}
              busy={identityStarting}
              disabled={!identitySetup?.available || !identityConfirmed}
              onClick={startIdentityPostTraining}
            >
              Train and validate identity
            </Button>
          </div>
        ) : null}

        {!identitySetup?.available ? (
          <InlineNotice kind="warning" title="Exact model is not selected">
            {identitySetup?.reason || "Select the verified steak20 Base Steak 2.0 bundle first."}
          </InlineNotice>
        ) : null}
      </section>

      {readyDatasets.length && eligibleVersions.length ? (
        <>
      {trainingOperation ? (
        <div className="r11-training-live">
          <OperationProgress
            operation={trainingOperation}
            phases={phasesForPolicy(
              trainingOperation.result?.post_training_policy ||
                trainingOperation.post_training_policy ||
                POST_TRAINING_POLICY,
            )}
            allowStop
            title="Current run"
            metrics={trainingMetrics}
          />
          <LiveTrainingLog operation={trainingOperation} reportError={reportError} />
        </div>
      ) : null}

      <section className="r11-training-card" aria-labelledby="r11-training-title">
        <header className="r11-training-card__header">
          <div>
            <span className="eyebrow">Training setup</span>
            <h2 id="r11-training-title">Plan one protected run</h2>
          </div>
          <span>Normal settings first · expert controls optional</span>
        </header>

        <div className="r11-training-workstation">
          <div className="r11-training-config">
          <SetupStep number="1" title="Training objective">
            <Field label="What should this run improve?">
              <select
                value={purpose}
                disabled={trainingActive}
                onChange={(event) => {
                  setPurpose(event.target.value);
                  setSafetyConfirmed(false);
                }}
              >
                <option value="instruction_following">Teach conversation and instructions</option>
                <option value="english_continuation">Continue English language learning</option>
                <option value="domain_adaptation">Adapt to a domain</option>
              </select>
            </Field>
            <p>This label documents your intent; the prepared dataset controls the actual curriculum.</p>
          </SetupStep>

          <SetupStep number="2" title="Prepared curriculum">
            <Field label="Verified prepared dataset">
              <select
                value={datasetId}
                disabled={trainingActive}
                onChange={(event) => {
                  setDatasetId(event.target.value);
                  setSafetyConfirmed(false);
                }}
              >
                {readyDatasets.map((dataset) => (
                  <option value={dataset.id} key={dataset.id}>
                    {dataset.display_name || dataset.name} - {formatNumber(dataset.prepared_token_count || dataset.token_count)} tokens
                  </option>
                ))}
              </select>
            </Field>
            <p>
              Verified -{" "}
              {formatNumber(
                selectedDataset?.prepared_record_count ||
                  selectedDataset?.record_count,
              )}{" "}
              prepared sequences
            </p>
          </SetupStep>

          <SetupStep number="3" title="Starting checkpoint">
            <Field label="Verified parent">
              <select
                value={versionId}
                disabled={trainingActive}
                onChange={(event) => {
                  setVersionId(event.target.value);
                  setSafetyConfirmed(false);
                }}
              >
                {eligibleVersions.map((version) => (
                  <option value={version.id} key={version.id}>
                    {friendlyVersionName(version)}
                    {sameSelectionId(version.id, recommendedVersion?.id) ? " - Recommended" : ""}
                  </option>
                ))}
              </select>
            </Field>
            <p>
              {sameSelectionId(selectedVersion?.id, recommendedVersion?.id)
                ? `Recommended: ${selectedVersion?.production_policy?.rationale || "latest bounded candidate allowed by the current policy."}`
                : `Selected: ${versionLabel(selectedVersion)}`}
            </p>
          </SetupStep>

          <SetupStep number="4" title="Run budget">
            <Field
              label="Approximate target-token budget"
              hint="Converted to whole optimizer steps. The service reports the exact assistant-target tokens processed during the run."
            >
              <input
                type="number"
                min="1"
                step="1000"
                value={tokenBudget}
                disabled={trainingActive}
                onChange={(event) => {
                  setTokenBudget(event.target.value);
                  setSafetyConfirmed(false);
                }}
              />
            </Field>
            <p>
              About {formatNumber(additionalSteps)} optimizer steps
              {budgetPasses === null
                ? ""
                : ` · ${budgetPasses.toFixed(2)} prepared-dataset passes by total-token estimate`}
            </p>
          </SetupStep>

          <Disclosure summary="Expert settings" className="r11-expert-settings">
            <div className="form-grid form-grid--three">
              <Field label="Sequence length" hint="Fixed by prepared data">
                <input type="number" readOnly value={settings.sequence_length} />
              </Field>
              <NumberSetting label="Micro batch size" value={settings.micro_batch_size} min={1} onChange={(value) => updateSetting("micro_batch_size", value)} />
              <NumberSetting label="Gradient accumulation" value={settings.gradient_accumulation} min={1} onChange={(value) => updateSetting("gradient_accumulation", value)} />
              <Field label="Effective batch"><input type="number" readOnly value={effectiveBatch} /></Field>
              <NumberSetting label="Learning rate" value={settings.learning_rate} min={0} step={0.00001} onChange={(value) => updateSetting("learning_rate", value)} />
              <Field label="Scheduler"><select value={settings.scheduler} onChange={(event) => updateSetting("scheduler", event.target.value)}><option value="cosine">Cosine</option><option value="linear">Linear</option><option value="constant">Constant</option></select></Field>
              <NumberSetting label="Warmup steps" value={settings.warmup_steps} min={0} onChange={(value) => updateSetting("warmup_steps", value)} />
              <NumberSetting label="Weight decay" value={settings.weight_decay} min={0} step={0.01} onChange={(value) => updateSetting("weight_decay", value)} />
              <NumberSetting label="Gradient clipping" value={settings.gradient_clip} min={0} step={0.1} onChange={(value) => updateSetting("gradient_clip", value)} />
              <Field label="Precision"><select value={settings.precision} onChange={(event) => updateSetting("precision", event.target.value)}><option value="bf16">BF16</option><option value="fp16">FP16</option><option value="fp32">FP32</option></select></Field>
              <NumberSetting label="Validation interval" value={settings.validation_interval} min={1} onChange={(value) => updateSetting("validation_interval", value)} />
              <NumberSetting label="Seed" value={settings.seed} min={0} onChange={(value) => updateSetting("seed", value)} />
              <Field label="Device"><select value={settings.device} onChange={(event) => updateSetting("device", event.target.value)}><option value="cuda">NVIDIA GPU (CUDA)</option><option value="cpu">CPU</option></select></Field>
            </div>
          </Disclosure>
          </div>

          <aside className="r11-run-summary" aria-label="Run review">
            <div className="r11-run-summary__heading">
              <span className="eyebrow">Review</span>
              <h3>Run summary</h3>
              <p>Calculated from the selected immutable inputs.</p>
            </div>
            <dl className="r11-run-summary__facts">
              <SummaryFact label="Curriculum" value={selectedDataset?.display_name || selectedDataset?.name} />
              <SummaryFact label="Starting checkpoint" value={friendlyVersionName(selectedVersion)} />
              <SummaryFact label="Checkpoint class" value={selectedVersion?.production_policy?.classification || "Verified"} />
              <SummaryFact label="Target-token budget" value={formatNumber(tokenBudget)} />
              <SummaryFact label="Estimated optimizer steps" value={formatNumber(additionalSteps)} />
              <SummaryFact label="Estimated dataset passes" value={budgetPasses === null ? "Not available" : budgetPasses.toFixed(3)} />
              <SummaryFact label="Effective batch" value={formatNumber(effectiveBatch)} />
              <SummaryFact label="Runtime" value={`${String(settings.device).toUpperCase()} · ${String(settings.precision).toUpperCase()} · ${formatNumber(settings.sequence_length)} tokens`} />
            </dl>
            <InlineNotice kind="information" title="Automatic protection">
              The stage holdout plus checkpoint and integrity checks are automatic.
              The result is saved but not activated. The fixed English-retention
              anchor and deterministic generation acceptance suite are not part of
              a normal training run.
            </InlineNotice>
            <label className="r11-safety-check">
              <input
                type="checkbox"
                checked={safetyConfirmed}
                disabled={trainingActive}
                onChange={(event) => setSafetyConfirmed(event.target.checked)}
              />
              <span>
                <strong>I reviewed the parent, data, and budget.</strong>
                <small>
                  The current Chat model remains active. Review the saved result and
                  its quality evidence in Versions before any manual activation.
                </small>
              </span>
            </label>
            <Button
              variant="primary"
              icon={Play}
              busy={starting}
              disabled={!canStart}
              onClick={startTraining}
            >
              Start protected training
            </Button>
          </aside>
        </div>
      </section>

      <TrainingHistory runs={history} loading={historyLoading} />
        </>
      ) : (
        <section className="r11-training-card" aria-label="General training prerequisites">
          <EmptyState
            icon={GraduationCap}
            title={
              !readyDatasets.length
                ? "Prepare verified training data for general training"
                : "A verified starting version is required for general training"
            }
            description={
              !readyDatasets.length
                ? "The learned identity and routing workflow above is independent. Add a prepared dataset only for a separate general training run."
                : "The learned identity and routing workflow above remains available while general checkpoints are prepared."
            }
            action={
              <Button
                variant="primary"
                onClick={() => onNavigate(!readyDatasets.length ? "data" : "versions")}
              >
                Go to {!readyDatasets.length ? "Data" : "Versions"}
              </Button>
            }
          />
        </section>
      )}
    </div>
  );

  function updateSetting(key, value) {
    setSettings((previous) => ({ ...previous, [key]: value }));
    setSafetyConfirmed(false);
  }
}

function SetupStep({ number, title, children }) {
  return (
    <section className="r11-setup-step">
      <span className="r11-setup-step__number" aria-hidden="true">{number}</span>
      <div>
        <h3>{title}</h3>
        {children}
      </div>
    </section>
  );
}

function SummaryFact({ label, value }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value || "Not reported"}</dd>
    </div>
  );
}

function NumberSetting({ label, value, onChange, ...props }) {
  return (
    <Field label={label}>
      <input
        type="number"
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        {...props}
      />
    </Field>
  );
}

function LiveTrainingLog({ operation, reportError }) {
  const [expanded, setExpanded] = useState(false);
  const [paused, setPaused] = useState(false);
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState([]);
  const lastSnapshotRef = useRef("");

  useEffect(() => {
    if (!operation || paused) return;
    const snapshot = snapshotRow(operation);
    const signature = JSON.stringify(snapshot);
    if (signature === lastSnapshotRef.current) return;
    lastSnapshotRef.current = signature;
    setRows((previous) => appendBounded(previous, [snapshot]));
  }, [
    operation,
    operation?.updated_at,
    operation?.phase,
    operation?.current_progress,
    operation?.training_loss,
    paused,
  ]);

  useEffect(() => {
    if (
      !expanded ||
      paused ||
      !operation?.id ||
      typeof api.getOperationEvents !== "function"
    ) {
      return undefined;
    }
    let cancelled = false;
    let timer;
    let afterSequence = 0;
    async function pollEvents() {
      try {
        const payload = await api.getOperationEvents(operation.id, {
          after_sequence: afterSequence,
          limit: MAX_LOG_ROWS,
        });
        const events = asList(payload, ["events"]);
        if (events.length && !cancelled) {
          afterSequence = Math.max(
            afterSequence,
            ...events.map((item) => Number(item.sequence) || 0),
          );
          setRows((previous) =>
            appendBounded(previous, events.map(eventRow)),
          );
        }
      } catch (error) {
        if (!cancelled) reportError(error, `training-log:${operation.id}`);
      } finally {
        if (!cancelled && isActive(operation)) {
          timer = window.setTimeout(pollEvents, 1200);
        }
      }
    }
    void pollEvents();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [expanded, operation?.id, operation?.state, paused, reportError]);

  const token = query.trim().toLowerCase();
  const visibleRows = token
    ? rows.filter((row) => JSON.stringify(row).toLowerCase().includes(token))
    : rows;

  async function copyRows() {
    const text = visibleRows.map(formatLogRow).join("\n");
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      reportError(new Error("The live log could not be copied."), "copy-training-log");
    }
  }

  return (
    <section className="r11-live-log">
      <button
        type="button"
        className="r11-live-log__toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span>
          <strong>Live training log</strong>
          <small>{rows.length} structured snapshots kept locally · maximum {MAX_LOG_ROWS}</small>
        </span>
        <ChevronDown aria-hidden="true" />
      </button>
      {expanded ? (
        <div className="r11-live-log__body">
          <div className="r11-live-log__tools">
            <label className="search-field">
              <Search aria-hidden="true" />
              <span className="sr-only">Search live training log</span>
              <input
                type="search"
                placeholder="Filter the log"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <Button icon={paused ? Play : Pause} onClick={() => setPaused((value) => !value)}>
              {paused ? "Continue updates" : "Pause view"}
            </Button>
            <Button icon={Clipboard} disabled={!visibleRows.length} onClick={copyRows}>
              Copy
            </Button>
          </div>
          <div className="r11-live-log__stream" role="log" aria-live={paused ? "off" : "polite"}>
            {visibleRows.length ? (
              visibleRows.map((row) => (
                <div className="r11-live-log__row" key={row.key}>
                  <time>{formatLogTime(row.time)}</time>
                  <span>{row.phase}</span>
                  <code>{row.message}</code>
                </div>
              ))
            ) : (
              <p>No matching snapshots.</p>
            )}
          </div>
          <p className="r11-live-log__footnote">
            This bounded view does not alter or truncate durable training evidence.
          </p>
        </div>
      ) : null}
    </section>
  );
}

function TrainingHistory({ runs, loading }) {
  return (
    <Disclosure
      summary={loading ? "Training history · loading" : `Training history · ${runs.length}`}
      className="r11-training-history"
    >
      {!runs.length && !loading ? (
        <p>No previous training runs are registered.</p>
      ) : (
        <div className="r11-training-history__rows">
          {runs.map((run) => {
            const state = normaliseToken(run.state || "unknown");
            const result = run.result || {};
            return (
              <article key={run.id}>
                <div>
                  <strong>{run.display_name || "Training run"}</strong>
                  <span>{formatDate(run.updated_at || run.created_at)}</span>
                </div>
                <Status
                  value={state}
                  label={state === "failed" ? "Needs attention" : undefined}
                  size="small"
                />
                <span>
                  {formatNumber(
                    run.current_step ??
                      result.current_step ??
                      run.current_progress ??
                      0,
                  )}{" "}
                  steps
                </span>
              </article>
            );
          })}
        </div>
      )}
      <p className="r11-history-note">
        Policy-hidden maintenance attempts stay in technical records, outside this
        normal workflow.
      </p>
    </Disclosure>
  );
}

function identityMetrics(operation) {
  const result = operation.result || {};
  const metrics = result.metrics || {};
  return [
    {
      label: "Current evidence",
      value: operation.phase || operation.state || "Waiting",
    },
    {
      label: "Identity prompts passed",
      value:
        metrics.identity_pass_count === undefined
          ? "Pending"
          : `${formatNumber(metrics.identity_pass_count)} of ${formatNumber(metrics.identity_count)}`,
    },
    {
      label: "Exact retention paths",
      value:
        metrics.retention_exact_baseline_count === undefined
          ? "Pending"
          : `${formatNumber(metrics.retention_exact_baseline_count)} of ${formatNumber(metrics.retention_count)}`,
    },
    {
      label: "Hardcoded response",
      value: "Never used",
    },
  ];
}

function trainingMetrics(operation) {
  const progress = operation.progress || operation.result || {};
  return [
    {
      label: "Training step",
      value: `${formatNumber(
        operation.current_step ?? progress.current_step ?? operation.current_progress,
      )} of ${formatNumber(
        operation.target_steps ?? progress.target_steps ?? operation.total_progress,
      )}`,
    },
    {
      label: "Measured target tokens",
      value: formatNumber(
        operation.valid_target_tokens_processed ??
          progress.valid_target_tokens_processed,
      ),
    },
    {
      label: "Training loss",
      value: numberOrStatus(
        operation.training_loss ?? progress.training_loss,
        "Not measured yet",
      ),
    },
    {
      label: "Validation loss",
      value: numberOrStatus(
        operation.validation_loss ?? progress.validation_loss,
        "Not measured yet",
      ),
    },
    {
      label: "Speed",
      value: formatRate(
        operation.tokens_per_second ?? progress.tokens_per_second,
        "tokens/sec",
      ),
    },
    {
      label: "Elapsed",
      value: formatDuration(operation.elapsed_seconds ?? progress.elapsed_seconds),
    },
    {
      label: "Remaining",
      value:
        operation.remaining_seconds ?? progress.remaining_seconds
          ? formatDuration(
              operation.remaining_seconds ?? progress.remaining_seconds,
            )
          : "Not reliable yet",
    },
    {
      label: "Worker",
      value: operation.worker_pid
        ? `Local process ${operation.worker_pid}`
        : "Waiting for local worker",
    },
  ];
}

function estimateTokensPerStep(dataset, effectiveBatch, sequenceLength) {
  const records = Number(
    dataset?.prepared_record_count || dataset?.record_count || 0,
  );
  const tokens = Number(
    dataset?.prepared_token_count || dataset?.token_count || 0,
  );
  const meanTokens = records > 0 && tokens > 0 ? tokens / records : sequenceLength;
  return Math.max(1, Math.min(meanTokens, sequenceLength) * effectiveBatch);
}

function snapshotRow(operation) {
  const result = operation.progress || operation.result || {};
  const step =
    operation.current_step ?? result.current_step ?? operation.current_progress;
  const loss = operation.training_loss ?? result.training_loss;
  return {
    key: `snapshot-${operation.updated_at || Date.now()}-${step ?? "phase"}`,
    time: operation.updated_at || new Date().toISOString(),
    phase: String(operation.phase || operation.state || "Training"),
    message: [
      step === null || step === undefined ? null : `step=${step}`,
      Number.isFinite(Number(loss)) ? `loss=${Number(loss).toFixed(6)}` : null,
      Number.isFinite(Number(result.valid_target_tokens_processed))
        ? `target_tokens=${formatNumber(result.valid_target_tokens_processed)}`
        : null,
    ]
      .filter(Boolean)
      .join(" · ") || "Status updated",
  };
}

function eventRow(event) {
  const evidence = event.evidence || event.details || {};
  return {
    key: `event-${event.sequence ?? event.id ?? JSON.stringify(event)}`,
    time: event.created_at || event.timestamp || new Date().toISOString(),
    phase: String(event.phase || event.event_type || event.type || "Event"),
    message:
      evidence.message ||
      evidence.detail ||
      Object.entries(evidence)
        .slice(0, 6)
        .map(([key, value]) => `${key}=${serialiseLogValue(value)}`)
        .join(" · ") ||
      String(event.state || "Recorded"),
  };
}

function appendBounded(previous, incoming) {
  const byKey = new Map(previous.map((row) => [row.key, row]));
  for (const row of incoming) byKey.set(row.key, row);
  return [...byKey.values()].slice(-MAX_LOG_ROWS);
}

function serialiseLogValue(value) {
  if (value === null || value === undefined) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function formatLogRow(row) {
  return `${row.time || ""}\t${row.phase}\t${row.message}`;
}

function formatLogTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "--:--:--"
    : date.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}

function friendlyVersionName(version) {
  return (
    version?.friendly_name ||
    version?.production_policy?.friendly_name ||
    version?.display_label ||
    version?.label ||
    versionLabel(version)
  );
}

function numberOrStatus(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(4) : fallback;
}

function positiveNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

function positiveIntegerOrNull(value) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function getHashParameter(name) {
  const query = window.location.hash.split("?")[1] || "";
  return new URLSearchParams(query).get(name);
}
