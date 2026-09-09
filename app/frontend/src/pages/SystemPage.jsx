import { useEffect, useMemo, useState } from "react";
import {
  AppWindow,
  Check,
  Copy,
  Cpu,
  Database,
  FolderOpen,
  Gauge,
  HardDrive,
  PackageCheck,
  RefreshCw,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { api, asList } from "../api/client.js";
import {
  Button,
  Disclosure,
  InlineNotice,
  PageHeader,
  Status,
} from "../components/Primitives.jsx";
import { useAppState } from "../state/AppState.jsx";
import { errorMessage } from "../workflows/formatters.js";
import { isActive, normaliseToken } from "../workflows/operations.mjs";
import "../styles/system-page-r12.css";

export function SystemPage() {
  const {
    operations,
    project,
    refreshDomain,
    startOperation,
  } = useAppState();
  const [about, setAbout] = useState(null);
  const [storage, setStorage] = useState(null);
  const [currentVersion, setCurrentVersion] = useState(null);
  const [selectedModelName, setSelectedModelName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [copied, setCopied] = useState(false);
  const [loadRevision, setLoadRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError("");
    Promise.allSettled([api.getAbout(), api.listVersions(), api.getChatStatus()])
      .then(([aboutResult, versionResult, chatResult]) => {
        if (cancelled) return;
        const aboutPayload = aboutResult.status === "fulfilled" ? aboutResult.value : null;
        const chatStatus = chatResult.status === "fulfilled" ? chatResult.value : null;
        if (aboutPayload) setAbout(aboutPayload);
        const failures = [aboutResult, versionResult, chatResult].filter((result) => result.status === "rejected");
        setLoadError(failures.map((result) => errorMessage(result.reason)).filter((value, index, values) => values.indexOf(value) === index).join(" "));
        setLoading(false);
        if (chatStatus) setSelectedModelName(String(chatStatus.active_version_label || ""));
        const activeId =
          chatStatus?.active_saved_version_id ||
          chatStatus?.saved_version_id ||
          aboutPayload?.model?.saved_version_id;
        if (versionResult.status === "fulfilled" && (chatStatus || aboutPayload)) setCurrentVersion(
          asList(versionResult.value, ["versions"]).find(
            (version) => String(version.id) === String(activeId),
          ) || null,
        );
      })
      .catch((value) => {
        if (!cancelled) setError(errorMessage(value));
      });
    void refreshDomain("project", { quiet: true }).catch((value) => {
      if (!cancelled) setError(errorMessage(value));
    });
    return () => {
      cancelled = true;
    };
  }, [refreshDomain, loadRevision]);

  const application = about?.application || {};
  const packageInfo = about?.package || {};
  const runtime = about?.runtime || {};
  const system = about?.system || {};
  const model = about?.model || {};
  const paths = about?.paths || {};
  const manifestPresent = Boolean(packageInfo.manifest_sha256);
  const runtimeState = runtimePresentation(runtime, model);
  const missingState = loading ? "Loading" : "Unavailable";
  const freeSpace = formatBytes(storage?.free_disk_bytes ?? system.drive_free_bytes);
  const verificationOperation = useMemo(
    () =>
      Object.values(operations || {})
        .filter(
          (operation) => normaliseToken(operation?.type) === "project_verification",
        )
        .sort(
          (left, right) =>
            Date.parse(right.updated_at || right.created_at || 0) -
            Date.parse(left.updated_at || left.created_at || 0),
        )[0] || null,
    [operations],
  );
  const verification = verificationPresentation(project, verificationOperation);
  const diagnostics = useMemo(
    () => JSON.stringify({ ...about, project, storage }, null, 2),
    [about, project, storage],
  );

  async function refreshStorage() {
    setBusy("storage");
    setError("");
    try {
      setStorage(await api.getAboutStorage(true));
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy("");
    }
  }

  async function verify() {
    setBusy("verify");
    setError("");
    try {
      await startOperation({
        type: "project_verification",
        targetId: "project",
        launch: (requestKey) => api.verifyProject(requestKey),
      });
    } catch (value) {
      setError(errorMessage(value));
    } finally {
      setBusy("");
    }
  }

  async function copyDiagnostics() {
    try {
      await navigator.clipboard.writeText(diagnostics);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch (value) {
      setError(errorMessage(value));
    }
  }

  return (
    <div className="page page--system-r18">
      <PageHeader title="System">
        <p>Runtime, hardware, storage, and privacy.</p>
      </PageHeader>

      {error ? <InlineNotice kind="error" title="Some system details are unavailable">{error}</InlineNotice> : null}
      {loadError ? <InlineNotice kind="error" title="System information unavailable"
        actions={<Button disabled={loading} onClick={() => setLoadRevision((value) => value + 1)}>Retry</Button>}
      >{loadError}</InlineNotice> : null}

      <section className="system-r18__summary">
        <div>
          <p>{loading ? "Connecting to the local service" : loadError ? "Some system information is unavailable" : "Connected to the local service"}</p>
        </div>
        <Button icon={ShieldCheck} busy={busy === "verify" || Boolean(verificationOperation && isActive(verificationOperation))} onClick={verify}>
          {verification.label === "Passed" ? "Verify again" : "Verify installation"}
        </Button>
      </section>

      <section className="system-r18__list" aria-label="System overview">
        <SystemOverviewRow
          icon={AppWindow}
          title="Application"
          description="Salty Steak"
          value={application.app_version ? `Version ${application.app_version}` : missingState}
        />
        <SystemOverviewRow
          icon={Database}
          title="Model runtime"
          description={
            currentVersion?.friendly_name ||
            selectedModelName ||
            model.display_name ||
            (about ? "No local model selected" : missingState)
          }
          value={!about ? missingState : runtimeState.loaded ? `${runtime.device || "Loaded"}${runtime.precision ? ` · ${runtime.precision}` : ""}` : "Idle"}
        />
        <SystemOverviewRow
          icon={Cpu}
          title="Hardware"
          description={about ? cpuSummary(system.cpu_model, system.logical_processor_count) : missingState}
          value={formatBytes(system.available_ram_bytes) ? `${formatBytes(system.available_ram_bytes)} available` : missingState}
        />
        <SystemOverviewRow
          icon={HardDrive}
          title="Storage"
          description={`${system.current_drive || "Local drive"} workspace`}
          value={freeSpace ? `${freeSpace} free` : missingState}
          action={<button type="button" disabled={busy === "storage"} onClick={refreshStorage}>{busy === "storage" ? "Measuring" : "Measure"}</button>}
        />
        <SystemOverviewRow
          icon={ShieldCheck}
          title="Privacy"
          description="Prompts, files, and model data stay on this computer by default."
          value="Local"
        />
      </section>

      <Disclosure summary="Technical details" className="system-r18__technical">
        <div className="system-command-bar">
          <Button icon={FolderOpen} disabled={!paths.logs} onClick={() => api.openPath(paths.logs)}>Open logs</Button>
          <Button icon={FolderOpen} disabled={!paths.workspace} onClick={() => api.openPath(paths.workspace)}>Open workspace</Button>
          <Button icon={copied ? Check : Copy} disabled={!about} onClick={copyDiagnostics}>{copied ? "Copied" : "Copy diagnostics"}</Button>
        </div>
        <pre className="technical-json">{diagnostics}</pre>
      </Disclosure>
    </div>
  );


  return (
    <div className="page page--system-r12">
      <PageHeader title="System">
        <p>Health, identity, hardware, storage, and local diagnostics.</p>
      </PageHeader>

      {error ? (
        <InlineNotice kind="error" title="Some system details are unavailable">
          {error}
        </InlineNotice>
      ) : null}

      <section className="system-health-strip" aria-labelledby="system-health-title">
        <div className="system-health-strip__heading">
          <h2 id="system-health-title">System status</h2>
          <p>Current local session</p>
        </div>
        <ul className="system-health-list">
          <HealthItem
            label="Service"
            value={about ? "Available" : "Loading"}
            status={about ? "ready" : "preparing"}
            detail="The desktop interface is connected to its local backend."
          />
          <HealthItem
            label="Manifest"
            value={manifestPresent ? "Present" : "Not present"}
            status={manifestPresent ? "information" : "warning"}
            detail={
              manifestPresent
                ? "Presence only — this is not an integrity verification result."
                : "Development source may not include a packaged integrity manifest."
            }
          />
          <HealthItem
            label="Verification"
            value={verification.label}
            status={verification.status}
            detail={verification.detail}
          />
          <HealthItem
            label="Runtime"
            value={runtimeState.label}
            status={runtimeState.status}
            detail={runtimeState.detail}
          />
          <HealthItem
            label={system.current_drive || "Drive"}
            value={formatBytes(storage?.free_disk_bytes ?? system.drive_free_bytes) || "Not reported"}
            status="information"
            detail={`${system.current_drive || "Local drive"} free space`}
          />
        </ul>
      </section>

      <div className="system-settings">
        <SettingsGroup
          title="Application and package"
          description="Installed build identity and the evidence available to verify it."
        >
          <SettingRow
            icon={AppWindow}
            title="Application build"
            description="The frontend, backend, and native host shipped together."
          >
            <PropertyGrid
              items={[
                { label: "Product", value: application.product_name },
                { label: "App version", value: application.app_version },
                { label: "Build type", value: application.build_type },
                { label: "Build date", value: formatDate(application.build_date) },
                { label: "Frontend", value: application.frontend_version },
                { label: "Backend", value: application.backend_version },
              ]}
            />
          </SettingRow>

          <SettingRow
            icon={PackageCheck}
            title="Package identity"
            description="Manifest availability and source isolation are facts, not a verification pass."
          >
            <PropertyGrid
              items={[
                {
                  label: "Manifest",
                  value: manifestPresent ? "Present — not verified by presence alone" : "Not present",
                },
                { label: "Source isolation", value: packageInfo.repository_source_isolation },
                { label: "Executable size", value: formatBytes(packageInfo.executable_size_bytes) },
              ]}
            />
          </SettingRow>

          <SettingRow
            icon={ShieldCheck}
            title="Installation verification"
            description="Checks required files, saved checkpoints, dataset sources, runtime identity, and database references."
            actions={
              <Button
                icon={ShieldCheck}
                busy={busy === "verify" || Boolean(verificationOperation && isActive(verificationOperation))}
                onClick={verify}
              >
                Verify installation
              </Button>
            }
          >
            <PropertyGrid
              items={[
                { label: "Status", value: <Status value={verification.status} label={verification.label} size="small" /> },
                { label: "Last checked", value: formatDate(project?.last_verified_at) || "Not run yet" },
                {
                  label: "Issues",
                  value:
                    project?.verification_issue_count === null ||
                    project?.verification_issue_count === undefined
                      ? "Not measured"
                      : formatCount(project.verification_issue_count),
                },
                { label: "Current phase", value: verificationOperation?.phase },
                { label: "Operation", value: verificationOperation?.id, mono: true, wide: true },
              ]}
            />
          </SettingRow>
        </SettingsGroup>

        <SettingsGroup
          title="Model and runtime"
          description="The checkpoint selected for Chat and what is actually loaded in this session."
        >
          <SettingRow
            icon={Database}
            title="Selected checkpoint"
            description={
              currentVersion?.production_policy?.rationale ||
              "The saved version currently selected for Chat."
            }
          >
            <PropertyGrid
              items={[
                { label: "Name", value: currentVersion?.friendly_name || model.display_name },
                {
                  label: "Classification",
                  value: readableToken(currentVersion?.production_policy?.classification),
                },
                { label: "Checkpoint ID", value: model.saved_version_id, mono: true, wide: true },
                { label: "Lineage", value: model.lineage_id, mono: true },
                { label: "Parent version", value: model.parent_version_id, mono: true },
                { label: "Role", value: readableToken(model.baseline_role) },
                { label: "Protection", value: model.protection_state },
                { label: "Added steps", value: formatCount(model.additional_steps) },
                {
                  label: "Cumulative steps",
                  value:
                    model.cumulative_training_steps === null ||
                    model.cumulative_training_steps === undefined
                      ? "Unknown at import"
                      : formatCount(model.cumulative_training_steps),
                },
              ]}
            />
          </SettingRow>

          <SettingRow
            icon={Gauge}
            title="Runtime session"
            description={runtimeState.detail}
          >
            <PropertyGrid
              items={[
                { label: "State", value: <Status value={runtimeState.status} label={runtimeState.label} size="small" /> },
                { label: "Engine", value: "Direct PyTorch" },
                { label: "Device", value: runtimeState.loaded ? runtime.device : "Not loaded" },
                { label: "Precision", value: runtimeState.loaded ? runtime.precision : "Not loaded" },
                { label: "Last verified activation", value: formatDate(runtime.last_runtime_load_time) },
                { label: "Active operation", value: runtime.active_operation?.phase || runtime.active_operation?.state },
              ]}
            />
          </SettingRow>

          <SettingRow
            icon={Database}
            title="Architecture and files"
            description="Stored checkpoint characteristics; context capability is not a quality claim."
          >
            <PropertyGrid
              items={[
                { label: "Architecture", value: model.architecture },
                { label: "Parameters", value: formatCount(model.parameter_count) },
                { label: "Layers", value: formatCount(model.layer_count) },
                { label: "Hidden size", value: formatCount(model.hidden_size) },
                { label: "Attention heads", value: formatCount(model.attention_head_count) },
                { label: "Vocabulary", value: formatCount(model.vocabulary_size) },
                { label: "Context capability", value: tokenValue(model.context_limit) },
                { label: "Weight format", value: model.weight_format },
                { label: "Weight files", value: formatCount(model.weight_file_count) },
                { label: "Weight bytes", value: formatBytes(model.weight_file_total_bytes) },
                { label: "Saved version size", value: formatBytes(model.saved_version_directory_size_bytes) },
                { label: "Checkpoint SHA-256", value: model.checkpoint_sha256, mono: true, wide: true },
              ]}
            />
          </SettingRow>
        </SettingsGroup>

        <SettingsGroup
          title="Hardware"
          description="Lightweight system facts. Accelerator details appear only when the current runtime reports them."
        >
          <SettingRow
            icon={Cpu}
            title={cpuSummary(system.cpu_model, system.logical_processor_count)}
            description="Processor details reported by Windows. The raw signature remains in Technical details."
          >
            <PropertyGrid
              items={[
                { label: "Logical processors", value: formatCount(system.logical_processor_count) },
                { label: "Installed memory", value: formatBytes(system.installed_ram_bytes) },
                { label: "Available memory", value: formatBytes(system.available_ram_bytes) },
                { label: "Windows", value: system.windows_version, wide: true },
              ]}
            />
          </SettingRow>

          <SettingRow
            icon={Gauge}
            title="Accelerator"
            description={acceleratorDescription(runtime)}
          >
            <PropertyGrid
              items={[
                {
                  label: "Current CUDA state",
                  value:
                    runtime.cuda_available === true
                      ? "Reported by loaded runtime"
                      : "Not probed in this session",
                },
                { label: "Runtime device", value: runtime.device },
                { label: "Runtime precision", value: runtime.precision },
                { label: "Reported VRAM", value: formatBytes(system.gpu_memory_bytes) },
              ]}
            />
          </SettingRow>
        </SettingsGroup>

        <SettingsGroup
          title="Storage"
          description="Detailed directory sizes are measured only when requested and cached for five minutes."
        >
          <SettingRow
            icon={HardDrive}
            title="Local workspace"
            description={
              storage
                ? `Measured ${formatDate(storage.measured_at) || "locally"}.`
                : "No recursive storage measurement has run in this session."
            }
            actions={
              <Button icon={RefreshCw} busy={busy === "storage"} onClick={refreshStorage}>
                Measure storage
              </Button>
            }
          >
            <PropertyGrid
              items={[
                { label: "Current drive", value: system.current_drive },
                { label: "Free space", value: formatBytes(storage?.free_disk_bytes ?? system.drive_free_bytes) },
                { label: "Workspace", value: formatBytes(storage?.workspace_total_bytes) },
                { label: "Workspace files", value: formatCount(storage?.workspace_file_count) },
                { label: "Dataset sources", value: formatBytes(storage?.dataset_bytes) },
                { label: "Prepared data", value: formatBytes(storage?.prepared_data_bytes) },
                { label: "Saved versions", value: formatBytes(storage?.saved_version_bytes) },
                { label: "Checkpoints", value: formatBytes(storage?.checkpoint_bytes) },
                { label: "Evaluation reports", value: formatBytes(storage?.evaluation_data_bytes) },
                { label: "Database", value: formatBytes(storage?.database_bytes) },
                { label: "Conversation text", value: formatBytes(storage?.conversation_payload_bytes) },
                { label: "Tokenizer + configuration", value: formatBytes(storage?.tokenizer_and_configuration_bytes) },
                { label: "Logs", value: formatBytes(storage?.logs_bytes) },
                { label: "Disposable cache", value: formatBytes(storage?.cache_bytes) },
                { label: "Current package", value: formatBytes(storage?.current_package_bytes) },
                { label: "Current package files", value: formatCount(storage?.current_package_file_count) },
                { label: "Configured rollback package", value: formatBytes(storage?.rollback_package_bytes) },
                { label: "Rollback package files", value: formatCount(storage?.rollback_package_file_count) },
              ]}
            />
          </SettingRow>
        </SettingsGroup>

        <SettingsGroup
          title="Diagnostics"
          description="Open local evidence or copy the exact state shown by the backend."
        >
          <SettingRow
            icon={Wrench}
            title="Local tools"
            description="These actions open project-owned folders or copy diagnostics; they do not change model state."
          >
            <div className="system-command-bar">
              <Button icon={FolderOpen} disabled={!paths.logs} onClick={() => api.openPath(paths.logs)}>
                Open logs
              </Button>
              <Button icon={FolderOpen} disabled={!paths.workspace} onClick={() => api.openPath(paths.workspace)}>
                Open workspace
              </Button>
              <Button icon={FolderOpen} disabled={!paths.package} onClick={() => api.openPath(paths.package)}>
                Open package
              </Button>
              <Button icon={copied ? Check : Copy} disabled={!about} onClick={copyDiagnostics}>
                {copied ? "Copied" : "Copy diagnostics"}
              </Button>
            </div>
            <Disclosure summary="Technical details" className="system-technical-details">
              <pre className="technical-json">{diagnostics}</pre>
            </Disclosure>
          </SettingRow>
        </SettingsGroup>
      </div>

      <p className="system-privacy">
        Local by design. Data, conversations, and model artifacts remain in this
        workspace unless you explicitly export them.
      </p>
    </div>
  );
}

function SystemOverviewRow({ icon: Icon, title, description, value, action }) {
  return (
    <article className="system-r18__row">
      <span className="system-r18__row-icon"><Icon aria-hidden="true" /></span>
      <div><h3>{title}</h3><p>{description}</p></div>
      <div className="system-r18__row-value"><strong>{value}</strong>{action}</div>
    </article>
  );
}

function HealthItem({ label, value, status, detail }) {
  return (
    <li className="system-health-item" title={detail}>
      <span className="system-health-item__label">{label}</span>
      <Status value={status} label={value} size="small" detail={detail} />
    </li>
  );
}

function SettingsGroup({ title, description, children }) {
  return (
    <section className="system-settings-group">
      <header>
        <h2>{title}</h2>
        <p>{description}</p>
      </header>
      <div>{children}</div>
    </section>
  );
}

function SettingRow({ icon: Icon, title, description, actions, children }) {
  return (
    <article className="system-setting-row">
      <div className="system-setting-row__label">
        <span className="system-setting-row__icon"><Icon aria-hidden="true" /></span>
        <div>
          <h3>{title}</h3>
          <p>{description}</p>
        </div>
      </div>
      <div className="system-setting-row__content">
        {children}
        {actions ? <div className="system-setting-row__actions">{actions}</div> : null}
      </div>
    </article>
  );
}

function PropertyGrid({ items }) {
  const visible = items.filter(
    (item) => item?.value !== null && item?.value !== undefined && item?.value !== "",
  );
  if (!visible.length) return <p className="system-unavailable">Not reported.</p>;
  return (
    <dl className="system-property-grid">
      {visible.map((item) => (
        <div
          key={item.label}
          className={item.wide ? "system-property system-property--wide" : "system-property"}
        >
          <dt>{item.label}</dt>
          <dd className={item.mono ? "mono" : undefined} title={item.mono ? String(item.value) : undefined}>
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function runtimePresentation(runtime, model) {
  const token = normaliseToken(runtime?.runtime_state);
  const loaded =
    token === "ready" ||
    String(model?.loaded_runtime_state || "").toLowerCase() === "loaded";
  if (loaded) {
    return {
      loaded: true,
      status: "ready",
      label: "Loaded for Chat",
      detail: runtime.device
        ? `The selected checkpoint is loaded on ${runtime.device}.`
        : "The selected checkpoint is loaded for Chat.",
    };
  }
  if (model?.saved_version_id) {
    return {
      loaded: false,
      status: "information",
      label: "Selected, not loaded",
      detail: "The checkpoint is selected for Chat but is not loaded in this desktop session.",
    };
  }
  return {
    loaded: false,
    status: "warning",
    label: "No checkpoint selected",
    detail: "Choose an eligible saved version before using Chat.",
  };
}

function verificationPresentation(project, operation) {
  if (operation && isActive(operation)) {
    const progress =
      Number.isFinite(Number(operation.current_progress)) &&
      Number.isFinite(Number(operation.total_progress)) &&
      Number(operation.total_progress) > 0
        ? ` ${formatCount(operation.current_progress)} of ${formatCount(operation.total_progress)}`
        : "";
    return {
      status: "preparing",
      label: `Checking${progress}`,
      detail: operation.phase || "The local verification operation is running.",
    };
  }
  if (project?.integrity_status === "ready") {
    return {
      status: "ready",
      label: "Passed",
      detail: project.last_verified_at
        ? `Last completed ${formatDate(project.last_verified_at)}.`
        : "The latest stored verification reported no issues.",
    };
  }
  if (project?.integrity_status === "needs_attention") {
    return {
      status: "warning",
      label: `${formatCount(project.verification_issue_count) || "Some"} issues`,
      detail: "The latest stored verification reported issues that need review.",
    };
  }
  return {
    status: "information",
    label: "Not run yet",
    detail: "Manifest presence does not replace an explicit installation verification.",
  };
}

function cpuSummary(value, logicalProcessors) {
  const raw = String(value || "").trim();
  let label = raw;
  if (/genuineintel|intel64/i.test(raw)) label = "Intel processor";
  else if (/authenticamd|amd64/i.test(raw)) label = "AMD processor";
  else if (!raw) label = "Processor";
  const logical = formatCount(logicalProcessors);
  return logical ? `${label} · ${logical} logical processors` : label;
}

function acceleratorDescription(runtime) {
  if (runtime?.cuda_available === true) {
    return runtime.device
      ? `The loaded runtime reports CUDA on ${runtime.device}.`
      : "The loaded runtime reports CUDA availability.";
  }
  if (runtime?.device) {
    return `The loaded runtime reports ${runtime.device}; CUDA is not active.`;
  }
  return "GPU, CUDA, and VRAM are not probed while the model runtime is unloaded.";
}

function readableToken(value) {
  if (!value) return null;
  return String(value)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatCount(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat().format(number) : null;
}

function tokenValue(value) {
  const count = formatCount(value);
  return count ? `${count} tokens` : null;
}

function formatBytes(value) {
  if (value === null || value === undefined || value === "") return null;
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return null;
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = bytes
    ? Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
    : 0;
  const amount = bytes / (1024 ** index);
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function formatDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
}
