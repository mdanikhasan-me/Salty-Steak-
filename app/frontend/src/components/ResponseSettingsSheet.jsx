import { ChevronDown, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap.js";
import {
  CONTEXT_WINDOW_PRESETS,
  MAXIMUM_OUTPUT_MODES,
  MAXIMUM_OUTPUT_TOKEN_PRESETS,
  IMAGE_ASPECT_RATIO_PRESETS,
  IMAGE_RESOLUTION_PRESETS,
  IMAGE_STEP_PRESETS,
  contextWindowForMaximumOutput,
  imageCanvasForSettings,
  normaliseContextWindowTokens,
  normaliseMaximumOutputMode,
  normaliseMaximumOutputTokens,
} from "../workflows/composerControls.mjs";
import "../styles/response-settings-sheet.css";

export function ResponseSettingsSheet({
  title = "Response settings",
  models = [],
  imageModels = [],
  selectedModelId = "",
  modelLabel = "No model selected",
  modelDetail = "Choose a local language model",
  modelReady = false,
  modelStatusLabel,
  modelBusy = false,
  settings,
  onSettingsChange,
  onModelChange,
  onOpenModels,
  onReset,
  onClose,
  initialSection = "response",
  embedded = false,
}) {
  const sheetRef = useModalFocusTrap({ active: !embedded, onClose });
  const imageSectionRef = useRef(null);
  const availableModels = normaliseModels(models, selectedModelId, modelLabel);
  const status = modelStatusLabel || (modelReady ? "Ready" : "Needs setup");
  const contextWindow = normaliseContextWindowTokens(settings.context_window_tokens);
  const outputMode = normaliseMaximumOutputMode(settings.maximum_output_mode);
  const manualOutputTokens = normaliseMaximumOutputTokens(settings.maximum_output_tokens);
  const imageCanvas = imageCanvasForSettings(
    settings.image_resolution,
    settings.image_aspect_ratio,
  );
  const availableImageModels = imageModels.length
    ? imageModels
    : [{ id: "steak-gen-1-scaledfp8", name: "Steak Gen 1 ScaledFP8" }];

  useEffect(() => {
    if (initialSection !== "image") return undefined;
    const frame = window.requestAnimationFrame(() => {
      imageSectionRef.current?.scrollIntoView({ block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [initialSection]);

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

  function selectManualOutput(event) {
    const maximum_output_tokens = Number(event.currentTarget.value);
    update({
      maximum_output_tokens,
      context_window_tokens: contextWindowForMaximumOutput(
        maximum_output_tokens,
        contextWindow,
      ),
    });
  }

  return (
    <aside ref={sheetRef} className={`response-settings-sheet ${embedded ? "settings-embedded" : ""} response-section--${initialSection}`} role={embedded ? undefined : "dialog"} aria-modal={embedded ? undefined : "true"} aria-labelledby="response-settings-title" tabIndex="-1">
      <header className="response-settings-sheet__header">
        <div>
          <h2 id="response-settings-title">{title}</h2>
          <p>{initialSection === "image" ? "Image model, canvas and quality for your next image." : "Model and response controls for this conversation."}</p>
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
              disabled={modelBusy || !onModelChange || !availableModels.length}
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
                    onChange={selectManualOutput}
                  >
                    {MAXIMUM_OUTPUT_TOKEN_PRESETS.map((tokens) => (
                      <option key={tokens} value={tokens}>{tokens.toLocaleString()}</option>
                    ))}
                  </select>
                ) : null}
              </span>
            </label>
          </div>

          <details className="response-settings-sheet__advanced" open={initialSection === "advanced" || undefined}>
            <summary>
              <span><strong>Advanced</strong><small>Sampling, seed, and instruction</small></span>
              <ChevronDown aria-hidden="true" />
            </summary>
            <div className="response-settings-sheet__advanced-body">
              <label className="response-settings-sheet__preset">
                <span>Cook reasoning budget</span>
                <select aria-label="Cook reasoning budget" value={settings.cooking_reasoning_tokens ?? 1024} onChange={event=>update({cooking_reasoning_tokens:Number(event.currentTarget.value)})}>
                  {[256,512,1024,2048,4096,8192].map(tokens=><option key={tokens} value={tokens}>{tokens.toLocaleString()} tokens{tokens===1024 ? " · Balanced" : ""}</option>)}
                </select>
                <small>Thinking allowance for Cook. Lock In uses 8,192 tokens. Your answer keeps its full Max tokens allowance.</small>
              </label>
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
              <label className="response-settings-sheet__instruction">
                <span>Stop sequences</span>
                <textarea rows="2" aria-label="Stop sequences" placeholder="One sequence per line" value={(settings.stop_sequences || []).join("\n")} onChange={(event) => update({ stop_sequences: event.target.value.split("\n").slice(0, 8).map((line) => line.slice(0, 64)) })} />
              </label>
            </div>
          </details>
        </section>

        <section ref={imageSectionRef} className="response-settings-sheet__response" aria-labelledby="image-generation-title">
          <div className="response-settings-sheet__section-heading response-settings-sheet__section-heading--compact">
            <div>
              <span>Image generation</span>
              <h3 id="image-generation-title">Model, canvas, and quality</h3>
              <p>Used whenever this conversation asks Steak Gen to create an image.</p>
            </div>
          </div>
          <div className="response-settings-sheet__quick-grid">
            <label className="response-settings-sheet__preset">
              <span>Image model</span>
              <select
                aria-label="Image model"
                value={settings.image_model_id}
                onChange={(event) => update({ image_model_id: event.currentTarget.value })}
              >
                {availableImageModels.map((model) => (
                  <option key={model.id} value={model.id}>{model.name || model.display_name}</option>
                ))}
              </select>
            </label>
            <label className="response-settings-sheet__preset">
              <span>Aspect ratio</span>
              <select
                aria-label="Image aspect ratio"
                value={settings.image_aspect_ratio}
                onChange={(event) => update({ image_aspect_ratio: event.currentTarget.value })}
              >
                {IMAGE_ASPECT_RATIO_PRESETS.map((ratio) => (
                  <option key={ratio} value={ratio}>{ratio}</option>
                ))}
              </select>
            </label>
            <label className="response-settings-sheet__preset">
              <span>Resolution</span>
              <select
                aria-label="Image resolution"
                value={settings.image_resolution}
                onChange={(event) => update({ image_resolution: Number(event.currentTarget.value) })}
              >
                {IMAGE_RESOLUTION_PRESETS.map((resolution) => (
                  <option key={resolution} value={resolution}>{resolution}px long edge</option>
                ))}
              </select>
            </label>
            <label className="response-settings-sheet__preset">
              <span>Quality</span>
              <select
                aria-label="Image quality"
                value={settings.image_steps}
                onChange={(event) => update({ image_steps: Number(event.currentTarget.value) })}
              >
                {IMAGE_STEP_PRESETS.map((steps) => (
                  <option key={steps} value={steps}>{steps} steps</option>
                ))}
              </select>
            </label>
          </div>
          <p className="response-settings-sheet__image-canvas">
            Canvas <strong>{imageCanvas.width} × {imageCanvas.height}</strong>
          </p>
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
