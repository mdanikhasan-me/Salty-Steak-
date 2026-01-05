export const SUPPORTED_EXTENSIONS =
  ".txt,.jsonl,.json,.csv,.parquet,.gz,.gzip,.zst,.zstd,.zip";

export const PREPARATION_PHASES = [
  { value: "waiting", label: "Waiting" },
  { value: "reading_data", label: "Reading data" },
  { value: "creating_splits", label: "Creating splits" },
  { value: "tokenising", label: "Tokenising" },
  { value: "packing", label: "Packing" },
  { value: "saving_prepared_data", label: "Saving prepared data" },
  { value: "verifying", label: "Verifying" },
  { value: "ready", label: "Ready" },
];

export const EMPTY_PREPARATION = {
  sequence_length: 512,
  training_split: 0.9,
  validation_split: 0.1,
  packing: true,
  truncation: "right",
  random_seed: 104729,
  bos: true,
  eos: true,
  padding: true,
};
