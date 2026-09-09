import { RefreshCw, Search } from "lucide-react";
import { Status } from "../../components/Primitives.jsx";
import { sameSelectionId } from "../../workflows/selection.mjs";

export function DatasetLibraryPane({
  datasets,
  visibleDatasets,
  selectedId,
  query,
  onQueryChange,
  onSelect,
  onRefresh,
}) {
  const primary = visibleDatasets.filter((dataset) => !isLegacy(dataset));
  const legacy = visibleDatasets.filter(isLegacy);
  return (
    <aside className="library-list" aria-label="Dataset library">
      <div className="library-list__header">
        <div>
          <h2>Dataset library</h2>
          <span>{datasets.length} saved locally</span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh datasets"
          onClick={onRefresh}
        >
          <RefreshCw aria-hidden="true" />
        </button>
      </div>
      <label className="search-field">
        <Search aria-hidden="true" />
        <span className="sr-only">Search datasets</span>
        <input
          type="search"
          value={query}
          placeholder="Find a dataset"
          onChange={(event) => onQueryChange(event.target.value)}
        />
      </label>
      <div className="library-list__items">
        {primary.map((dataset) => (
          <DatasetRow
            key={dataset.id}
            dataset={dataset}
            selected={sameSelectionId(dataset.id, selectedId)}
            onSelect={onSelect}
          />
        ))}
        {legacy.length ? (
          <div className="library-list__group-label">Preserved legacy evidence</div>
        ) : null}
        {legacy.map((dataset) => (
          <DatasetRow
            key={dataset.id}
            dataset={dataset}
            selected={sameSelectionId(dataset.id, selectedId)}
            onSelect={onSelect}
          />
        ))}
      </div>
    </aside>
  );
}

function DatasetRow({ dataset, selected, onSelect }) {
  const state = datasetState(dataset);
  return (
          <button
            type="button"
            className={`library-item ${selected ? "library-item--active" : ""}`}
            aria-current={selected ? "true" : undefined}
            onClick={() => onSelect(dataset.id)}
          >
            <span className="library-item__name">{dataset.display_name || dataset.name}</span>
            <span className="library-item__file" title={dataset.source_filename}>
              {[
                dataset.source_filename,
                String(dataset.format || "").toUpperCase(),
              ].filter(Boolean).join(" · ")}
            </span>
            {dataset.training_ready && formatTokenCount(dataset.prepared_token_count ?? dataset.token_count) ? <span className="library-item__facts">{formatTokenCount(dataset.prepared_token_count ?? dataset.token_count)}</span> : null}
            <Status
              size="small"
              value={state.value}
              label={state.label}
            />
          </button>
  );
}

function formatTokenCount(value) {
  if (value === null || value === undefined || value === "") return null;
  const count = Number(value);
  return Number.isFinite(count)
    ? `${new Intl.NumberFormat().format(count)} tokens`
    : null;
}

function isLegacy(dataset) {
  return dataset.prepared_policy?.classification === "blocked_legacy";
}

function datasetState(dataset) {
  if (dataset.source_changed) return { value: "source_changed", label: "Source changed" };
  if (isLegacy(dataset)) return { value: "blocked", label: "Legacy artifact blocked" };
  if (dataset.training_ready) return { value: "ready", label: "Ready for training" };
  if (dataset.object_kind === "raw_source") return { value: "information", label: "Raw source" };




  if (dataset.validation_status === "valid") {
    return { value: "needs_preparation", label: "Ready to prepare" };
  }
  return { value: "needs_validation", label: "Needs validation" };
}
