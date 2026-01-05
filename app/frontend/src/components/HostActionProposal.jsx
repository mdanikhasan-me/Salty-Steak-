import { useEffect, useState } from "react";
import {
  CircleAlert,
  Image,
  RotateCcw,
  Square,
  Terminal,
  Trash2,
} from "lucide-react";
import { Dialog } from "./Dialog.jsx";
import {
  isActive,
  measurableProgress,
  operationState,
  phaseLabel,
} from "../workflows/operations.mjs";
import { createFreshImageSeed } from "../workflows/generatedImages.mjs";
import { TEMP_CLEANUP_CONFIRMATION } from "../workflows/hostActions.mjs";

const DEFAULT_IMAGE_SETTINGS = Object.freeze({
  width: 512,
  height: 512,
  num_inference_steps: 8,
});

export function HostActionProposal({
  proposal,
  operation = null,
  busy = false,
  onReview,
  onConfirm,
  onStop,
  onDismiss,
}) {
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [cleanupAcknowledged, setCleanupAcknowledged] = useState(false);
  const [seed, setSeed] = useState(() => createFreshImageSeed());
  const image = proposal?.kind === "image.generate";
  const fileTrash = proposal?.kind === "filesystem.trash_file";
  const tempCleanup = proposal?.kind === "system.clean_temp";
  const directAction = image || fileTrash || tempCleanup;
  const Icon = image ? Image : (fileTrash || tempCleanup) ? Trash2 : Terminal;
  const proposalState = String(proposal?.state || "");
  const currentState = operation ? operationState(operation) : proposalState;
  const running = isActive(operation);
  const failed = ["failed", "interrupted", "cancelled"].includes(currentState);
  const completed = currentState === "completed" || proposalState === "completed";
  const reviewable = proposalState === "pending_review";
  const blocked = proposalState.startsWith("blocked_");
  const progress = running ? measurableProgress(operation) : null;

  useEffect(() => {
    if (running || completed) setConfirmationOpen(false);
  }, [completed, running]);

  if (!proposal) return null;

  function openActionConfirmation() {
    if (busy || running || completed || blocked) return;
    if (tempCleanup) setCleanupAcknowledged(false);
    setConfirmationOpen(true);
  }

  function confirmAction(event) {
    event.preventDefault();
    if (tempCleanup) {
      if (!cleanupAcknowledged) return;
      onConfirm?.(proposal, { confirmation_text: TEMP_CLEANUP_CONFIRMATION });
      return;
    }
    if (fileTrash) {
      onConfirm?.(proposal, null);
      return;
    }
    const checkedSeed = Number(seed);
    if (!Number.isSafeInteger(checkedSeed) || checkedSeed < 0) return;
    onConfirm?.(proposal, {
      ...DEFAULT_IMAGE_SETTINGS,
      seed: checkedSeed,
    });
  }

  return (
    <>
      <section
        className={`host-action host-action--${blocked ? "blocked" : currentState || "pending"}`}
        aria-label={proposal.title}
        aria-busy={running || undefined}
      >
        <span className="host-action__icon" aria-hidden="true"><Icon /></span>
          <div className="host-action__copy">
            <div className="host-action__heading">
              <strong>{proposal.title}</strong>
              {!running ? <span>{hostActionOperationLabel(proposal, operation)}</span> : null}
            </div>
            <p>{proposal.summary}</p>
            {running ? (
              <div className="host-action__progress" role="status" aria-live="polite">
                <span className="activity-phrase activity-phrase--compact">
                  {phaseLabel(operation)}
                </span>
                {progress ? (
                  <progress
                  max={progress.total}
                  value={progress.current}
                  aria-label={`${Math.round(progress.percentage)} percent complete`}
                />
                ) : null}
              </div>
            ) : null}
            {image && running ? (
              <div className="image-generation-stage" aria-hidden="true">
                <span className="image-generation-stage__field" />
              </div>
            ) : null}
            {proposal.runtimeReason ? (
            <small><CircleAlert aria-hidden="true" />{proposal.runtimeReason}</small>
          ) : null}
        </div>
        <div className="host-action__actions">
          {image && blocked && typeof onReview === "function" ? (
            <button type="button" className="host-action__primary" onClick={() => onReview(proposal)}>
              View model
            </button>
          ) : null}
          {directAction && running && typeof onStop === "function" ? (
            <button
              type="button"
              className="host-action__dismiss"
              disabled={busy}
              onClick={() => onStop(operation)}
            >
              <Square aria-hidden="true" /> Stop
            </button>
          ) : null}
          {directAction && !blocked && !running && !completed && (reviewable || failed) ? (
            <button
              type="button"
              className="host-action__primary"
              disabled={busy}
              onClick={openActionConfirmation}
            >
              {failed ? <RotateCcw aria-hidden="true" /> : null}
              {failed
                ? "Retry"
                : image ? "Create image" : fileTrash ? "Move to Recycle Bin" : "Review cleanup"}
            </button>
          ) : null}
          {!directAction && reviewable && typeof onReview === "function" ? (
            <button type="button" className="host-action__primary" onClick={() => onReview(proposal)}>
              Review command
            </button>
          ) : null}
          {!running && !completed && typeof onDismiss === "function" ? (
            <button type="button" className="host-action__dismiss" onClick={() => onDismiss(proposal)}>
              Dismiss
            </button>
          ) : null}
        </div>
      </section>

      {directAction ? (
        <Dialog
          open={confirmationOpen}
          title={fileTrash
            ? "Move this exact file to Recycle Bin?"
            : tempCleanup
              ? "Clean Windows temporary files?"
              : failed ? "Try this image again?" : "Create this image?"}
          description={fileTrash
            ? "Salty Steak will recheck the exact file before using the Windows Recycle Bin."
            : tempCleanup
              ? "Only the reviewed temporary-folder contents are cleaned. In-use and inaccessible entries are skipped."
            : "Steak Gen will run locally and temporarily take over the GPU."}
          onClose={() => {
            if (!busy) {
              setConfirmationOpen(false);
              setCleanupAcknowledged(false);
            }
          }}
        >
          <form
            className="image-confirmation"
            onSubmit={tempCleanup ? (event) => event.preventDefault() : confirmAction}
          >
            <div className="image-confirmation__prompt">
              <span>{fileTrash ? "Exact file" : tempCleanup ? "Scope" : "Prompt"}</span>
              <p>{tempCleanup
                ? (proposal.arguments?.roots || []).map((root) => `${root.name}: ${root.path}`).join("\n")
                : proposal.summary}</p>
            </div>
            {fileTrash ? (
              <dl className="image-confirmation__profile">
                <div><dt>Action</dt><dd>Move one file</dd></div>
                <div><dt>Recovery</dt><dd>Windows Recycle Bin</dd></div>
                <div><dt>Shell command</dt><dd>Not used</dd></div>
              </dl>
            ) : tempCleanup ? (
              <dl className="image-confirmation__profile">
                <div><dt>Root folders</dt><dd>Preserved</dd></div>
                <div><dt>Files in use</dt><dd>Skipped</dd></div>
                <div><dt>Recovery</dt><dd>Not available</dd></div>
              </dl>
            ) : (
              <dl className="image-confirmation__profile">
                <div><dt>Canvas</dt><dd>512 × 512</dd></div>
                <div><dt>Quality pass</dt><dd>8 steps</dd></div>
                <div><dt>Model</dt><dd>Steak Gen 1 ScaledFP8</dd></div>
              </dl>
            )}
            {tempCleanup ? (
              <label className="image-confirmation__acknowledgment">
                <input
                  type="checkbox"
                  checked={cleanupAcknowledged}
                  disabled={busy}
                  onChange={(event) => setCleanupAcknowledged(event.target.checked)}
                />
                <span>I understand these temporary files are deleted permanently.</span>
              </label>
            ) : null}
            {!fileTrash && !tempCleanup ? <label className="image-confirmation__seed">
              <span>Seed</span>
              <input
                type="number"
                min="0"
                max="9007199254740991"
                step="1"
                required
                value={seed}
                disabled={busy}
                onChange={(event) => setSeed(event.target.value)}
              />
              <small>Use the same seed to reproduce the same starting noise.</small>
              <button type="button" disabled={busy} onClick={() => setSeed(createFreshImageSeed())}>
                New seed
              </button>
            </label> : null}
            <div className="image-confirmation__actions">
              <button type="button" disabled={busy} onClick={() => setConfirmationOpen(false)}>
                Cancel
              </button>
              <button
                type={tempCleanup ? "button" : "submit"}
                className="host-action__primary"
                disabled={busy || (tempCleanup && !cleanupAcknowledged)}
                onClick={tempCleanup ? confirmAction : undefined}
              >
                {busy
                  ? "Starting…"
                  : fileTrash ? "Move file" : tempCleanup ? "Clean temporary files" : "Create image"}
              </button>
            </div>
          </form>
        </Dialog>
      ) : null}
    </>
  );
}

function hostActionOperationLabel(proposal, operation) {
  const state = operation ? operationState(operation) : String(proposal?.state || "");
  if (isActive(operation)) return phaseLabel(operation);
  if (state === "completed") {
    if (proposal?.kind === "filesystem.trash_file") return "Moved to Recycle Bin";
    if (proposal?.kind === "system.clean_temp") return "Cleanup complete";
    if (proposal?.kind === "image.generate") return "Image ready";
    return "Completed";
  }
  if (["queued", "running"].includes(state)) return phaseLabel(operation);
  if (state === "stop_requested") return "Stopping";
  if (state === "failed") return actionTerminalLabel(proposal?.kind, "failed");
  if (["interrupted", "cancelled"].includes(state)) return actionTerminalLabel(proposal?.kind, "stopped");
  if (state === "pending_review") return "Confirmation required";
  if (state === "needs_clarification") return "More detail needed";
  if (state === "blocked_platform_mismatch") return "Windows plan required";
  if (state === "blocked_target_unavailable") return "File unavailable";
  return "Runtime not ready";
}

function actionTerminalLabel(kind, state) {
  if (kind === "image.generate") return `Image generation ${state}`;
  if (kind === "filesystem.trash_file") return `Move ${state}`;
  if (kind === "system.clean_temp") return `Cleanup ${state}`;
  return state === "failed" ? "Action failed" : "Action stopped";
}
