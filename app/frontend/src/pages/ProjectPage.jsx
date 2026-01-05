import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  Database,
  FolderOpen,
  HardDrive,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { api } from "../api/client.js";
import { Dialog } from "../components/Dialog.jsx";
import { FormActions } from "../components/Forms.jsx";
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
import { formatBytes, formatDate, formatNumber } from "../workflows/formatters.js";
import { isActive, operationMatches } from "../workflows/operations.mjs";

const PATHS = [
  { key: "model_architecture", label: "Model architecture" },
  { key: "tokenizer", label: "Tokenizer" },
  { key: "datasets", label: "Datasets" },
  { key: "latest_saved_version", label: "Latest saved version" },
  { key: "previous_saved_version", label: "Previous saved version" },
  { key: "training_recovery_state", label: "Training recovery state" },
  { key: "active_chat_version", label: "Active chat version" },
  { key: "runtime_artifact", label: "Runtime artifact" },
  { key: "database", label: "Application database" },
  { key: "cache", label: "Cache" },
  { key: "logs", label: "Logs" },
];

export function ProjectPage() {
  const {
    project,
    operations,
    startOperation,
    reportError,
    refreshDomain,
  } = useAppState();
  const [busy, setBusy] = useState(null);
  const [clearOpen, setClearOpen] = useState(false);

  useEffect(() => {
    void refreshDomain("project", { quiet: true }).catch((error) =>
      reportError(error, "open-project"),
    );
  }, [refreshDomain, reportError]);

  const verificationOperation = useMemo(
    () => latestOperation(operations, "project_verification"),
    [operations],
  );
  const cacheOperation = useMemo(
    () => latestOperation(operations, "cache_clear"),
    [operations],
  );
  const working =
    (verificationOperation && isActive(verificationOperation)) ||
    (cacheOperation && isActive(cacheOperation));

  async function verifyFiles() {
    setBusy("verify");
    try {
      await startOperation({
        type: "project_verification",
        targetId: "project",
        launch: (requestKey) => api.verifyProject(requestKey),
      });
    } catch (error) {
      reportError(error, "project-verification");
    } finally {
      setBusy(null);
    }
  }

  async function clearCache() {
    setBusy("cache");
    try {
      await startOperation({
        type: "cache_clear",
        targetId: "cache",
        launch: (requestKey) => api.clearCache(requestKey),
      });
      setClearOpen(false);
    } catch (error) {
      reportError(error, "clear-cache");
    } finally {
      setBusy(null);
    }
  }

  async function openPath(path, key) {
    if (!path) return;
    setBusy(`path:${key}`);
    try {
      await api.openPath(path);
    } catch (error) {
      reportError(error, `path:${key}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page page--project">
      <PageHeader
        title="Project"
        actions={
          <Button
            icon={RefreshCw}
            onClick={() => refreshDomain("project").catch((error) => reportError(error, "refresh-project"))}
          >
            Refresh
          </Button>
        }
      />

      {!project ? (
        <EmptyState
          icon={HardDrive}
          title="Project details are unavailable"
          description="The local service has not returned the current project paths yet."
        />
      ) : (
        <>
          <section className="project-overview">
            <div className="project-state">
              <ShieldCheck aria-hidden="true" />
              <div>
                <span className="eyebrow">Local and offline</span>
                <h2>{project.product_name || "Salty Steak"}</h2>
                <p>
                  {formatNumber(project.model?.parameter_count)} parameters,{" "}
                  {formatNumber(project.model?.vocab_size)} tokenizer vocabulary
                </p>
              </div>
              <Status
                value={project.integrity_status || "not_verified"}
                label={project.integrity_label || "Verification not reported"}
              />
            </div>
            <DefinitionList
              items={[
                {
                  label: "Architecture revision",
                  value: project.model?.architecture_revision,
                },
                {
                  label: "Architectural context",
                  value: project.model?.architectural_context_tokens
                    ? `${formatNumber(project.model.architectural_context_tokens)} tokens`
                    : "Not reported",
                },
                {
                  label: "Training sequence length",
                  value: project.training?.sequence_length
                    ? `${formatNumber(project.training.sequence_length)} tokens`
                    : "Not reported",
                },
                { label: "Device", value: String(project.training?.device || "Not reported").toUpperCase() },
                { label: "Precision", value: String(project.training?.precision || "Not reported").toUpperCase() },
                { label: "Last verified", value: formatDate(project.last_verified_at) },
              ]}
            />
          </section>

          <section className="path-section">
            <div className="section-heading">
              <div>
                <span className="eyebrow">Real storage locations</span>
                <h2>Project paths</h2>
              </div>
            </div>
            <div className="path-list">
              {PATHS.map((item) => {
                const value = project.paths?.[item.key];
                return (
                  <div className="path-row" key={item.key}>
                    <div>
                      <strong>{item.label}</strong>
                      <code>{value || "Not present"}</code>
                    </div>
                    <Button
                      icon={FolderOpen}
                      busy={busy === `path:${item.key}`}
                      disabled={!value}
                      onClick={() => openPath(value, item.key)}
                    >
                      Open folder
                    </Button>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="project-actions">
            <div className="section-heading">
              <div>
                <span className="eyebrow">Safe maintenance</span>
                <h2>Project actions</h2>
              </div>
            </div>
            <div className="project-action-row">
              <div>
                <CheckCircle2 aria-hidden="true" />
                <span>
                  <strong>Verify files</strong>
                  <small>Check required files and registered checksums in the background.</small>
                </span>
              </div>
              <Button
                icon={ShieldCheck}
                busy={busy === "verify"}
                disabled={working}
                onClick={verifyFiles}
              >
                Verify files
              </Button>
            </div>
            <div className="project-action-row">
              <div>
                <Database aria-hidden="true" />
                <span>
                  <strong>Clear disposable cache</strong>
                  <small>
                    Remove regenerable cache only. Datasets, tokenizer, saved versions, and recovery
                    state stay untouched.
                  </small>
                </span>
              </div>
              <Button
                variant="danger-quiet"
                icon={Trash2}
                disabled={working}
                onClick={() => setClearOpen(true)}
              >
                Clear disposable cache
              </Button>
            </div>
          </section>

          {verificationOperation ? (
            <OperationProgress
              operation={verificationOperation}
              title="File verification"
              metrics={(operation) => [
                {
                  label: "Files checked",
                  value: formatNumber(operation.current_progress),
                },
                {
                  label: "Total files",
                  value: formatNumber(operation.total_progress),
                },
                {
                  label: "Issues",
                  value: formatNumber(operation.result?.issue_count),
                },
              ]}
            />
          ) : null}
          {cacheOperation ? (
            <OperationProgress
              operation={cacheOperation}
              title="Cache clearing"
              metrics={(operation) => [
                {
                  label: "Storage reclaimed",
                  value: formatBytes(operation.result?.storage_reclaimed_bytes),
                },
                {
                  label: "Files removed",
                  value: formatNumber(operation.result?.files_removed_count),
                },
              ]}
            />
          ) : null}
        </>
      )}

      <Dialog
        open={clearOpen}
        title="Clear disposable cache?"
        description="Only files the rebuilt application can safely regenerate will be removed."
        onClose={() => setClearOpen(false)}
      >
        <InlineNotice kind="warning" title="This action cannot be undone">
          Training material, prepared datasets, tokenizer files, saved versions, evaluations,
          conversations, runtime identity, and recovery state are excluded.
        </InlineNotice>
        <DefinitionList
          items={[
            {
              label: "Cache location",
              value: project?.paths?.cache || "Not reported",
              mono: true,
            },
            {
              label: "Current disposable size",
              value: formatBytes(project?.cache?.disposable_size_bytes),
            },
          ]}
        />
        <FormActions>
          <Button onClick={() => setClearOpen(false)}>Cancel</Button>
          <Button
            variant="danger"
            icon={Trash2}
            busy={busy === "cache"}
            onClick={clearCache}
          >
            Clear cache
          </Button>
        </FormActions>
      </Dialog>
    </div>
  );
}

function latestOperation(operations, type) {
  return Object.values(operations)
    .filter((operation) => operationMatches(operation, type))
    .sort((a, b) => Date.parse(b.updated_at || 0) - Date.parse(a.updated_at || 0))[0];
}
