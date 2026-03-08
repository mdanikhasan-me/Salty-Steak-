import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  Check,
  ChefHat,
  ChevronDown,
  CircleAlert,
  FileText,
  Image,
  MessageCircle,
  MoreHorizontal,
  MousePointerClick,
  Paperclip,
  Pencil,
  Plug,
  Plus,
  Send,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api/client.js";
import { ChatMessage } from "../components/ChatMessage.jsx";
import { AgentActivityPanel } from "../components/AgentActivityPanel.jsx";
import { CookingActivityPanel } from "../components/CookingActivityPanel.jsx";
import { CookingStatus } from "../components/CookingStatus.jsx";
import { PluginConnectionDialog } from "../components/PluginConnectionDialog.jsx";
import { PluginsPanel } from "../components/PluginsPanel.jsx";
import { ResponseSettingsSheet } from "../components/ResponseSettingsSheet.jsx";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap.js";
import { useShell } from "../components/AppShell.jsx";
import { AboutPage } from "./AboutPage.jsx";
import { Dialog } from "../components/Dialog.jsx";
import { Field, FormActions } from "../components/Forms.jsx";
import {
  Button,
  InlineNotice,
  Status,
} from "../components/Primitives.jsx";
import { useAppState } from "../state/AppState.jsx";
import { errorMessage } from "../workflows/formatters.js";
import {
  conversationSelectionExists,
  conversationStateAfterDelete,
  groupConversationsByRecency,
  synchronizeConversationSelection,
  validConversationTitle,
} from "../workflows/conversations.mjs";
import {
  canStartChatSubmission,
  createSerialGenerationExecutor,
  isChatGenerationOperation,
  isImageGenerationOperation,
  latestRetryableUserMessageId,
  mergeFetchedConversationWithPending,
  ownsConversationGeneration,
  ownsImageProposalGeneration,
  ownsGenerationTask,
  shouldRenderConversationGeneration,
  visibleConversationForSelection,
} from "../workflows/chatGeneration.mjs";
import { normaliseToken } from "../workflows/operations.mjs";
import {
  isNearTranscriptBottom,
  shouldFollowTranscript,
} from "../workflows/transcriptScroll.mjs";
import {
  COOKING_MODES,
  COMPUTER_AUTHORITY_MODES,
  computerAuthorityLabel,
  cookingModeLabel,
  generationSettingsForRequest,
  nextEnabledMenuIndex,
  normaliseGenerationSettingsSnapshot,
  normaliseCookingMode,
  toggleComposerMenu,
} from "../workflows/composerControls.mjs";
import {
  attachmentPromptSuffix,
  validateAttachment,
  validateVisionAttachment,
} from "../workflows/chatAttachments.mjs";
import {
  createComputerControlAdapter,
  fullAccessCapabilities,
  fullAccessGrantRequest,
  normaliseAutomationStatus,
} from "../workflows/computerControlPermissions.mjs";

const SELECTED_CONVERSATION_KEY = "salty-potato:selected-conversation";
const GENERATION_SETTINGS_KEY = "salty-steak:generation-settings-v2";



const GENERATION_PREVIEW_POLL_MS = 250;
const AUTOMATION_ADAPTER = createComputerControlAdapter({
  getStatus: api.getAutomationStatus,
  grant: api.grantAutomation,
  revoke: api.revokeAutomation,
  invoke: api.invokeAutomation,
  getAudit: api.getAutomationAudit,
});
const DEFAULT_GENERATION_SETTINGS = {
  context_window_tokens: 32_768,
  maximum_output_mode: "automatic",
  maximum_output_tokens: 8_192,
  temperature: 0.8,
  top_p: 0.95,
  top_k: 40,
  repetition_penalty: 1.1,
  seed: -1,
  reasoning_mode: "instant",
  computer_authority_mode: "ask_every_time",
  web_search_enabled: false,
  system_prompt: "",
  stop_sequences: [],
};

function initialGenerationSettings() {
  try {
    const stored = window.localStorage.getItem(GENERATION_SETTINGS_KEY);
    return {
      remembered: Boolean(stored),
      settings: normaliseGenerationSettingsSnapshot(
        JSON.parse(stored || "{}"),
        DEFAULT_GENERATION_SETTINGS,
      ),
    };
  } catch {
    return { remembered: false, settings: { ...DEFAULT_GENERATION_SETTINGS } };
  }
}

export function ChatPage({ onNavigate, showAbout = false, onCloseAbout }) {
  const {
    conversations,
    chatStatus,
    operations,
    versions,
    refreshDomain,
    reportError,
    addNotification: notify,
    setResources,
    startOperation,
  } = useAppState();
  const [selectedId, setSelectedId] = useState(() => {
    try {
      return window.sessionStorage.getItem(SELECTED_CONVERSATION_KEY);
    } catch {
      return null;
    }
  });
  const [conversation, setConversation] = useState(null);
  const [draft, setDraft] = useState("");
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [sending, setSending] = useState(false);
  const [activeGeneration, setActiveGeneration] = useState(null);
  const [generationActionBusy, setGenerationActionBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [copiedId, setCopiedId] = useState(null);
  const [managedConversation, setManagedConversation] = useState(null);
  const [managementMode, setManagementMode] = useState(null);
  const [managementBusy, setManagementBusy] = useState(false);
  const [managementError, setManagementError] = useState("");
  const [renameTitle, setRenameTitle] = useState("");
  const [actionsFor, setActionsFor] = useState(null);
  const initialGenerationSettingsRef = useRef(null);
  if (!initialGenerationSettingsRef.current) {
    initialGenerationSettingsRef.current = initialGenerationSettings();
  }
  const [generationSettings, setGenerationSettingsState] = useState(
    initialGenerationSettingsRef.current.settings,
  );
  const generationDefaultsAppliedRef = useRef(
    initialGenerationSettingsRef.current.remembered,
  );
  const setGenerationSettings = useCallback((next) => {
    generationDefaultsAppliedRef.current = true;
    setGenerationSettingsState(next);
  }, []);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [settingsView, setSettingsView] = useState("response");
  const [attachments, setAttachments] = useState([]);
  const [pluginState, setPluginState] = useState(null);
  const [pluginLoadError, setPluginLoadError] = useState("");
  const [pluginBusyId, setPluginBusyId] = useState("");
  const [pluginSetup, setPluginSetup] = useState(null);
  const [pluginError, setPluginError] = useState("");
  const [modelActivationBusy, setModelActivationBusy] = useState(false);
  const [composerMenu, setComposerMenu] = useState(null);
  const [cookingActivityMessageId, setCookingActivityMessageId] = useState(null);
  const [selectedActionProposal, setSelectedActionProposal] = useState(null);
  const transcriptRef = useRef(null);
  const textareaRef = useRef(null);
  const attachmentInputRef = useRef(null);
  const imageAttachmentInputRef = useRef(null);
  const selectedIdRef = useRef(selectedId);
  const generationTaskRef = useRef(0);
  const activeGenerationRef = useRef(null);
  const [agentMode, setAgentMode] = useState(false);
  const [dismissedAgentTask, setDismissedAgentTask] = useState("");


  const [fullAccessRequest, setFullAccessRequest] = useState(null);
  const [grantingFullAccess, setGrantingFullAccess] = useState(false);


  const agentModeRef = useRef(false);
  useEffect(() => {
    agentModeRef.current = agentMode;
  }, [agentMode]);
  const serialGenerationTransitionRef = useRef(null);
  if (!serialGenerationTransitionRef.current) {
    serialGenerationTransitionRef.current = createSerialGenerationExecutor();
  }
  const followsTranscriptRef = useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const composerActionRef = useRef(null);
  const actionTriggerRefs = useRef(new Map());
  const actionMenuRefs = useRef(new Map());
  const composerMenuTriggerRefs = useRef(new Map());
  const cookingActivityTriggerRef = useRef(null);
  const readinessRefreshStartedRef = useRef(false);
  const { sidebarOpen, closeSidebar } = useShell();

  const selectConversation = useCallback((conversationId) => (
    synchronizeConversationSelection(
      selectedIdRef,
      setSelectedId,
      conversationId,
    )
  ), []);

  function closeCompactSidebar() {
    if (window.matchMedia("(max-width: 960px)").matches) closeSidebar();
  }

  const readiness = getReadiness(chatStatus);
  const globalActiveGeneration = useMemo(
    () =>
      Object.values(operations || {})
        .filter(
          (operation) =>
            isChatGenerationOperation(operation) &&
            !generationTerminal(operation),
        )
        .sort(
          (left, right) =>
            Date.parse(right.updated_at || right.created_at || 0) -
            Date.parse(left.updated_at || left.created_at || 0),
        )[0] || null,
    [operations],
  );

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  useEffect(() => {
    setCookingActivityMessageId(null);
    setComposerMenu(null);
    setActionsFor(null);
  }, [selectedId]);

  useEffect(() => {
    if (
      cookingActivityMessageId === "active" &&
      (!sending || generationTerminal(activeGeneration))
    ) {
      setCookingActivityMessageId(null);
    }
  }, [activeGeneration, cookingActivityMessageId, sending]);

  useEffect(() => {



    if (ownsConversationGeneration(globalActiveGeneration, selectedId)) {
      activeGenerationRef.current = globalActiveGeneration;
      setActiveGeneration(globalActiveGeneration);
      setSending(true);
      return;
    }
    const current = activeGenerationRef.current;
    if (current && !ownsConversationGeneration(current, selectedId)) {
      activeGenerationRef.current = null;
      setActiveGeneration(null);
      setSending(false);
      return;
    }
    const persisted = current?.id ? operations?.[current.id] : null;
    if (persisted && generationTerminal(persisted)) {
      if (isImageGenerationOperation(persisted)) {
        const owner = persisted?.result?.conversation_id
          ?? persisted?.details?.conversation_id
          ?? persisted?.target_id;
        if (owner && String(owner) === String(selectedIdRef.current)) {
          void api.getConversation(owner)
            .then((payload) => {
              if (String(owner) === String(selectedIdRef.current)) {
                setConversation(payload?.conversation || payload);
              }
            })
            .catch(() => {});
        }
      }
      activeGenerationRef.current = null;
      setActiveGeneration(null);
      setSending(false);
    }
  }, [globalActiveGeneration, operations, selectedId]);




  useEffect(() => {
    const timer = window.requestAnimationFrame(() => textareaRef.current?.focus());
    return () => window.cancelAnimationFrame(timer);
  }, []);

  const resizeComposer = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    const maximum = Math.max(120, Math.floor(window.innerHeight * 0.38));
    textarea.style.height = `${Math.min(textarea.scrollHeight, maximum)}px`;
    textarea.style.overflowY = textarea.scrollHeight > maximum ? "auto" : "hidden";
  }, []);

  useEffect(() => {
    resizeComposer();
  }, [draft, resizeComposer]);

  useEffect(() => {
    window.addEventListener("resize", resizeComposer);
    return () => window.removeEventListener("resize", resizeComposer);
  }, [resizeComposer]);

  useEffect(() => {
    if (readinessRefreshStartedRef.current) return;
    readinessRefreshStartedRef.current = true;
    void refreshDomain("chat", { quiet: true }).catch(() => {});
  }, [refreshDomain]);

  useEffect(() => {
    if (readiness.key !== "preparing") return undefined;
    let refreshInFlight = false;
    const timer = window.setInterval(() => {
      if (refreshInFlight) return;
      refreshInFlight = true;
      void refreshDomain("chat", { quiet: true })
        .catch(() => {})
        .finally(() => {
          refreshInFlight = false;
        });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [readiness.key, refreshDomain]);

  useEffect(() => {
    if (generationDefaultsAppliedRef.current || !chatStatus?.generation_defaults) return;
    generationDefaultsAppliedRef.current = true;
    setGenerationSettingsState(normaliseGenerationSettingsSnapshot(
      chatStatus.generation_defaults,
      DEFAULT_GENERATION_SETTINGS,
    ));
  }, [chatStatus?.generation_defaults]);

  useEffect(() => {
    if (!generationDefaultsAppliedRef.current) return;
    try {
      window.localStorage.setItem(
        GENERATION_SETTINGS_KEY,
        JSON.stringify(generationSettings),
      );
    } catch {

    }
  }, [generationSettings]);

  const refreshPlugins = useCallback(async () => {
    setPluginLoadError("");
    try {
      setPluginState(await api.getPlugins());
    } catch (error) {
      setPluginState(null);
      setPluginLoadError(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    void refreshPlugins();
  }, [refreshPlugins]);

  useEffect(() => {
    if (!conversations.length) {
      if (selectedId) selectConversation(null);
      return;
    }
    if (!conversations.some((item) => String(item.id) === String(selectedId))) {
      selectConversation(conversations[0].id);
    }
  }, [conversations, selectConversation, selectedId]);

  useEffect(() => {
    try {
      if (selectedId) {
        window.sessionStorage.setItem(SELECTED_CONVERSATION_KEY, selectedId);
      } else {
        window.sessionStorage.removeItem(SELECTED_CONVERSATION_KEY);
      }
    } catch {

    }
  }, [selectedId]);

  useEffect(() => {
    let cancelled = false;
    if (!selectedId) {
      setConversation(null);
      return undefined;
    }
    if (!conversationSelectionExists(conversations, selectedId)) {
      setConversation(null);
      setLoadingConversation(false);
      return undefined;
    }
    setLoadingConversation(true);
    api
      .getConversation(selectedId)
      .then((payload) => {
        if (!cancelled) {
          const fetched = payload?.conversation || payload;
          setConversation((current) =>
            mergeFetchedConversationWithPending(fetched, current),
          );
        }
      })
      .catch((error) => {
        if (!cancelled) {
          if (error) notify({ message: errorMessage(error), kind: "error" });
          reportError(error, `conversation:${selectedId}`);
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingConversation(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversations, reportError, selectedId]);

  useEffect(() => {
    const transcript = transcriptRef.current;
    if (!transcript || !shouldFollowTranscript({
      wasNearBottom: followsTranscriptRef.current,
      contentChanged: true,
    })) return;
    transcript.scrollTo({ top: transcript.scrollHeight, behavior: "auto" });
    setShowJumpToLatest(false);
  }, [conversation?.messages?.length, sending]);

  const onTranscriptScroll = useCallback((event) => {
    const nearBottom = isNearTranscriptBottom(event.currentTarget);
    followsTranscriptRef.current = nearBottom;
    setShowJumpToLatest(!nearBottom);
  }, []);

  const jumpToLatest = useCallback(() => {
    const transcript = transcriptRef.current;
    if (!transcript) return;
    transcript.scrollTo({ top: transcript.scrollHeight, behavior: "smooth" });
    followsTranscriptRef.current = true;
    setShowJumpToLatest(false);
  }, []);

  useEffect(() => {
    if (!actionsFor) return undefined;
    function closeOnOutside(event) {
      if (!event.target.closest(".conversation-actions-menu") && !event.target.closest(".conversation-actions-button")) {
        setActionsFor(null);
      }
    }
    function closeOnEscape(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        setActionsFor(null);
        actionTriggerRefs.current.get(actionsFor)?.focus();
      }
    }
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [actionsFor]);

  useEffect(() => {
    if (!composerMenu) return undefined;
    function closeOnOutside(event) {
      if (!event.target.closest(".composer-control")) setComposerMenu(null);
    }
    function closeOnEscape(event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      const trigger = composerMenuTriggerRefs.current.get(composerMenu);
      setComposerMenu(null);
      window.requestAnimationFrame(() => trigger?.focus());
    }
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [composerMenu]);






  const chooseAuthorityMode = useCallback(async (modeId) => {
    if (modeId !== "full_access") {
      setGenerationSettings((current) => ({
        ...current,
        computer_authority_mode: modeId,
      }));
      return;
    }
    let status;
    try {
      status = normaliseAutomationStatus(await AUTOMATION_ADAPTER.getStatus());
    } catch (error) {
      notify({ message: errorMessage(error), kind: "error" });
      return;
    }
    const capabilities = fullAccessCapabilities(status);
    const missing = capabilities.filter((item) => !item.alreadyGranted);
    if (!missing.length) {
      setGenerationSettings((current) => ({
        ...current,
        computer_authority_mode: "full_access",
      }));
      notify({
        message: capabilities.length
          ? `Full access is on. ${capabilities.length} capabilities are already enabled.`
          : "Full access is on, but this build has no computer capabilities available.",
        kind: capabilities.length ? "success" : "warning",
      });
      return;
    }
    setFullAccessRequest({ capabilities, missing, status });
  }, [notify, setGenerationSettings]);

  const confirmFullAccess = useCallback(async () => {
    const pending = fullAccessRequest;
    if (!pending) return;
    const request = fullAccessGrantRequest(pending.status);
    if (!request) {
      setFullAccessRequest(null);
      notify({
        message: "This build has no computer capabilities to enable.",
        kind: "warning",
      });
      return;
    }
    setGrantingFullAccess(true);
    try {
      const granted = normaliseAutomationStatus(
        await AUTOMATION_ADAPTER.grant(request),
      );
      const enabled = granted.capabilities.filter((item) => item.effectiveEnabled);
      setGenerationSettings((current) => ({
        ...current,
        computer_authority_mode: "full_access",
      }));
      setFullAccessRequest(null);
      notify({
        message: `Full access is on. ${enabled.length} of ${request.capabilities.length} capabilities are enabled.`,
        kind: enabled.length === request.capabilities.length ? "success" : "warning",
      });
    } catch (error) {
      notify({ message: errorMessage(error), kind: "error" });
    } finally {
      setGrantingFullAccess(false);
    }
  }, [fullAccessRequest, notify, setGenerationSettings]);

  const sendExactMessage = useCallback(
    async (content, requestedConversationId = selectedIdRef.current) => {
      if (!content || !content.trim()) return;
      const taskId = generationTaskRef.current + 1;
      generationTaskRef.current = taskId;
      setSending(true);
      let conversationId = requestedConversationId;
      try {
        let operation = await serialGenerationTransitionRef.current(async () => {
          conversationId = conversationId || selectedIdRef.current;
          if (!conversationId) {
            const createdPayload = await api.createConversation();
            const created = createdPayload?.conversation || createdPayload;
            conversationId = created.id;
            selectConversation(created.id);
            setConversation({ ...created, messages: created.messages || [] });
            setResources((previous) => ({
              ...previous,
              conversations: [
                created,
                ...previous.conversations.filter((item) => item.id !== created.id),
              ],
            }));
          }

          if (String(selectedIdRef.current) === String(conversationId)) {
            setConversation((previous) => ({
              ...(previous || { id: conversationId, title: "New chat" }),
              messages: [
                ...(previous?.messages || []),
                {
                  id: `pending-user-${taskId}`,
                  role: "user",
                  content,
                  pending: true,
                  created_at: new Date().toISOString(),
                },
              ],
            }));
          }


          const requestSettings = {
            ...generationSettingsForRequest(
              generationSettings,
              DEFAULT_GENERATION_SETTINGS,
            ),
            agent_mode: Boolean(agentModeRef.current),
          };
          const submitted = await api.sendMessage(
            conversationId,
            content,
            api.makeRequestKey(),
            requestSettings,
          );
          activeGenerationRef.current = submitted;
          return submitted;
        });
        if (ownsGenerationTask(generationTaskRef.current, taskId)) {
          setActiveGeneration(operation);
        }
        while (!generationTerminal(operation)) {
          await waitFor(GENERATION_PREVIEW_POLL_MS);
          operation = await api.getOperation(operation.id);
          if (ownsGenerationTask(generationTaskRef.current, taskId)) {
            activeGenerationRef.current = operation;
            setActiveGeneration(operation);
          }
        }
        if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
        const refreshed = await api.getConversation(conversationId);
        if (String(selectedIdRef.current) === String(conversationId)) {
          setConversation(refreshed?.conversation || refreshed);
        }
        if (operation.state === "failed") {
          notify({
            message: operation.error?.message || "Salty Steak could not complete this response.",
            kind: "error",
          });
        }
        await Promise.all([
          refreshDomain("conversations", { quiet: true }),
          refreshDomain("chat", { quiet: true }),
        ]);
      } catch (error) {
        if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
        setConversation((previous) => ({
          ...previous,
          messages: (previous?.messages || []).filter((message) => !message.pending),
        }));
        setDraft(content);
        if (error) notify({ message: errorMessage(error), kind: "error" });
      } finally {
        if (ownsGenerationTask(generationTaskRef.current, taskId)) {
          activeGenerationRef.current = null;
          setActiveGeneration(null);
          setSending(false);
        }
      }
    },
    [
      refreshDomain,
      selectConversation,
      setResources,
      generationSettings,
    ],
  );

  const analyzeSelectedImage = useCallback(
    async (content, imageAttachment, requestedConversationId = selectedIdRef.current) => {
      if (!content?.trim() || !imageAttachment?.file) return;
      const taskId = generationTaskRef.current + 1;
      generationTaskRef.current = taskId;
      setSending(true);
      let conversationId = requestedConversationId;
      try {
        let operation = await serialGenerationTransitionRef.current(async () => {
          conversationId = conversationId || selectedIdRef.current;
          if (!conversationId) {
            const createdPayload = await api.createConversation();
            const created = createdPayload?.conversation || createdPayload;
            conversationId = created.id;
            selectConversation(created.id);
            setConversation({ ...created, messages: created.messages || [] });
            setResources((previous) => ({
              ...previous,
              conversations: [created, ...previous.conversations.filter((item) => item.id !== created.id)],
            }));
          }
          const staged = await api.stageVisionInput(imageAttachment.file);
          const submitted = await api.analyzeVisionInput(
            conversationId,
            content,
            staged.vision_input_token,
            api.makeRequestKey(),
            128,
          );
          activeGenerationRef.current = submitted;
          return submitted;
        });
        if (ownsGenerationTask(generationTaskRef.current, taskId)) setActiveGeneration(operation);
        while (!generationTerminal(operation)) {
          await waitFor(GENERATION_PREVIEW_POLL_MS);
          operation = await api.getOperation(operation.id);
          if (ownsGenerationTask(generationTaskRef.current, taskId)) {
            activeGenerationRef.current = operation;
            setActiveGeneration(operation);
          }
        }
        if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
        const refreshed = await api.getConversation(conversationId);
        if (String(selectedIdRef.current) === String(conversationId)) {
          setConversation(refreshed?.conversation || refreshed);
        }
        if (operation.state === "failed") {
          setDraft(content);
          setAttachments([imageAttachment]);
          notify({
            message: operation.error?.message || "Salty Steak could not analyze this image.",
            kind: "error",
          });
        }
        await Promise.all([
          refreshDomain("conversations", { quiet: true }),
          refreshDomain("chat", { quiet: true }),
        ]);
      } catch (error) {
        if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
        setDraft(content);
        setAttachments([imageAttachment]);
        if (error) notify({ message: errorMessage(error), kind: "error" });
      } finally {
        if (ownsGenerationTask(generationTaskRef.current, taskId)) {
          activeGenerationRef.current = null;
          setActiveGeneration(null);
          setSending(false);
        }
      }
    },
    [refreshDomain, selectConversation, setResources, generationSettings],
  );

  async function newChat() {
    closeCompactSidebar();
    setCreating(true);
    try {
      const payload = await api.createConversation();
      const created = payload?.conversation || payload;
      setResources((previous) => ({
        ...previous,
        conversations: [created, ...previous.conversations.filter((item) => item.id !== created.id)],
      }));
      selectConversation(created.id);
      setConversation({ ...created, messages: created.messages || [] });
      setDraft("");
    } catch (error) {
      if (error) notify({ message: errorMessage(error), kind: "error" });
    } finally {
      setCreating(false);
    }
  }

  function submit(event) {
    event.preventDefault();
    if (!canStartChatSubmission({
      sending: sending || Boolean(globalActiveGeneration),
      content: draft,
    })) return;
    const imageAttachments = attachments.filter((attachment) => attachment.kind === "image");
    if (imageAttachments.length) {
      const selectedImage = imageAttachments[0];
      setDraft("");
      setAttachments([]);
      void analyzeSelectedImage(draft, selectedImage);
      return;
    }
    const attachmentText = attachmentPromptSuffix(
      attachments.filter((attachment) => attachment.kind !== "image"),
    );
    const exact = `${draft}${attachmentText}`;
    if (!["ready", "preparing"].includes(readiness.key)) {
      if (readiness.blockedMessage) notify({ message: readiness.blockedMessage, kind: "error" });
      return;
    }
    setDraft("");
    setAttachments([]);
    void sendExactMessage(exact);
  }

  async function attachFiles(event) {
    const selected = Array.from(event.target.files || []);
    event.target.value = "";
    if (!selected.length) return;
    const accepted = [];
    const rejected = [];
    let totalBytes = attachments.reduce((sum, item) => sum + item.size, 0);
    for (const file of selected) {
      if (String(file.type || "").toLowerCase().startsWith("image/")) {
        const visionValidation = validateVisionAttachment(
          file,
          chatStatus?.vision,
          attachments.filter((item) => item.kind === "image").length + accepted.filter((item) => item.kind === "image").length,
          attachments.filter((item) => item.kind !== "image").length + accepted.filter((item) => item.kind !== "image").length,
        );
        if (!visionValidation.accepted) {
          rejected.push(visionValidation.message);
          continue;
        }
        accepted.push({
          kind: "image",
          name: visionValidation.name,
          size: visionValidation.size,
          type: visionValidation.type,
          file,
        });
        continue;
      }
      const validation = validateAttachment(file, totalBytes);
      if (
        attachments.some((item) => item.kind === "image")
        || accepted.some((item) => item.kind === "image")
      ) {
        rejected.push("Text-file attachments cannot be mixed with image analysis in one request.");
        continue;
      }
      if (!validation.accepted) {
        rejected.push(validation.message);
        continue;
      }
      try {
        const content = await file.text();
        if (content.includes("\u0000")) {
          rejected.push(`${validation.name} does not appear to be a text file.`);
          continue;
        }
        accepted.push({ kind: "text", name: validation.name, size: validation.size, content });
        totalBytes += validation.size;
      } catch {
        rejected.push(`Salty Steak could not read ${validation.name} as text.`);
      }
    }
    if (accepted.length) {
      setAttachments((current) => [...current, ...accepted]);
    }
    if (rejected.length) notify({ message: rejected.join(" "), kind: "error" });
  }

  async function stopGeneration(requestedOperation = null) {
    const requested = requestedOperation?.id ? requestedOperation : null;
    const candidate = requested || activeGenerationRef.current || activeGeneration;
    if (!candidate?.id || generationActionBusy) return;
    const taskId = generationTaskRef.current + 1;
    generationTaskRef.current = taskId;
    setGenerationActionBusy(true);
    try {
      const terminal = await serialGenerationTransitionRef.current(async () => {
        const current = activeGenerationRef.current || requested || activeGeneration;
        if (!current?.id || generationTerminal(current)) return current;
        let acknowledged = await api.stopOperation(
          current.id,
          api.makeRequestKey(),
        );
        activeGenerationRef.current = acknowledged;
        if (ownsGenerationTask(generationTaskRef.current, taskId)) {
          setActiveGeneration(acknowledged);
        }
        acknowledged = await awaitTerminalOperation(
          acknowledged,
          (operation) => {
            activeGenerationRef.current = operation;
            if (ownsGenerationTask(generationTaskRef.current, taskId)) {
              setActiveGeneration(operation);
            }
          },
        );
        return acknowledged;
      });
      if (generationTerminal(terminal)) {
        setSending(false);
        setActiveGeneration(null);
        activeGenerationRef.current = null;
        const owner = terminal.target_id;
        if (
          owner &&
          String(selectedIdRef.current) === String(owner)
        ) {
          const refreshed = await api.getConversation(owner);
          setConversation(refreshed?.conversation || refreshed);
        }
      }
    } catch (error) {
      if (error) notify({ message: errorMessage(error), kind: "error" });
    } finally {
      setGenerationActionBusy(false);
      window.requestAnimationFrame(() => textareaRef.current?.focus());
    }
  }

  async function retryUserMessage(userMessageId) {
    if (!selectedId || generationActionBusy) return;
    const taskId = generationTaskRef.current + 1;
    generationTaskRef.current = taskId;
    const conversationId = selectedId;
    setGenerationActionBusy(true);
    try {
      setSending(true);
      let operation = await serialGenerationTransitionRef.current(async () => {
        const current = activeGenerationRef.current || activeGeneration;
        if (current?.id && !generationTerminal(current)) {
          let cancelled = await api.stopOperation(
            current.id,
            api.makeRequestKey(),
          );
          activeGenerationRef.current = cancelled;
          if (ownsGenerationTask(generationTaskRef.current, taskId)) {
            setActiveGeneration(cancelled);
          }
          cancelled = await awaitTerminalOperation(
            cancelled,
            (update) => {
              activeGenerationRef.current = update;
              if (ownsGenerationTask(generationTaskRef.current, taskId)) {
                setActiveGeneration(update);
              }
            },
          );
          if (!generationTerminal(cancelled)) {
            throw new Error(
              "The current response could not be stopped safely.",
            );
          }
        }
        const submitted = await api.retryMessage(
          conversationId,
          userMessageId,
          api.makeRequestKey(),
          generationSettingsForRequest(
            generationSettings,
            DEFAULT_GENERATION_SETTINGS,
          ),
        );
        activeGenerationRef.current = submitted;
        return submitted;
      });
      if (ownsGenerationTask(generationTaskRef.current, taskId)) {
        activeGenerationRef.current = operation;
        setActiveGeneration(operation);
      }
      operation = await awaitTerminalOperation(operation, (current) => {
        if (ownsGenerationTask(generationTaskRef.current, taskId)) {
          activeGenerationRef.current = current;
          setActiveGeneration(current);
        }
      });
      if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
      const refreshed = await api.getConversation(conversationId);
      if (String(selectedIdRef.current) === String(conversationId)) {
        setConversation(refreshed?.conversation || refreshed);
      }
      if (operation.state === "failed") {
        notify({
          message: operation.error?.message || "Salty Steak could not complete the retry.",
          kind: "error",
        });
      }
      await Promise.all([
        refreshDomain("conversations", { quiet: true }),
        refreshDomain("chat", { quiet: true }),
      ]);
    } catch (error) {
      if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
      if (error) notify({ message: errorMessage(error), kind: "error" });
    } finally {
      if (ownsGenerationTask(generationTaskRef.current, taskId)) {
        activeGenerationRef.current = null;
        setActiveGeneration(null);
        setSending(false);
      }
      setGenerationActionBusy(false);
      window.requestAnimationFrame(() => textareaRef.current?.focus());
    }
  }

  async function confirmHostActionProposal(message, proposal, actionSettings = null) {
    const conversationId = selectedIdRef.current;
    if (
      !conversationId
      || !message?.id
      || !proposal?.id
      || generationActionBusy
      || globalActiveGeneration
    ) {
      if (globalActiveGeneration) {
        notify({
          message: "Finish or stop the current local operation before starting this action.",
          kind: "information",
        });
      }
      return;
    }
    const taskId = generationTaskRef.current + 1;
    generationTaskRef.current = taskId;
    setGenerationActionBusy(true);
    setSending(true);
    try {
      let operation = await serialGenerationTransitionRef.current(async () => {
        const current = activeGenerationRef.current;
        if (current?.id && !generationTerminal(current)) {
          throw new Error("Another local generation is already using the model runtime.");
        }
        const requestKey = api.makeRequestKey();
        const confirmationText = proposal.kind === "system.clean_temp"
          ? actionSettings?.confirmation_text || ""
          : "";
        const operationSettings = proposal.kind === "image.generate"
          ? actionSettings
          : null;
        const submitted = await startOperation({
          type: proposal.kind === "image.generate"
            ? "chat_image_generation"
            : "chat_host_action_execution",
          targetId: conversationId,
          requestKey,
          launch: (key) => api.confirmHostActionProposal(
            conversationId,
            proposal.id,
            message.id,
            operationSettings,
            confirmationText,
            key,
          ),
        });
        activeGenerationRef.current = submitted;
        return submitted;
      });
      if (ownsGenerationTask(generationTaskRef.current, taskId)) {
        setActiveGeneration(operation);


        setGenerationActionBusy(false);
      }
      operation = await awaitTerminalOperation(operation, (current) => {
        if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
        activeGenerationRef.current = current;
        setActiveGeneration(current);
      });
      if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
      const refreshed = await api.getConversation(conversationId);
      if (String(selectedIdRef.current) === String(conversationId)) {
        setConversation(refreshed?.conversation || refreshed);
      }
      await Promise.all([
        refreshDomain("conversations", { quiet: true }),
        refreshDomain("chat", { quiet: true }),
      ]);
    } catch (error) {
      if (ownsGenerationTask(generationTaskRef.current, taskId) && error) {
        notify({ message: errorMessage(error), kind: "error" });
      }
    } finally {
      if (ownsGenerationTask(generationTaskRef.current, taskId)) {
        activeGenerationRef.current = null;
        setActiveGeneration(null);
        setSending(false);
      }
      setGenerationActionBusy(false);
    }
  }

  async function copyMessage(message) {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopiedId(message.id);
      setTimeout(() => setCopiedId(null), 1500);
    } catch {
      notify({ message: "This computer did not allow copying to the clipboard.", kind: "error" });
    }
  }

  function openConversationActions(item, event) {
    event.preventDefault();
    event.stopPropagation();
    setManagedConversation(item);
    setRenameTitle(item.title || "New chat");
    setManagementError("");
    const opening = String(actionsFor) !== String(item.id);
    setActionsFor(opening ? item.id : null);
    if (opening) {
      window.requestAnimationFrame(() => {
        actionMenuRefs.current.get(item.id)?.querySelector('[role="menuitem"]')?.focus();
      });
    }
  }

  function moveConversationMenuFocus(event, itemId) {
    const direction = ({
      ArrowDown: "next",
      ArrowUp: "previous",
      Home: "first",
      End: "last",
    })[event.key];
    if (!direction) return;
    const items = Array.from(
      actionMenuRefs.current.get(itemId)?.querySelectorAll('[role="menuitem"]') || [],
    );
    const nextIndex = nextEnabledMenuIndex(
      items.indexOf(document.activeElement),
      direction,
      items.map((item) => !item.disabled),
    );
    if (nextIndex < 0) return;
    event.preventDefault();
    items[nextIndex]?.focus();
  }

  function closeManagement() {
    if (managementBusy) return;
    setManagementMode(null);
    setManagedConversation(null);
    setManagementError("");
    window.requestAnimationFrame(() => actionTriggerRefs.current.get(managedConversation?.id)?.focus());
  }

  async function renameConversation(event) {
    event.preventDefault();
    const title = validConversationTitle(renameTitle);
    if (!title) {
      setManagementError("Enter a title with no more than 80 characters.");
      return;
    }

    setManagementBusy(true);
    setManagementError("");
    try {
      const payload = await api.renameConversation(managedConversation.id, title);
      const returned = payload?.conversation || payload;
      const updated = {
        ...managedConversation,
        ...(returned?.id ? returned : {}),
        id: managedConversation.id,
        title,
      };
      setResources((previous) => ({
        ...previous,
        conversations: previous.conversations.map((item) =>
          String(item.id) === String(updated.id) ? { ...item, ...updated } : item,
        ),
      }));
      if (String(selectedId) === String(updated.id)) {
        setConversation((previous) => (previous ? { ...previous, title } : previous));
      }
      setManagementMode(null);
      setManagedConversation(null);
      window.requestAnimationFrame(() => actionTriggerRefs.current.get(updated.id)?.focus());
    } catch (error) {
      setManagementError(errorMessage(error));
    } finally {
      setManagementBusy(false);
    }
  }

  async function deleteConversation() {
    if (!managedConversation) return;
    setManagementBusy(true);
    setManagementError("");
    try {
      const deletedId = managedConversation.id;
      await serialGenerationTransitionRef.current(() =>
        api.deleteConversation(deletedId),
      );
      if (String(activeGenerationRef.current?.target_id) === String(deletedId)) {
        generationTaskRef.current += 1;
        activeGenerationRef.current = null;
        setActiveGeneration(null);
        setSending(false);
      }
      const next = conversationStateAfterDelete(conversations, deletedId, selectedId);
      setResources((previous) => ({
        ...previous,
        conversations: previous.conversations.filter(
          (item) => String(item.id) !== String(deletedId),
        ),
      }));
      if (next.selectedWasDeleted) {
        selectConversation(next.selectedId);
        setConversation(null);
        setDraft("");
      }
      setManagementMode(null);
      setManagedConversation(null);
      window.requestAnimationFrame(() => textareaRef.current?.focus());
    } catch (error) {
      setManagementError(errorMessage(error));
    } finally {
      setManagementBusy(false);
    }
  }

  const visibleConversation = visibleConversationForSelection(conversation, selectedId);
  const messages = visibleConversation?.messages || [];
  const actionOperationByMessageId = useMemo(() => {
    const actionOperations = [activeGeneration, ...Object.values(operations || {})]
      .filter((operation) => operation && [
        "chat_image_generation",
        "chat_host_action_execution",
      ].includes(operation.type))
      .sort(
        (left, right) =>
          Date.parse(right.updated_at || right.created_at || 0)
          - Date.parse(left.updated_at || left.created_at || 0),
      );
    const matches = {};
    for (const message of messages) {
      const proposal = (message.technical_details || message.details || {})
        .host_action_proposal;
      if (!proposal?.id) continue;
      const operation = actionOperations.find((candidate) => {
        if (proposal.kind === "image.generate") {
          return ownsImageProposalGeneration(
            candidate,
            selectedId,
            proposal.id,
            message.id,
          );
        }
        const result = candidate?.result || {};
        return candidate?.type === "chat_host_action_execution"
          && String(candidate?.target_id) === String(selectedId)
          && String(result.proposal_id) === String(proposal.id)
          && String(result.assistant_message_id) === String(message.id);
      });
      if (operation) matches[String(message.id)] = operation;
    }
    return matches;
  }, [activeGeneration, messages, operations, selectedId]);
  const conversationGroups = useMemo(
    () => groupConversationsByRecency(conversations),
    [conversations],
  );
  const retryableUserMessageId = latestRetryableUserMessageId(messages);
  const emptyConversation = !loadingConversation && messages.length === 0;
  const activeVersionId =
    chatStatus?.active_target_id ||
    chatStatus?.selected_model_id ||
    chatStatus?.active_saved_version_id ||
    chatStatus?.saved_version_id;
  const activeVersion = versions.find(
    (version) => String(version.id) === String(activeVersionId),
  );
  const recoveredCandidate =
    activeVersion?.production_policy?.classification === "recovered_candidate" ||
    String(chatStatus?.active_version_label || "")
      .toLowerCase()
      .includes("bounded recovery");
  const conversationTitle = visibleConversation?.title || "New chat";
  const cookingMode = normaliseCookingMode(generationSettings.reasoning_mode);
  const activeModelLabel =
    chatStatus?.active_version_label || chatStatus?.selected_model_label || "No model";
  const modelOptions = useMemo(() => {
    const options = versions
      .filter((version) => !version.model_role || version.model_role === "language" || version.role === "language")
      .map((version) => ({
      id: String(version.id),
      label: version.friendly_name || version.display_name || version.label || version.name || "Local model",
      ready: version.runtime_eligible !== false && version.chat_eligible !== false,
      }));
    const activeId = chatStatus?.active_target_id || chatStatus?.selected_model_id;
    if (activeId && !options.some((option) => String(option.id) === String(activeId))) {
      options.unshift({
        id: String(activeId),
        label: activeModelLabel,
        ready: chatStatus?.runtime_ready !== false,
      });
    }
    return options;
  }, [
    versions,
    chatStatus?.active_target_id,
    chatStatus?.selected_model_id,
    chatStatus?.runtime_ready,
    activeModelLabel,
  ]);
  const pluginConnections = useMemo(() => Object.fromEntries(
    (pluginState?.connectors || []).map((connector) => [
      String(connector.provider || connector.id || "").replaceAll("-", "_"),
      {
        ...connector,
        status: connector.status || "disconnected",
        detail:
          connector.last_error?.message ||
          (connector.last_error?.tool_names?.length
            ? `Not found on server: ${connector.last_error.tool_names.join(", ")}`
            : "") ||
          connector.detail ||
          "",
      },
    ]),
  ), [pluginState]);
  const webSearchPlugin = (pluginState?.plugins || []).find((plugin) => plugin.id === "web_search");
  const cookingActivityMessage = messages.find(
    (message) => String(message.id) === String(cookingActivityMessageId),
  );
  const selectedImageGenerating = Boolean(
    isImageGenerationOperation(activeGeneration)
      && ownsConversationGeneration(activeGeneration, selectedId)
      && !generationTerminal(activeGeneration),
  );
  const activeReasoningMode = operationReasoningMode(
    selectedImageGenerating ? null : activeGeneration,
    cookingMode,
  );
  const selectedConversationGenerating = shouldRenderConversationGeneration(
    activeGeneration,
    selectedId,
    sending,
    messages,
  );
  const selectedConversationBusy = selectedConversationGenerating || selectedImageGenerating;
  const cookingActivityOpen = cookingActivityMessageId === "active"
    ? selectedConversationGenerating
    : Boolean(cookingActivityMessage);



  const liveAgentTask = ownsConversationGeneration(activeGeneration, selectedId)
    && !generationTerminal(activeGeneration)
    ? activeGeneration?.result?.agent_task || activeGeneration?.progress?.agent_task || null
    : null;
  const finishedAgentTask = [...messages]
    .reverse()
    .map((message) => (message.technical_details || message.details)?.agent_task)
    .find((value) => value && Array.isArray(value.steps));
  const agentRunning = Boolean(liveAgentTask);
  const agentActivityKey = agentRunning
    ? String(activeGeneration?.id || "live")
    : String(finishedAgentTask?.step_count ?? "none");
  const agentActivity = dismissedAgentTask === agentActivityKey
    ? null
    : liveAgentTask || finishedAgentTask || null;

  const openComposerMenu = (menuId) => {
    setComposerMenu((current) => toggleComposerMenu(current, menuId));
  };
  const closeComposerMenu = () => setComposerMenu(null);
  const openSettings = (view) => {
    setComposerMenu(null);
    setCookingActivityMessageId(null);
    setSettingsView(view);
    setInspectorOpen(true);
  };
  const reviewHostAction = (proposal) => {
    if (!proposal) return;
    if (proposal.kind === "image.generate") {
      onNavigate("versions", { role: "image_generation" });
      return;
    }
    setSelectedActionProposal(proposal);
    openSettings("plugins");
  };

  const selectConversationModel = async (modelId) => {
    if (!modelId || String(modelId) === String(activeVersionId) || modelActivationBusy) return;
    const model = versions.find((version) => String(version.id) === String(modelId));
    if (!model) return;
    setModelActivationBusy(true);
    try {
      await startOperation({
        type: "version_activation",
        targetId: model.id,
        launch: (requestKey) => api.activateVersion(model.id, requestKey),
      });
      await Promise.all([
        refreshDomain("versions", { quiet: true }),
        refreshDomain("chat", { quiet: true }),
      ]);
    } catch (error) {
      reportError(error, `chat-model:${model.id}`);
    } finally {
      setModelActivationBusy(false);
    }
  };

  const connectorApiId = (connectorId) => String(connectorId || "").replaceAll("_", "-");
  const connectPlugin = async (configuration) => {
    const connectorId = pluginSetup?.connectorId;
    if (!connectorId || pluginBusyId) return;
    setPluginBusyId(connectorId);
    setPluginError("");
    let configured = false;
    try {
      await api.configurePluginConnector(connectorApiId(connectorId), configuration);
      configured = true;
      const result = await api.testPluginConnector(connectorApiId(connectorId));
      await refreshPlugins();
      setPluginSetup((current) => current ? { ...current, mode: "manage" } : current);
      return result;
    } catch (error) {
      setPluginError(errorMessage(error));
      if (configured) await refreshPlugins();
      return null;
    } finally {
      setPluginBusyId("");
    }
  };
  const recheckPlugin = async () => {
    const connectorId = pluginSetup?.connectorId;
    if (!connectorId || pluginBusyId) return;
    setPluginBusyId(connectorId);
    setPluginError("");
    try {
      const result = await api.testPluginConnector(connectorApiId(connectorId));
      await refreshPlugins();
      return result;
    } catch (error) {
      setPluginError(errorMessage(error));
      await refreshPlugins();
      return null;
    } finally {
      setPluginBusyId("");
    }
  };
  const disconnectPlugin = async () => {
    const connectorId = pluginSetup?.connectorId;
    if (!connectorId || pluginBusyId) return;
    setPluginBusyId(connectorId);
    setPluginError("");
    try {
      await api.disconnectPluginConnector(connectorApiId(connectorId));
      await refreshPlugins();
      setPluginSetup(null);
    } catch (error) {
      setPluginError(errorMessage(error));
    } finally {
      setPluginBusyId("");
    }
  };
  const toggleCookingActivity = (messageId, event) => {
    cookingActivityTriggerRef.current = event?.currentTarget || null;
    setInspectorOpen(false);
    setComposerMenu(null);
    setCookingActivityMessageId((current) => (
      String(current) === String(messageId) ? null : messageId
    ));
  };
  const closeCookingActivity = useCallback(() => {
    setCookingActivityMessageId(null);
    window.requestAnimationFrame(() => cookingActivityTriggerRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!cookingActivityOpen) return undefined;
    function closeOnEscape(event) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      closeCookingActivity();
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [closeCookingActivity, cookingActivityOpen]);

  return (
    <div className={`chat-page ${sidebarOpen ? "chat-page--sidebar-open" : ""} ${
      cookingActivityOpen ? "chat-page--activity-open" : ""
    }`}>
      {sidebarOpen ? (
        <button
          type="button"
          className="chat-sidebar-backdrop"
          aria-label="Close chat sidebar"
          onClick={closeSidebar}
        />
      ) : null}
      <aside
        id="workspace-sidebar"
        className={`workspace-sidebar chat-sidebar ${
          sidebarOpen ? "workspace-sidebar--open" : "workspace-sidebar--closed"
        }`}
        aria-label="Chats"
        aria-hidden={!sidebarOpen}
        inert={!sidebarOpen ? "" : undefined}
      >
          <div className="workspace-sidebar__heading">Chats</div>
          <button
            type="button"
            className="sidebar-row sidebar-new-chat"
            disabled={creating}
            onClick={newChat}
          >
            <Plus aria-hidden="true" />
            <span>{creating ? "Creating" : "New chat"}</span>
          </button>
          <nav className="conversation-list" aria-label="Conversations">
            {conversationGroups.map((group) => (
              <section className="conversation-group" key={group.label}>
                <h2>{group.label}</h2>
                {group.items.map((item) => (
                  <div
                    key={item.id}
                    className={
                      String(item.id) === String(selectedId)
                        ? "conversation-row conversation-row--active"
                        : "conversation-row"
                    }
                  >
                    <button
                      type="button"
                      className="conversation-link"
                      aria-current={String(item.id) === String(selectedId) ? "page" : undefined}
                      onClick={() => {
                        selectConversation(item.id);
                        closeCompactSidebar();
                      }}
                    >
                      <MessageCircle aria-hidden="true" />
                      <span>{item.title || "New chat"}</span>
                    </button>
                    <button
                      type="button"
                      className="conversation-actions-button"
                      aria-label={`Actions for ${item.title || "New chat"}`}
                      aria-haspopup="menu"
                      aria-expanded={String(actionsFor) === String(item.id)}
                      aria-controls={`conversation-menu-${item.id}`}
                      ref={(element) => {
                        if (element) actionTriggerRefs.current.set(item.id, element);
                        else actionTriggerRefs.current.delete(item.id);
                      }}
                      onClick={(event) => openConversationActions(item, event)}
                      onKeyDown={(event) => {
                        if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
                        event.preventDefault();
                        setManagedConversation(item);
                        setRenameTitle(item.title || "New chat");
                        setManagementError("");
                        setActionsFor(item.id);
                        window.requestAnimationFrame(() => {
                          const items = actionMenuRefs.current
                            .get(item.id)
                            ?.querySelectorAll('[role="menuitem"]');
                          items?.[event.key === "ArrowUp" ? items.length - 1 : 0]?.focus();
                        });
                      }}
                    >
                      <MoreHorizontal aria-hidden="true" />
                    </button>
                    {String(actionsFor) === String(item.id) ? (
                      <div
                        className="conversation-actions-menu"
                        id={`conversation-menu-${item.id}`}
                        role="menu"
                        aria-label={`Actions for ${item.title || "New chat"}`}
                        ref={(element) => {
                          if (element) actionMenuRefs.current.set(item.id, element);
                          else actionMenuRefs.current.delete(item.id);
                        }}
                        onKeyDown={(event) => moveConversationMenuFocus(event, item.id)}
                      >
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => {
                            setActionsFor(null);
                            setManagementMode("rename");
                          }}
                        >
                          <Pencil aria-hidden="true" /> Rename
                        </button>
                        <button
                          type="button"
                          role="menuitem"
                          className="conversation-actions-menu__delete"
                          onClick={() => {
                            setActionsFor(null);
                            setManagementMode("delete");
                          }}
                        >
                          <Trash2 aria-hidden="true" /> Delete
                        </button>
                      </div>
                    ) : null}
                  </div>
                ))}
              </section>
            ))}
            {!conversations.length ? (
              <p className="conversation-list__empty">No conversations yet.</p>
            ) : null}
          </nav>
      </aside>

      {showAbout ? (
        <AboutPage onBack={onCloseAbout} />
      ) : (
      <section className={`conversation-workspace ${emptyConversation ? "conversation-workspace--empty" : ""}`}>
        {recoveredCandidate ? (
          <div className="model-caution-strip model-caution-strip--floating" role="note">
            <ShieldAlert aria-hidden="true" />
            <span>Limited model quality</span>
            <button type="button" onClick={() => onNavigate("versions")}>Evidence</button>
          </div>
        ) : null}
        <div ref={transcriptRef} className="message-scroll" onScroll={onTranscriptScroll}>

          {loadingConversation && !messages.length && !selectedConversationGenerating ? (
            <div className="message-loading" role="status" aria-label="Opening conversation">
              <span className="message-loading__mark" aria-hidden="true" />
              <span>Opening conversation</span>
            </div>
          ) : !messages.length && !selectedConversationGenerating ? (
            <div className="chat-welcome">
              <img src="/assets/salty-potato-symbol.svg" alt="" width="38" height="38" />
              <h1>Start a conversation</h1>
              <p>Your messages and model stay on this device.</p>
              <span className="chat-welcome__eyebrow">Private · Local · Your computer</span>
              {readiness.key === "no_version" ? (
                <button type="button" onClick={() => onNavigate("versions")}>Choose a local model</button>
              ) : null}
            </div>
          ) : (
            <div className="message-list">
              {messages.map((message) => (
                <ChatMessage
                  key={message.id}
                  message={message}
                  copied={copiedId === message.id}
                  onCopy={() => copyMessage(message)}
                  retryEligible={message.id === retryableUserMessageId}
                  retryBusy={generationActionBusy}
                  onRetry={() => retryUserMessage(message.id)}
                  cookingOpen={String(cookingActivityMessageId) === String(message.id)}
                  onOpenCooking={(event) => toggleCookingActivity(message.id, event)}
                  onReviewAction={reviewHostAction}
                  actionOperation={actionOperationByMessageId[String(message.id)] || null}
                  actionBusy={generationActionBusy}
                  onConfirmAction={(proposal, settings) =>
                    confirmHostActionProposal(message, proposal, settings)}
                  onStopAction={stopGeneration}
                />
              ))}
              {selectedConversationGenerating ? (
                <div className="message message--assistant message--generating">
                  <div className="message__avatar message__avatar--assistant" aria-hidden="true">
                    <img src="/assets/salty-potato-symbol.svg" alt="" width="22" height="22" />
                  </div>
                  <div className="message__body">
                    <header className="message__header">
                      <strong className="message__author">Salty Steak</strong>
                    </header>
                    <div className="message-generating__status">
                      {activeReasoningMode === "cooking" ? (
                      <CookingStatus
                        label={activeCookingLabel(activeGeneration?.phase, activeReasoningMode)}
                        busy={cookingActivityMessageId !== "active"}
                        active={cookingActivityMessageId === "active"}
                        controls="cooking-activity-panel"
                        onClick={(event) => toggleCookingActivity("active", event)}
                      />
                      ) : (
                        <button
                          type="button"
                          className="message-generating__instant"
                          aria-expanded={cookingActivityMessageId === "active"}
                          aria-controls="cooking-activity-panel"
                          onClick={(event) => toggleCookingActivity("active", event)}
                        >
                          <span
                            className={cookingActivityMessageId === "active"
                              ? undefined
                              : "activity-phrase activity-phrase--response"}
                            aria-hidden={cookingActivityMessageId === "active" ? undefined : "true"}
                          >
                            Responding
                          </span>
                          {cookingActivityMessageId !== "active" ? (
                            <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
                              Responding
                            </span>
                          ) : null}
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>
        {showJumpToLatest ? (
          <button type="button" className="jump-to-latest" onClick={jumpToLatest}>
            Jump to latest
          </button>
        ) : null}

        <form className="composer" onSubmit={submit}>
          <div className="composer__surface">
            <label>
            <span className="sr-only">Message Salty Steak</span>
            <textarea
              ref={textareaRef}
              rows="1"
              value={draft}
              placeholder="Message Salty Steak"
              onChange={(event) => setDraft(event.target.value)}
              onCut={() => window.requestAnimationFrame(resizeComposer)}
              onPaste={() => window.requestAnimationFrame(resizeComposer)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            </label>
            {attachments.length ? (
              <div className="composer-attachments" aria-label="Attached files">
                {attachments.map((file, index) => (
                  <span key={`${file.name}-${index}`}>
                    <Paperclip aria-hidden="true" />
                    {file.name}
                    <button
                      type="button"
                      aria-label={`Remove ${file.name}`}
                      onClick={() => setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                    >
                      <X aria-hidden="true" />
                    </button>
                  </span>
                ))}
              </div>
            ) : null}
            <div className="composer__toolbar">
              <div className="composer__tools">
                <input
                  ref={attachmentInputRef}
                  className="sr-only"
                  type="file"
                  multiple
                  accept="text
