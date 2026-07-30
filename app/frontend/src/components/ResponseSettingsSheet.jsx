import { ChevronDown, X } from "lucide-react";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap.js";
import {
  CONTEXT_WINDOW_PRESETS,
  MAXIMUM_OUTPUT_MODES,
  MAXIMUM_OUTPUT_TOKEN_PRESETS,
  normaliseContextWindowTokens,
  normaliseMaximumOutputMode,
  normaliseMaximumOutputTokens,
} from "../workflows/composerControls.mjs";
import "../styles/response-settings-sheet.css";

export function ResponseSettingsSheet({
  title = "Response settings",
  models = [],
  selectedModelId = "",
  modelLabel = "No model selected",
  modelDetail = "Choose a local language model",
  modelReady = false,
  modelStatusLabel,
  settings,
  onSettingsChange,
  onModelChange,
  onOpenModels,
  onReset,
  onClose,
}) {
  const sheetRef = useModalFocusTrap({ onClose });
  const availableModels = normaliseModels(models, selectedModelId, modelLabel);
  const status = modelStatusLabel || (modelReady ? "Ready" : "Needs setup");
  const contextWindow = normaliseContextWindowTokens(settings.context_window_tokens);
  const outputMode = normaliseMaximumOutputMode(settings.maximum_output_mode);
  const manualOutputTokens = normaliseMaximumOutputTokens(settings.maximum_output_tokens);

  function update(patch) {
    onSettingsChange((current) => ({ ...current, ...patch }));
  }

  function updateNumber(key, minimum, maximum, event) {
    const value = event.currentTarget.valueAsNumber;
    if (!Number.isFinite(value)) return;
    update({ [key]: Math.min(maximum, Math.max(minimum, value)) });
  }

  function selectOutputMode(event) {
    const maximum_output_mode = normaliseMaximumOutputMode(event.currentTarget.value);
    update({
      maximum_output_mode,
      ...(maximum_output_mode === "manual"
        ? { maximum_output_tokens: manualOutputTokens }
        : {}),
    });
  }

  return (
    <aside ref={sheetRef} className="response-settings-sheet" role="dialog" aria-modal="true" aria-labelledby="response-settings-title" tabIndex="-1">
      <header className="response-settings-sheet__header">
        <div>
          <h2 id="response-settings-title">{title}</h2>
          <p>Model, context, and response behavior for the next reply.</p>
        </div>
        {onClose ? (
          <button className="response-settings-sheet__close" type="button" aria-label="Close response settings" onClick={onClose}>
            <X aria-hidden="true" />
          </button>
        ) : null}
      </header>

      <div className="response-settings-sheet__scroll">
        <section className="response-settings-sheet__model" aria-labelledby="response-model-title">
          <div className="response-settings-sheet__section-heading">
            <div>
              <span>Local model</span>
              <h3 id="response-model-title">{modelLabel}</h3>
              <p>{modelDetail}</p>
            </div>
            <span className={`response-settings-sheet__status ${modelReady ? "is-ready" : "is-warning"}`}>
              <i aria-hidden="true" />
              {status}
            </span>
          </div>

          <label className="response-settings-sheet__select-row">
            <span>Use model</span>
            <select
              value={String(selectedModelId || availableModels[0]?.id || "")}
              disabled={!onModelChange || !availableModels.length}
              onChange={(event) => onModelChange?.(event.currentTarget.value)}
            >
              {availableModels.map((model) => (
                <option key={model.id} value={model.id} disabled={model.disabled}>
                  {model.label}
                </option>
              ))}
            </select>
          </label>

          {onOpenModels ? (
            <button className="response-settings-sheet__text-action" type="button" onClick={onOpenModels}>
              Manage local models
            </button>
          ) : null}
        </section>

        <section className="response-settings-sheet__response" aria-labelledby="response-behavior-title">
          <div className="response-settings-sheet__section-heading response-settings-sheet__section-heading--compact">
            <div>
              <span>Response</span>
              <h3 id="response-behavior-title">Length and style</h3>
            </div>
            {onReset ? <button className="response-settings-sheet__text-action" type="button" onClick={onReset}>Reset</button> : null}
          </div>

          <div className="response-settings-sheet__quick-grid">
            <label className="response-settings-sheet__preset">
              <span>Context window</span>
              <select
                aria-label="Context window"
                value={contextWindow}
                onChange={(event) => update({ context_window_tokens: Number(event.currentTarget.value) })}
              >
                {CONTEXT_WINDOW_PRESETS.map((preset) => (
                  <option key={preset.tokens} value={preset.tokens}>{preset.label}</option>
                ))}
              </select>
            </label>
            <label className="response-settings-sheet__preset">
              <span>Max tokens</span>
              <span className="response-settings-sheet__output-controls">
                <select
                  aria-label="Max tokens mode"
                  value={outputMode}
                  onChange={selectOutputMode}
                >
                  {MAXIMUM_OUTPUT_MODES.map((mode) => (
                    <option key={mode.id} value={mode.id}>{mode.label}</option>
                  ))}
                </select>
                {outputMode === "manual" ? (
                  <select
                    aria-label="Manual max tokens"
                    value={manualOutputTokens}
                    onChange={(event) => update({ maximum_output_tokens: Number(event.currentTarget.value) })}
                  >
                    {MAXIMUM_OUTPUT_TOKEN_PRESETS.map((tokens) => (
                      <option key={tokens} value={tokens}>{tokens.toLocaleString()}</option>
                    ))}
                  </select>
                ) : null}
              </span>
            </label>
          </div>

          <details className="response-settings-sheet__advanced">
            <summary>
              <span><strong>Advanced</strong><small>Sampling, seed, and instruction</small></span>
              <ChevronDown aria-hidden="true" />
            </summary>
            <div className="response-settings-sheet__advanced-body">
              <NumericSetting label="Temperature" value={settings.temperature} min={0} max={2} step={0.05} onChange={(event) => updateNumber("temperature", 0, 2, event)} />
              <NumericSetting label="Top P" value={settings.top_p} min={0.05} max={1} step={0.05} onChange={(event) => updateNumber("top_p", 0.05, 1, event)} />
              <NumericSetting label="Top K" value={settings.top_k} min={0} max={200} step={1} onChange={(event) => updateNumber("top_k", 0, 200, event)} />
              <NumericSetting label="Repeat penalty" value={settings.repetition_penalty} min={0.8} max={2} step={0.05} onChange={(event) => updateNumber("repetition_penalty", 0.8, 2, event)} />
              <NumericSetting label="Seed" value={settings.seed} min={-1} max={2_147_483_647} step={1} onChange={(event) => updateNumber("seed", -1, 2_147_483_647, event)} />

              <label className="response-settings-sheet__trace-toggle">
                <span>
                  <strong>Raw local trace</strong>
                  <small>Developer view of this model's local &lt;think&gt; text. Activity summaries remain the default.</small>
                </span>
                <input
                  type="checkbox"
                  checked={settings.reasoning_visibility === "raw_local"}
                  onChange={(event) => update({
                    reasoning_visibility: event.currentTarget.checked ? "raw_local" : "summaries",
                  })}
                />
              </label>

              <label className="response-settings-sheet__instruction">
                <span>System instruction</span>
                <textarea
                  rows="4"
                  maxLength="4000"
                  value={settings.system_prompt || ""}
                  placeholder="Optional behavior for this conversation"
                  onChange={(event) => update({ system_prompt: event.currentTarget.value })}
                />
                <small>{String(settings.system_prompt || "").length.toLocaleString()} / 4,000</small>
              </label>
            </div>
          </details>
        </section>
      </div>
    </aside>
  );
}

function NumericSetting({ label, value, min, max, step, suffix, onChange }) {
  return (
    <label className="response-settings-sheet__numeric">
      <span>{label}</span>
      <span className="response-settings-sheet__number-field">
        <input type="number" value={value} min={min} max={max} step={step} onChange={onChange} />
        {suffix ? <small>{suffix}</small> : null}
      </span>
    </label>
  );
}

function normaliseModels(models, selectedModelId, modelLabel) {
  if (models.length) {
    return models.map((model) => ({
      ...model,
      id: String(model.id),
      label: model.label || model.friendly_name || model.display_name || "Local model",
    }));
  }
  return modelLabel && selectedModelId
    ? [{ id: String(selectedModelId), label: modelLabel, ready: true }]
    : [];
}
