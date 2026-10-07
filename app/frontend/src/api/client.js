import { createSharedRead } from "../workflows/sharedRead.mjs";
import { createLatestWriter } from "../workflows/latestWriter.mjs";

const API_ROOT = "/api";
const REQUEST_TIMEOUT_MS = 15_000;
const CHAT_TIMEOUT_MS = 960_000;
export const FRONTEND_BUILD_ID =
  typeof __SALTY_POTATO_BUILD_ID__ === "string"
    ? __SALTY_POTATO_BUILD_ID__
    : "development";

export class ApiError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = "ApiError";
    this.status = options.status ?? 0;
    this.code = options.code || "request_failed";
    this.details = options.details;
    this.isTimeout = options.isTimeout || false;
  }
}

export function assertBuildIdentity(backendBuildId, frontendBuildId = FRONTEND_BUILD_ID) {
  const backend = String(backendBuildId || "");
  const frontend = String(frontendBuildId || "");
  if (!backend || !frontend || backend !== frontend) {
    throw new ApiError(
      `Frontend build ${frontend || "unavailable"} cannot run against backend build ${backend || "unavailable"}.`,
      {
        code: "build_identity_mismatch",
        details: { frontend_build_id: frontend, backend_build_id: backend },
      },
    );
  }
  return backend;
}

export async function verifyBuildIdentity() {
  const health = await request("/health", { timeout: 5_000 });
  assertBuildIdentity(health?.build_id);
  return health;
}

function makeRequestKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `request-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function unwrap(payload) {
  if (payload && Object.prototype.hasOwnProperty.call(payload, "data")) return payload.data;
  return payload;
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("timeout"), options.timeout ?? REQUEST_TIMEOUT_MS);
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");

  let body = options.body;
  if (body !== undefined && body !== null && !(body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }
  if (options.requestKey) headers.set("Idempotency-Key", options.requestKey);

  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      method: options.method || "GET",
      headers,
      body,
      signal: controller.signal,
      cache: "no-store",
    });

    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();

    if (!response.ok) {
      const message =
        payload?.error?.message ||
        payload?.detail ||
        payload?.message ||
        `Request failed with status ${response.status}.`;
      throw new ApiError(message, {
        status: response.status,
        code: payload?.error?.code || payload?.code,
        details: payload,
      });
    }
    return unwrap(payload);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) {
      throw new ApiError("The request took longer than expected.", {
        code: "request_timeout",
        isTimeout: true,
      });
    }
    throw new ApiError("Salty Steak could not reach its local service.", {
      code: "service_unavailable",
      details: error,
    });
  } finally {
    clearTimeout(timeout);
  }
}

function withQuery(path, values = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function operationRequest(path, body, requestKey) {
  const key = requestKey || makeRequestKey();
  return request(path, {
    method: "POST",
    requestKey: key,
    body: { ...body, request_key: key },
  });
}

const readConversation = createSharedRead(id => request(`/chat/conversations/${encodeURIComponent(id)}`));
const readOperation = createSharedRead(id => request(`/operations/${encodeURIComponent(id)}`, { timeout: 5_000 }), { freshFor: 300 });
const saveUiPreferences = createLatestWriter(preferences => request("/preferences/ui", { method: "POST", body: preferences }));

export const api = {
  makeRequestKey,

  health: () => request("/health", { timeout: 5_000 }),
  getPlugins: () => request("/plugins"),
  getPluginConnectors: () => request("/plugins/connectors"),
  configurePluginConnector: (id, configuration) =>
    request(`/plugins/connectors/${encodeURIComponent(id)}/configure`, {
      method: "POST",
      body: configuration,
      timeout: 30_000,
    }),
  testPluginConnector: (id) =>
    request(`/plugins/connectors/${encodeURIComponent(id)}/test`, {
      method: "POST",
      body: {},
      timeout: 30_000,
    }),
  callPluginConnector: (id, name, argumentsPayload = {}) =>
    request(`/plugins/connectors/${encodeURIComponent(id)}/call`, {
      method: "POST",
      body: { name, arguments: argumentsPayload },
      timeout: 30_000,
    }),
  disconnectPluginConnector: (id) =>
    request(`/plugins/connectors/${encodeURIComponent(id)}/disconnect`, {
      method: "POST",
      body: {},
    }),
  getAutomationStatus: () => request("/automation/status"),
  browserPreviewUrl: (auditId) => `${API_ROOT}/automation/browser/preview/${encodeURIComponent(auditId)}`,
  grantAutomation: (payload) => request("/automation/grant", {
    method: "POST",
    body: payload,
  }),
  revokeAutomation: (payload) => request("/automation/revoke", {
    method: "POST",
    body: payload,
  }),
  invokeAutomation: (payload) => request("/automation/invoke", {
    method: "POST",
    body: payload,
    timeout: 45_000,
  }),
  getAutomationAudit: (limit = 100) => request(withQuery("/automation/audit", { limit })),
  planRuntime: (payload) =>
    request("/runtime/plan", { method: "POST", body: payload }),
  getModelRoles: () => request("/models/roles"),

  listDatasets: () => request("/datasets"),
  getDataset: (id) => request(`/datasets/${encodeURIComponent(id)}`),
  inspectDatasetPath: (path) =>
    request("/datasets/inspect", { method: "POST", body: { path } }),
  inspectDatasetFile: (file) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request("/datasets/inspect", { method: "POST", body: form });
  },
  previewDataset: (path, mapping) =>
    request("/datasets/preview", {
      method: "POST",
      body: { path, mapping },
      timeout: 120_000,
    }),
  addDataset: (payload) => request("/datasets", { method: "POST", body: payload }),
  validateDataset: (id, requestKey) =>
    operationRequest(`/datasets/${encodeURIComponent(id)}/validate`, {}, requestKey),
  preflightDataset: (id, settings) =>
    request(`/datasets/${encodeURIComponent(id)}/preflight`, {
      method: "POST",
      body: settings,
      timeout: 180_000,
    }),
  prepareDataset: (id, settings, requestKey) =>
    operationRequest(`/datasets/${encodeURIComponent(id)}/prepare`, settings, requestKey),

  getTrainingSetup: () => request("/training/setup"),
  getTrainingStatus: () => request("/training/status"),
  getTrainingHistory: (limit = 30) =>
    request(withQuery("/training/history", { limit })),
  startTraining: (payload, requestKey) => operationRequest("/training", payload, requestKey),
  startIdentityPostTraining: (requestKey) =>
    operationRequest(
      "/training/identity",
      { user_confirmed: true },
      requestKey,
    ),
  retryTrainingFinalisation: (operationId, requestKey) =>
    operationRequest(
      `/training/${encodeURIComponent(operationId)}/retry-finalisation`,
      {},
      requestKey,
    ),
  resumePostTraining: (operationId, requestKey) =>
    operationRequest(
      `/training/${encodeURIComponent(operationId)}/resume-post-training`,
      {},
      requestKey,
    ),

  listEvaluations: () => request("/evaluations"),
  startEvaluation: (payload, requestKey) =>
    operationRequest("/evaluations", payload, requestKey),

  listVersions: () => request("/versions"),
  getVersion: (id) => request(`/versions/${encodeURIComponent(id)}`),
  getVersionTechnicalDetails: (id) =>
    request(`/versions/${encodeURIComponent(id)}/technical-details`),
  getDeletionPreview: (id) => request(`/versions/${encodeURIComponent(id)}/deletion-preview`),
  deleteVersion: (id, requestKey) =>
    request(`/versions/${encodeURIComponent(id)}`, {
      method: "DELETE",
      requestKey,
      body: { request_key: requestKey },
    }),
  activateVersion: (id, requestKey) =>
    operationRequest(`/versions/${encodeURIComponent(id)}/activate`, {}, requestKey),

  getOperation: readOperation,
  getOperationEvents: (id, filters = {}) =>
    request(withQuery(`/operations/${encodeURIComponent(id)}/events`, filters)),
  listOperations: (filters = {}) => request(withQuery("/operations", filters)),
  reconcileOperation: (requestKey) =>
    request(withQuery("/operations/reconcile", { request_key: requestKey })),
  stopOperation: async (id, requestKey) => {
    const result = await operationRequest(`/operations/${encodeURIComponent(id)}/stop`, {}, requestKey);
    readOperation.invalidate(id);
    return result;
  },

  getChatStatus: () => request("/chat/status"),
  inspectChatAttachment: (file) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request("/chat/attachments/inspect", {
      method: "POST",
      body: form,
      timeout: 120_000,
    });
  },
  listChatMemories: (limit = 200) =>
    request(withQuery("/chat/memory", { limit })),
  saveChatMemory: (note) =>
    request("/chat/memory", { method: "POST", body: { note } }),
  saveConversationMemory: (conversationId, note = "") =>
    request(`/chat/conversations/${encodeURIComponent(conversationId)}/memory`, {
      method: "POST",
      body: { note },
    }),
  forgetChatMemory: (memoryId) =>
    request(`/chat/memory/${encodeURIComponent(memoryId)}`, { method: "DELETE" }),
  clearChatMemories: () => request("/chat/memory", { method: "DELETE" }),
  startAgentTask: (conversationId, instruction, requestKey, generationSettings) =>
    operationRequest(
      `/chat/conversations/${encodeURIComponent(conversationId)}/agent-tasks`,
      { instruction, generation_settings: generationSettings },
      requestKey,
    ),
  stageVisionInput: (file) => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("user_confirmed", "true");
    return request("/chat/vision-input", {
      method: "POST",
      body: form,
      timeout: 120_000,
    });
  },
  analyzeVisionInput: (
    conversationId,
    prompt,
    visionInputToken,
    requestKey,
    maximumOutputTokens = 256,
    generationSettings = null,
    attachments = [],
  ) => request(`/chat/conversations/${encodeURIComponent(conversationId)}/vision-analyses`, {
    method: "POST",
    requestKey,
    body: {
      prompt,
      vision_input_token: visionInputToken,
      maximum_output_tokens: maximumOutputTokens,
      generation_settings: generationSettings,
      attachments,
      continue_with_chat: true,
      request_key: requestKey,
    },
    timeout: CHAT_TIMEOUT_MS,
  }),
  retryVisionInput: (operationId) => request(
    `/chat/vision-analyses/${encodeURIComponent(operationId)}/retry-input`,
    { method: "POST", body: {} },
  ),
  stageScreenCaptureForVision: (auditRecordId) => request(
    "/chat/vision-input/screen-capture",
    {
      method: "POST",
      body: {
        user_confirmed: true,
        screen_capture_audit_id: auditRecordId,
      },
    },
  ),
  listConversations: (workspaceMode) => request(withQuery("/chat/conversations", { workspace_mode: workspaceMode })),
  createConversation: (workspaceMode = "chat") => request("/chat/conversations", { method: "POST", body: { workspace_mode: workspaceMode } }),
  getWorkspaceState: (mode) => request(`/chat/workspaces/${encodeURIComponent(mode)}/state`),
  getUiPreferences: () => request("/preferences/ui"),
  saveUiPreferences,
  saveWorkspaceState: (mode, state) => request(`/chat/workspaces/${encodeURIComponent(mode)}/state`, { method: "POST", body: state }),
  getConversation: readConversation,
  sendMessage: (
    conversationId,
    content,
    requestKey,
    generationSettings = null,
    attachments = [],
  ) =>
    request(`/chat/conversations/${encodeURIComponent(conversationId)}/messages`, {
      method: "POST",
      requestKey,
      body: {
        content,
        request_key: requestKey,
        generation_settings: generationSettings,
        attachments,
      },
      timeout: CHAT_TIMEOUT_MS,
    }),
  retryMessage: (conversationId, userMessageId, requestKey, generationSettings = null) =>
    request(`/chat/conversations/${encodeURIComponent(conversationId)}/messages/retry`, {
      method: "POST",
      requestKey,
      body: {
        user_message_id: userMessageId,
        request_key: requestKey,
        generation_settings: generationSettings,
      },
      timeout: CHAT_TIMEOUT_MS,
    }),
  confirmHostActionProposal: (
    conversationId,
    proposalId,
    assistantMessageId,
    generationSettings,
    confirmationText,
    requestKey,
  ) => operationRequest(
    `/chat/conversations/${encodeURIComponent(conversationId)}`
      + `/host-actions/${encodeURIComponent(proposalId)}/confirm`,
    {
      assistant_message_id: assistantMessageId,
      user_confirmed: true,
      generation_settings: generationSettings,
      confirmation_text: confirmationText || undefined,
    },
    requestKey,
  ),

  confirmImageProposal: (
    conversationId,
    proposalId,
    assistantMessageId,
    generationSettings,
    requestKey,
  ) => operationRequest(
    `/chat/conversations/${encodeURIComponent(conversationId)}`
      + `/host-actions/${encodeURIComponent(proposalId)}/confirm`,
    {
      assistant_message_id: assistantMessageId,
      user_confirmed: true,
      generation_settings: generationSettings,
    },
    requestKey,
  ),
  renameConversation: (id, title) =>
    request(`/conversations/${encodeURIComponent(id)}/rename`, {
      method: "POST",
      body: { title },
    }),
  deleteConversation: (id) =>
    request(`/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),



  setConversationPinned: (id, pinned) =>
    request(`/chat/conversations/${encodeURIComponent(id)}/pin`, {
      method: "POST",
      body: { pinned },
    }),




  openExternal: (url) =>
    request("/chat/open-external", { method: "POST", body: { url } }),
  listConversationLabels: (workspaceMode) => request(withQuery("/chat/labels", { workspace_mode: workspaceMode })),
  createConversationLabel: (name, tone, workspaceMode = "chat") =>
    request("/chat/labels", { method: "POST", body: { name, tone, workspace_mode: workspaceMode } }),
  updateConversationLabel: (id, changes) =>
    request(`/chat/labels/${encodeURIComponent(id)}`, {
      method: "POST",
      body: changes,
    }),
  deleteConversationLabel: (id) =>
    request(`/chat/labels/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setConversationLabel: (conversationId, labelId, applied) =>
    request(
      `/chat/conversations/${encodeURIComponent(conversationId)}/labels/${encodeURIComponent(labelId)}`,
      { method: "POST", body: { applied } },
    ),

  getProject: () => request("/project"),
  getAbout: () => request("/about", { timeout: 15_000 }),
  getAboutStorage: (refresh = false) =>
    request(withQuery("/about/storage", { refresh: refresh ? 1 : undefined }), {
      timeout: 180_000,
    }),
  verifyProject: (requestKey) => operationRequest("/project/verify", {}, requestKey),
  clearCache: (requestKey) => operationRequest("/project/cache/clear", {}, requestKey),
  openPath: (path) => request("/project/open-path", { method: "POST", body: { path } }),
};

export function asList(payload, keys = []) {
  if (Array.isArray(payload)) return payload;
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  if (Array.isArray(payload?.items)) return payload.items;
  return [];
}

export function asOperation(payload) {
  return payload?.operation || payload;
}
