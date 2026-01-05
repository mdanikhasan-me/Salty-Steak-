import { useEffect, useMemo, useState } from "react";
import { Database, FilePlus2, FileText, Plus } from "lucide-react";
import { api } from "../api/client.js";
import {
  Button,
  EmptyState,
  PageHeader,
} from "../components/Primitives.jsx";
import { useAppState } from "../state/AppState.jsx";
import { DatasetDetail } from "./data/DatasetDetail.jsx";
import { DatasetLibraryPane } from "./data/DatasetLibraryPane.jsx";
import { ImportDatasetDialog } from "./data/ImportDatasetDialog.jsx";
import { PreparationDialog } from "./data/PreparationDialog.jsx";
import { EMPTY_PREPARATION } from "./data/constants.js";
import { latestMatchingOperation } from "./data/dataOperations.js";
import { sameSelectionId, selectedItemOrFirst } from "../workflows/selection.mjs";

export function DataPage() {
  const {
    datasets,
    operations,
    refreshDomain,
    reportError,
    startOperation,
    connection,
  } = useAppState();
  const [selectedId, setSelectedId] = useState(null);
  const [query, setQuery] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [prepareOpen, setPrepareOpen] = useState(false);
  const [preparation, setPreparation] = useState(EMPTY_PREPARATION);
  const [preflight, setPreflight] = useState(null);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [learningPreview, setLearningPreview] = useState(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [starting, setStarting] = useState(null);

  useEffect(() => {
    void refreshDomain("datasets", { quiet: true }).catch((error) =>
      reportError(error, "open-data"),
    );
  }, [refreshDomain, reportError]);

  useEffect(() => {
    if (!selectedId && datasets[0]) setSelectedId(datasets[0].id);
    if (selectedId && !datasets.some((dataset) => sameSelectionId(dataset.id, selectedId))) {
      setSelectedId(datasets[0]?.id || null);
    }
  }, [datasets, selectedId]);

  useEffect(() => {
    setLearningPreview(null);
  }, [selectedId]);

  const visibleDatasets = useMemo(() => {
    const token = query.trim().toLowerCase();
    if (!token) return datasets;
    return datasets.filter((dataset) =>
      [dataset.name, dataset.source_filename, dataset.format]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(token)),
    );
  }, [datasets, query]);

  const selected = selectedItemOrFirst(visibleDatasets, selectedId);
  const effectiveSelectedId = selected?.id ?? null;
  const readyCount = datasets.filter((dataset) => dataset.training_ready).length;
  const totalPreparedTokens = datasets.reduce(
    (total, dataset) => total + Number(dataset.prepared_token_count || 0),
    0,
  );
  const validationOperation = latestMatchingOperation(
    operations,
    "dataset_validation",
    effectiveSelectedId,
  );
  const preparationOperation = latestMatchingOperation(
    operations,
    "dataset_preparation",
    effectiveSelectedId,
  );

  async function validateDataset() {
    if (!selected) return;
    setStarting("validation");
    try {
      await startOperation({
        type: "dataset_validation",
        targetId: selected.id,
        launch: (requestKey) => api.validateDataset(selected.id, requestKey),
      });
    } catch (error) {
      reportError(error, `validate:${selected.id}`);
    } finally {
      setStarting(null);
    }
  }

  async function prepareDataset(event) {
    event.preventDefault();
    if (!selected) return;
    setStarting("preparation");
    try {
      await startOperation({
        type: "dataset_preparation",
        targetId: selected.id,
        launch: (requestKey) =>
          api.prepareDataset(selected.id, preparation, requestKey),
      });
      setPrepareOpen(false);
    } catch (error) {
      reportError(error, `prepare:${selected.id}`);
    } finally {
      setStarting(null);
    }
  }

  async function analyzePreparation() {
    if (!selected) return;
    setPreflightBusy(true);
    try {
      setPreflight(
        await api.preflightDataset(selected.id, preparation),
      );
    } catch (error) {
      setPreflight(null);
      reportError(error, `preflight:${selected.id}`);
    } finally {
      setPreflightBusy(false);
    }
  }

  function openPreparation() {
    const semantic = ["instruction", "conversation", "oasst2"].includes(
      selected?.mapping?.type,
    );
    setPreparation({
      ...EMPTY_PREPARATION,
      sequence_length:
        selected?.mapping?.type === "oasst2"
          ? 2048
          : EMPTY_PREPARATION.sequence_length,
      packing: semantic ? false : EMPTY_PREPARATION.packing,
    });
    setPreflight(null);
    setPrepareOpen(true);
  }

  async function loadLearningPreview() {
    if (!selected) return;
    setPreviewBusy(true);
    try {
      setLearningPreview(
        await api.previewDataset(selected.source_path, selected.mapping),
      );
    } catch (error) {
      reportError(error, `learning-preview:${selected.id}`);
    } finally {
      setPreviewBusy(false);
    }
  }

  return (
    <div className="page page--data">
      <PageHeader
        title="Data"
        actions={datasets.length ? (
          <Button
            variant="primary"
            icon={Plus}
            onClick={() => setImportOpen(true)}
          >
            Add dataset
          </Button>
        ) : null}
      >
        <div className="page-context-summary" aria-label="Dataset summary">
          <span><strong>{datasets.length}</strong> local sources</span>
          <span><strong>{readyCount}</strong> training ready</span>
          <span><strong>{new Intl.NumberFormat().format(totalPreparedTokens)}</strong> prepared tokens</span>
        </div>
      </PageHeader>

      {!datasets.length && !connection.loading ? (
        <EmptyState
          icon={Database}
          title="Add your first dataset"
          description="Nothing is added, validated, or trained automatically. You stay in control of every step."
          action={
            <Button
              variant="primary"
              icon={FilePlus2}
              onClick={() => setImportOpen(true)}
            >
              Add dataset
            </Button>
          }
        />
      ) : (
        <div className="library-layout">
          <DatasetLibraryPane
            datasets={datasets}
            visibleDatasets={visibleDatasets}
            selectedId={effectiveSelectedId}
            query={query}
            onQueryChange={setQuery}
            onSelect={setSelectedId}
            onRefresh={() => refreshDomain("datasets").catch(reportError)}
          />

          <div className="library-detail">
            {selected ? (
              <DatasetDetail
                dataset={selected}
                validationOperation={validationOperation}
                preparationOperation={preparationOperation}
                starting={starting}
                onValidate={validateDataset}
                onPrepare={openPreparation}
                learningPreview={learningPreview}
                previewBusy={previewBusy}
                onLoadPreview={loadLearningPreview}
              />
            ) : (
              <EmptyState
                icon={FileText}
                title="No matching dataset"
                description="Try a different search."
              />
            )}
          </div>
        </div>
      )}

      <ImportDatasetDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onAdded={async (dataset) => {
          await refreshDomain("datasets");
          setSelectedId(dataset?.id || null);
          setImportOpen(false);
        }}
      />

      <PreparationDialog
        open={prepareOpen}
        datasetName={selected?.name}
        mappingType={selected?.mapping?.type}
        preparation={preparation}
        setPreparation={(updater) => {
          setPreflight(null);
          setPreparation(updater);
        }}
        preflight={preflight}
        preflightBusy={preflightBusy}
        starting={starting}
        onClose={() => setPrepareOpen(false)}
        onSubmit={prepareDataset}
        onAnalyze={analyzePreparation}
      />
    </div>
  );
}
