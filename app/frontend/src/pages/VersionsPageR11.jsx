import { useEffect, useMemo, useState } from "react";
import {
  BarChart3,
  FolderOpen,
  GraduationCap,
  Info,
  MessageCircle,
  Search,
} from "lucide-react";
import { api } from "../api/client.js";
import {
  Button,
  DefinitionList,
  Disclosure,
  EmptyState,
  PageHeader,
  Status,
} from "../components/Primitives.jsx";
import { useAppState } from "../state/AppState.jsx";
import { formatBytes, formatDate, formatNumber } from "../workflows/formatters.js";
import { sameSelectionId, selectedItemOrFirst } from "../workflows/selection.mjs";

export function VersionsPageR11({ onNavigate }) {
  const {
    versions,
    chatStatus,
    refreshDomain,
    reportError,
    startOperation,
  } = useAppState();
  const [selectedId, setSelectedId] = useState("");
  const [query, setQuery] = useState("");
  const [technical, setTechnical] = useState(null);
  const [busy, setBusy] = useState("");
  const [roleCatalog, setRoleCatalog] = useState(null);
  const [selectedRoleId, setSelectedRoleId] = useState(() => {
    const requested = new URLSearchParams(String(window.location.hash).split("?")[1] || "").get("role");
    return requested || "text_generation";
  });

  useEffect(() => {
    Promise.all([
      refreshDomain("versions", { quiet: true }),
      refreshDomain("chat", { quiet: true }),
      api.getModelRoles().then(setRoleCatalog),
    ]).catch((error) => reportError(error, "open-versions-r11"));
  }, [refreshDomain, reportError]);

  const roles = roleCatalog?.roles || [{
    id: "text_generation",
    label: "Language",
    description: "Conversation, writing, coding, and reasoning.",
    registered_models: versions.length,
  }];
  const selectedRole = roles.find((role) => role.id === selectedRoleId) || roles[0];
  const effectiveRoleId = selectedRole?.id || selectedRoleId;
  const roleVersions = useMemo(() => {
    const checkpoints = versions.filter(
      (version) => (version.model_role || "text_generation") === effectiveRoleId,
    );
    const bundles = Array.isArray(selectedRole?.models) ? selectedRole.models : [];
    return [...bundles, ...checkpoints];
  }, [effectiveRoleId, selectedRole, versions]);

  useEffect(() => {
    if (!selectedId && roleVersions[0]) setSelectedId(roleVersions[0].id);
    if (selectedId && !roleVersions.some((item) => sameSelectionId(item.id, selectedId))) {
      setSelectedId(roleVersions[0]?.id || "");
    }
  }, [roleVersions, selectedId]);

  const visible = useMemo(() => {
    const token = query.trim().toLowerCase();
    if (!token) return roleVersions;
    return roleVersions.filter((version) =>
      [friendlyName(version), friendlyStatus(version), version.dataset_name]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(token)),
    );
  }, [query, roleVersions]);
  const selected = selectedItemOrFirst(visible, selectedId);
  const effectiveSelectedId = selected?.id ?? "";
  const activeId =
    chatStatus?.active_saved_version_id ||
    chatStatus?.saved_version_id ||
    versions.find((version) => version.runtime_loaded)?.id;

  async function useInChat(version) {
    setBusy("activate");
    try {
      await startOperation({
        type: "version_activation",
        targetId: version.id,
        launch: (requestKey) => api.activateVersion(version.id, requestKey),
      });
    } catch (error) {
      reportError(error, `activate:${version.id}`);
    } finally {
      setBusy("");
    }
  }

  async function loadTechnical(version) {
    setBusy("technical");
    try {
      setTechnical(
        version.library_kind === "model_bundle"
          ? version.technical_details
          : await api.getVersionTechnicalDetails(version.id),
      );
    } catch (error) {
      reportError(error, `technical:${version.id}`);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="page page--versions-r11">
      <PageHeader title="Models">
        <p>Manage local language models without mixing them with future vision, image, audio, or retrieval runtimes.</p>
      </PageHeader>
      <nav className="model-role-switch" aria-label="Model roles">
        {roles.map((role) => (
          <button
            type="button"
            key={role.id}
            className={effectiveRoleId === role.id ? "is-active" : ""}
            aria-pressed={effectiveRoleId === role.id}
            onClick={() => {
              setSelectedRoleId(role.id);
              setSelectedId("");
              setQuery("");
              setTechnical(null);
            }}
          >
            <span>{role.label}</span>
            <small>{formatNumber(role.registered_models || 0)}</small>
          </button>
        ))}
      </nav>
      {!roleVersions.length ? (
        <EmptyState
          title={`No ${String(selectedRole?.label || "local").toLowerCase()} model registered`}
          description={`${selectedRole?.description || "This local model role is empty."} This role has an independent runtime and cannot be confused with a Chat checkpoint.`}
          action={selectedRoleId === "text_generation" ? <Button icon={GraduationCap} onClick={() => onNavigate("train")}>Go to Train</Button> : null}
        />
      ) : (
        <div className="version-manager">
          <aside className="version-manager__list" aria-label="Language models">
            <label className="search-field">
              <Search aria-hidden="true" />
              <span className="sr-only">Search versions</span>
              <input type="search" placeholder="Find a version" value={query} onChange={(event) => setQuery(event.target.value)} />
            </label>
            <div className="version-manager__items">
              {visible.map((version) => {
                const active = String(activeId) === String(version.id);
                return (
                  <button
                    type="button"
                    key={version.id}
                    className={`version-manager__item ${sameSelectionId(effectiveSelectedId, version.id) ? "is-selected" : ""}`}
                    onClick={() => {
                      setSelectedId(version.id);
                      setTechnical(null);
                    }}
                  >
                    <span className="version-manager__name">{friendlyName(version)}</span>
                    {


                                                                                 }
                    <span className="version-manager__meta">
                      {[
                        version.model_role_label || "Language",
                        version.saved_at || version.created_at
                          ? formatDate(version.saved_at || version.created_at)
                          : "",
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                    <Status value={active ? "active" : statusValue(version)} label={active ? "Current Chat" : friendlyStatus(version)} size="small" />
                  </button>
                );
              })}
            </div>
          </aside>

          <section className="version-manager__detail" aria-label="Selected version details">
            {selected ? (
              <>
                <header className="version-detail__header">
                  <div>
                    <span className="eyebrow">{selected.model_role_label || "Language"} model · {friendlyStatus(selected)}</span>
                    <h2>{friendlyName(selected)}</h2>
                    <p>{versionSummary(selected)}</p>
                  </div>
                  <Status
                    value={String(activeId) === String(selected.id) ? "active" : statusValue(selected)}
                    label={String(activeId) === String(selected.id) ? "Current Chat" : friendlyStatus(selected)}
                  />
                </header>
                <VersionMetricStrip version={selected} activeId={activeId} />
                <DefinitionList items={normalFacts(selected, activeId)} />
                <div className="version-actions">
                  {selected.available_actions?.use_in_chat?.enabled && String(activeId) !== String(selected.id) ? (
                    <Button variant="primary" icon={MessageCircle} busy={busy === "activate"} onClick={() => useInChat(selected)}>
                      Use in Chat
                    </Button>
                  ) : null}
                  {selected.available_actions?.continue_training?.enabled ? (
                    <Button icon={GraduationCap} onClick={() => onNavigate("train", { version: selected.id })}>Continue training</Button>
                  ) : null}
                  {selected.available_actions?.evaluate?.enabled ? (
                    <Button icon={BarChart3} onClick={() => onNavigate("evaluate", { version: selected.id })}>Run evaluation</Button>
                  ) : null}
                  <Button
                    icon={FolderOpen}
                    disabled={!selected.checkpoint_path}
                    onClick={() => api.openPath(selected.checkpoint_path)}
                  >
                    Open folder
                  </Button>
                  <Button icon={Info} busy={busy === "technical"} onClick={() => loadTechnical(selected)}>Technical details</Button>
                </div>
                {isBlocked(selected) ? (
                  <div className="blocked-version-note" role="note">
                    <strong>Activation blocked</strong>
                    <span>This checkpoint was trained from a malformed legacy artifact and is preserved only for evidence.</span>
                  </div>
                ) : null}
                <section className="version-quality-evidence" aria-labelledby="version-quality-title">
                  <div className="version-quality-evidence__heading">
                    <span className="eyebrow">Measured evidence</span>
                    <h3 id="version-quality-title">Quality gates and limitations</h3>
                  </div>
                  <DefinitionList items={qualityFacts(selected)} />
                  {generationLimitation(selected) ? (
                    <p className="version-quality-evidence__limitation">{generationLimitation(selected)}</p>
                  ) : null}
                </section>
                {technical ? (
                  <Disclosure summary="Raw technical details" open>
                    <pre className="technical-json">{JSON.stringify({
                      saved_version_id: selected.id,
                      checkpoint_path: selected.checkpoint_path,
                      policy: selected.production_policy,
                      details: technical,
                    }, null, 2)}</pre>
                  </Disclosure>
                ) : null}
              </>
            ) : <EmptyState title="No matching version" description="Try a different search." />}
          </section>
        </div>
      )}
    </div>
  );
}

function friendlyName(version) {
  return version.friendly_name || version.production_policy?.friendly_name || version.display_label || version.label || "Saved version";
}

function isBlocked(version) {
  return version.production_policy?.classification === "regressed_forensic";
}

function friendlyStatus(version) {
  if (version.library_kind === "model_bundle") {
    if (version.runtime_loaded) return "Ready";
    if (version.selected_as_base) return "Base model";
    return version.integrity === "verified" ? "Registered" : "Needs attention";
  }
  if (isBlocked(version)) return "Regressed";
  if (version.production_policy?.recommended) return "Recommended";
  if (version.production_policy?.classification === "recommended_fallback") return "Rollback";
  if (version.production_policy?.classification === "experimental") return "Experimental";
  return version.integrity === "verified" ? "Verified" : "Needs attention";
}

function statusValue(version) {
  if (version.library_kind === "model_bundle") {
    return version.runtime_loaded ? "active" : version.integrity;
  }
  return isBlocked(version) ? "blocked" : version.production_policy?.recommended ? "recommended" : version.integrity;
}

function versionSummary(version) {
  if (version.library_kind === "model_bundle") {
    return version.runtime_loaded
      ? `${version.friendly_name || "The selected model"} is loaded by the private Salty native engine.`
      : "Local Chat model. Activation remains fail-closed until its registered native engine passes.";
  }
  if (isBlocked(version)) return "Malformed training artifact · activation and continuation blocked.";
  if (version.production_policy?.classification === "recovered_candidate") {
    return "Bounded recovery candidate. Retention and corrected holdout gates passed; this is not complete conversational-quality recovery.";
  }
  if (version.production_policy?.classification === "recommended_fallback") {
    return "Verified pre-OASST2 rollback checkpoint.";
  }
  return "Verified local SafeTensors checkpoint.";
}

function VersionMetricStrip({ version, activeId }) {
  if (version.library_kind === "model_bundle") {
    return (
      <div className="version-metric-strip" aria-label="Selected model summary">
        <VersionMetric label="Role" value="Chat base" />
        <VersionMetric label="Architecture" value={version.public_architecture_name || "Salty Steak"} />
        <VersionMetric label="Quantization" value={version.quantization || "Not reported"} />
        <VersionMetric label="Context" value={formatNumber(version.architectural_context_tokens || 0)} />
        <VersionMetric label="Model size" value={version.artifact_size_bytes ? formatBytes(version.artifact_size_bytes) : "Not reported"} />
      </div>
    );
  }
  const anchor = evidence(version, "english_retention_anchor_v1");
  const holdout = evidence(version, "corrected_oasst2_english_holdout_v1");
  const checkpointBytes =
    version.checkpoint_size_bytes ||
    version.total_checkpoint_bytes ||
    version.artifact_size_bytes;
  return (
    <div className="version-metric-strip" aria-label="Selected checkpoint summary">
      <VersionMetric label="Runtime" value={String(activeId) === String(version.id) ? "Current Chat" : "Not active"} />
      <VersionMetric label="Optimizer steps" value={version.additional_steps === null ? "Unknown" : formatNumber(version.additional_steps)} />
      <VersionMetric label="English anchor" value={metricSummary(anchor)} />
      <VersionMetric label="Instruction holdout" value={metricSummary(holdout)} />
      <VersionMetric label="Checkpoint size" value={checkpointBytes ? formatBytes(checkpointBytes) : "Not reported"} />
    </div>
  );
}

function VersionMetric({ label, value }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function generationLimitation(version) {
  if (version.production_policy?.classification !== "recovered_candidate") return "";
  return "Deterministic generation still failed to stop in 7 of 10 measured scenarios (0 fatal failures). Longer real training remains necessary.";
}

function evidence(version, family) {
  return version.quality_evidence?.find((item) => item.family === family);
}

function normalFacts(version, activeId) {
  if (version.library_kind === "model_bundle") {
    return [
      { label: "Model role", value: version.model_role_label || "Language" },
      { label: "Public architecture", value: version.public_architecture_name || "Salty Steak" },
      { label: "Inputs and outputs", value: `${(version.input_modalities || ["text"]).join(", ")} → ${(version.output_modalities || ["text"]).join(", ")}` },
      { label: "Runtime", value: "Private Salty native engine" },
      { label: "Chat status", value: version.runtime_loaded ? "Current Chat" : version.selected_as_base ? "Selected · not loaded" : "Not selected" },
      { label: "Format", value: `${version.model_format || "Salty Native"} · ${version.quantization || "unknown"}` },
      { label: "Context capacity", value: `${formatNumber(version.architectural_context_tokens || 0)} tokens` },
      { label: "Configured limit", value: `${formatNumber(version.configured_context_tokens || 0)} tokens` },
      { label: "Integrity", value: version.integrity === "verified" ? "SHA-256 verified at import; size still matches" : "Needs attention" },
      { label: "External service", value: version.external_service_required ? "Required" : "Not required" },
    ];
  }
  const anchor = evidence(version, "english_retention_anchor_v1");
  const holdout = evidence(version, "corrected_oasst2_english_holdout_v1");
  return [
    { label: "Model role", value: version.model_role_label || "Language" },
    { label: "Inputs and outputs", value: `${(version.input_modalities || ["text"]).join(", ")} → ${(version.output_modalities || ["text"]).join(", ")}` },
    { label: "Runtime", value: version.runtime_family === "salty_native_decoder" ? "Salty native decoder" : version.runtime_family || "Not reported" },
    { label: "Chat status", value: String(activeId) === String(version.id) ? "Current Chat" : "Not active" },
    { label: "Integrity", value: version.integrity === "verified" ? "Verified" : version.integrity },
    { label: "Training stage", value: friendlyStatus(version) },
    { label: "Known optimizer steps", value: version.additional_steps === null ? "Unknown" : formatNumber(version.additional_steps) },
    { label: "Saved", value: formatDate(version.saved_at || version.created_at) },
    { label: "English retention", value: metricSummary(anchor, true) },
    { label: "Instruction holdout", value: metricSummary(holdout, true) },
    { label: "Recommendation", value: version.production_policy?.recommended ? "Recommended starting version" : friendlyStatus(version) },
  ];
}

function qualityFacts(version) {
  if (version.library_kind === "model_bundle") {
    return [
      { label: "Artifact import", value: version.integrity === "verified" ? "Passed" : "Failed" },
      { label: "Native load", value: version.runtime_loaded ? "Passed" : "Not yet passed" },
      { label: "Configured context", value: version.context_activation_state === "active" || version.runtime_loaded ? "Active" : "Not loaded" },
    ];
  }
  const rows = (version.quality_evidence || []).map((item) => ({
    label: evidenceLabel(item.family),
    value: evidenceValue(item),
  }));
  return rows.length ? rows : [{ label: "Evaluation", value: version.evaluation_summary || "Not measured" }];
}

function evidenceLabel(family) {
  if (family === "english_retention_anchor_v1") return "English retention anchor";
  if (family === "corrected_oasst2_english_holdout_v1") return "Corrected instruction holdout";
  return String(family || "Evaluation").replace(/[_-]+/g, " ");
}

function evidenceValue(item) {
  const facts = [item.passed === true ? "Passed" : item.passed === false ? "Failed" : "Not classified"];
  if (Number.isFinite(Number(item.loss))) facts.push(`loss ${Number(item.loss).toFixed(6)}`);
  if (Number.isFinite(Number(item.regression_percent))) {
    facts.push(`${Number(item.regression_percent).toFixed(2)}% vs baseline`);
  }
  return facts.join(" · ");
}

function metricSummary(item, longLabel = false) {
  if (!item) return "Not measured";
  const facts = [
    item.passed === true
      ? longLabel ? "Passed" : "Pass"
      : item.passed === false
        ? longLabel ? "Failed" : "Fail"
        : "Not classified",
  ];
  if (Number.isFinite(Number(item.loss))) {
    facts.push(`${longLabel ? "loss " : ""}${Number(item.loss).toFixed(4)}`);
  }
  return facts.join(" · ");
}
