import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AppWindow,
  Bot,
  Brain,
  CalendarDays,
  Camera,
  Check,
  ChefHat,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  FileText,
  Github,
  Globe,
  HardDrive,
  Image,
  Mail,
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
  Terminal,
  Trash2,
  Video,
  X,
  Zap,
} from "lucide-react";
import { api, asList } from "../api/client.js";
import { ChatMessage } from "../components/ChatMessage.jsx";
import { LiveResponse } from "../components/LiveResponse.jsx";
import { AgentActivityPanel } from "../components/AgentActivityPanel.jsx";
import { ConversationSidebar } from "../components/ConversationSidebar.jsx";
import { ResponseDetails } from "../components/ResponseDetails.jsx";
import { externalLinkFromEvent } from "../workflows/externalLinks.mjs";
import { CookingActivityPanel } from "../components/CookingActivityPanel.jsx";
import { ResearchProgress } from "../components/ResearchProgress.jsx";
import { CookingStatus } from "../components/CookingStatus.jsx";
import { PluginConnectionDialog } from "../components/PluginConnectionDialog.jsx";
import { PluginsPanel } from "../components/PluginsPanel.jsx";
import { MemoryPanel } from "../components/MemoryPanel.jsx";
import { ResponseSettingsSheet } from "../components/ResponseSettingsSheet.jsx";
import { useModalFocusTrap } from "../hooks/useModalFocusTrap.js";
import { useShell } from "../components/AppShell.jsx";
import { WorkspaceSettings, GeneralSettings, CompanionSettings } from "../components/WorkspaceSettings.jsx";
import { GmailBrand, CalendarBrand, CloudBrand } from "../components/ConnectorBrand.jsx";
import { workspaceModeDetails } from "../workflows/workspaceModes.mjs";
import { recoverConversationDraft } from "../workflows/draftRecovery.mjs";
import { createLatestWriter } from "../workflows/latestWriter.mjs";
import { createConversationCache } from "../workflows/conversationCache.mjs";
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
import {
  composerCommandSuggestions,
  readComposerCommand,
} from "../workflows/composerCommands.mjs";
import { composerPickerSections } from "../workflows/composerPlugins.mjs";
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
  generationTurnSettingsForRequest,
  imageCanvasForSettings,
  nextEnabledMenuIndex,
  normaliseGenerationSettingsSnapshot,
  normaliseCookingMode,
  toggleComposerMenu,
} from "../workflows/composerControls.mjs";
import {
  validateAttachment,
  validateVisionAttachment,
} from "../workflows/chatAttachments.mjs";
import { buildVisionContactSheet } from "../workflows/visionContactSheet.mjs";
import {
  createComputerControlAdapter,
  fullAccessCapabilities,
  fullAccessGrantRequest,
  normaliseAutomationStatus,
} from "../workflows/computerControlPermissions.mjs";

const SELECTED_CONVERSATION_KEY = "salty-potato:selected-conversation";
const GENERATION_SETTINGS_KEY = "salty-steak:generation-settings-v2";
const IMAGE_DEFAULT_REVISION_KEY = "salty-steak:image-default-resolution-v1";



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
  maximum_output_tokens: 32_768,
  temperature: 0.8,
  top_p: 0.95,
  top_k: 40,
  repetition_penalty: 1.1,
  seed: -1,
  reasoning_mode: "instant",
  reasoning_visibility: "summaries",
  computer_authority_mode: "ask_every_time",
  web_search_enabled: false,
  system_prompt: "",
  stop_sequences: [],
  image_model_id: "steak-gen-1-scaledfp8",
  image_aspect_ratio: "1:1",
  image_resolution: 768,
  image_steps: 8,
};

function initialGenerationSettings(storageKey = GENERATION_SETTINGS_KEY) {
  try {
    const stored = window.localStorage.getItem(storageKey);
    const parsed = JSON.parse(stored || "{}");


    parsed.system_prompt = "";
    if (!window.localStorage.getItem(IMAGE_DEFAULT_REVISION_KEY)) {
      parsed.image_resolution = 768;
      window.localStorage.setItem(IMAGE_DEFAULT_REVISION_KEY, "768");
    }
    return {
      remembered: Boolean(stored),
      settings: normaliseGenerationSettingsSnapshot(
        parsed,
        DEFAULT_GENERATION_SETTINGS,
      ),
    };
  } catch {
    return { remembered: false, settings: { ...DEFAULT_GENERATION_SETTINGS } };
  }
}






const PLUGIN_ICONS = {
  memory: Brain,
  web_search: Globe,
  text_files: FileText,
  images: Image,
  terminal: Terminal,
  screen_capture: Camera,
  screen_recording: Video,
  app_control: AppWindow,
};

const PLUGIN_CATEGORY_ICONS = {
  attachment_plugin: Paperclip,
  computer_control_plugin: MousePointerClick,
  built_in_plugin: Plug,
};

export function pluginIcon(plugin) {
  return (
    PLUGIN_ICONS[String(plugin?.id || "")]
    || PLUGIN_CATEGORY_ICONS[String(plugin?.category || "")]
    || Plug
  );
}





const CONNECTED_APP_ICONS = {
  gmail: GmailBrand,
  "mail.google": GmailBrand,
  google_calendar: CalendarBrand,
  "google-calendar": CalendarBrand,
  icloud_calendar: CloudBrand,
  "icloud-calendar": CloudBrand,
  google_drive: HardDrive,
  discord: MessageCircle,
  github: Github,
  mcp: Plug,
};




const CONNECTED_APP_KIND_ICONS = [
  [/mail|gmail|inbox/i, Mail],
  [/calendar/i, CalendarDays],
  [/drive|storage|files/i, HardDrive],
  [/discord|chat|message/i, MessageCircle],
  [/github|git\b/i, Github],
];

export function connectedAppIcon(app) {
  const known =
    CONNECTED_APP_ICONS[String(app?.id || "")]
    || CONNECTED_APP_ICONS[String(app?.provider || "")];
  if (known) return known;
  const described = `${app?.id || ""} ${app?.provider || ""} ${app?.name || ""}`;
  const matched = CONNECTED_APP_KIND_ICONS.find(([pattern]) => pattern.test(described));
  return matched ? matched[1] : Plug;
}

const workspaceSessions = new Map();
const sharedPlugins = { value: null, readAt: 0 };
function sessionFor(mode) {
  if (!workspaceSessions.has(mode)) workspaceSessions.set(mode, { drafts: new Map(), instructions: new Map(), settings: null, views: createConversationCache(), scroll: new Map() });
  return workspaceSessions.get(mode);
}
const pendingWorkspaceSaves = new Map();
function persistWorkspace(mode, session) {
  const snapshot = {
    selectedId: session.selectedId || null, settings: session.settings,
    drafts: [...session.drafts].slice(-32).map(([id, value]) => [id, { draft: value.draft, settings: value.settings, researchMode: Boolean(value.researchMode) }]),
    instructions: [...session.instructions].slice(-64),
  };
  if (!pendingWorkspaceSaves.has(mode)) pendingWorkspaceSaves.set(mode, createLatestWriter(value => api.saveWorkspaceState(mode, value)));
  return pendingWorkspaceSaves.get(mode)(snapshot);
}
export function ChatPage(props) {
  const { workspaceMode } = useShell();
  const [loaded, setLoaded] = useState(null);
  const [loadError, setLoadError] = useState("");
  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    const session = sessionFor(workspaceMode);
    if (session.loaded) { setLoaded(workspaceMode); return; }
    api.getWorkspaceState(workspaceMode).then((saved) => {
      if (cancelled) return;
      Object.assign(session, { ...saved, drafts: new Map(saved.drafts || []), instructions: new Map(saved.instructions || []), loaded: true });
      setLoaded(workspaceMode);
    }).catch(() => { if (!cancelled) setLoadError("This workspace could not be opened. Try again."); });
    return () => { cancelled = true; };
  }, [workspaceMode]);
  if (!sessionFor(workspaceMode).loaded && loaded !== workspaceMode) return <div className="workspace-opening" role="status">{loadError || "Opening workspace…"}{loadError ? <button type="button" onClick={() => window.location.reload()}>Try again</button> : null}</div>;
  return <ChatWorkspace key={workspaceMode} {...props} workspaceMode={workspaceMode} />;
}

function ChatWorkspace({ onNavigate, showAbout = false, onCloseAbout, workspaceMode }) {
  const session = sessionFor(workspaceMode);
  const selectionKey = `${SELECTED_CONVERSATION_KEY}:${workspaceMode}`;
  const settingsKey = `${GENERATION_SETTINGS_KEY}:${workspaceMode}`;
  const freshDraft = session.drafts.get(session.selectedId || "new") || {};
  const {
    conversations: allConversations,
    chatStatus,
    operations,
    versions,
    refreshDomain,
    reportError,
    addNotification: notify,
    setResources,
    startOperation,
    applyOperations,
  } = useAppState();
  const conversations = useMemo(() => allConversations.filter((item) => (item.workspace_mode || "chat") === workspaceMode), [allConversations, workspaceMode]);
  const [selectedId, setSelectedId] = useState(() => {
    try {
      return Object.hasOwn(session, "selectedId") ? session.selectedId : window.sessionStorage.getItem(selectionKey);
    } catch {
      return null;
    }
  });
  const [conversation, setConversation] = useState(() => session.views.get(session.selectedId));
  const [draft, setDraft] = useState(freshDraft.draft || "");
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [sending, setSending] = useState(false);
  const [activeGeneration, setActiveGeneration] = useState(null);
  const [generationActionBusy, setGenerationActionBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [copiedId, setCopiedId] = useState(null);
  const [managedConversation, setManagedConversation] = useState(null);

  const [labels, setLabels] = useState(session.labels || []);


  const [responseDetailsFor, setResponseDetails] = useState(null);
  const [managementMode, setManagementMode] = useState(null);
  const [managementBusy, setManagementBusy] = useState(false);
  const [managementError, setManagementError] = useState("");
  const [renameTitle, setRenameTitle] = useState("");
  const [actionsFor, setActionsFor] = useState(null);
  const initialGenerationSettingsRef = useRef(null);
  if (!initialGenerationSettingsRef.current) {
    initialGenerationSettingsRef.current = initialGenerationSettings(settingsKey);
    if (session.settings) initialGenerationSettingsRef.current.settings = session.settings;
  }
  const [generationSettings, setGenerationSettingsState] = useState(
    initialGenerationSettingsRef.current.settings,
  );
  const generationDefaultsAppliedRef = useRef(
    initialGenerationSettingsRef.current.remembered,
  );



  const conversationInstructionDraftsRef = useRef(session.instructions);
  const setGenerationSettings = useCallback((next) => {
    generationDefaultsAppliedRef.current = true;
    setGenerationSettingsState((current) => {
      const resolved = typeof next === "function" ? next(current) : next;
      const conversationId = selectedIdRef.current;
      if (
        conversationId
        && String(resolved?.system_prompt || "")
          !== String(current?.system_prompt || "")
      ) {
        conversationInstructionDraftsRef.current.set(
          String(conversationId),
          String(resolved?.system_prompt || ""),
        );
      }
      return resolved;
    });
  }, []);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [settingsView, setSettingsView] = useState("response");
  const [attachments, setAttachments] = useState(freshDraft.attachments || []);
  const [pluginState, setPluginState] = useState(sharedPlugins.value);
  const [pluginLoadError, setPluginLoadError] = useState("");
  const [pluginBusyId, setPluginBusyId] = useState("");
  const [pluginSetup, setPluginSetup] = useState(null);
  const [pluginError, setPluginError] = useState("");
  const [modelActivationBusy, setModelActivationBusy] = useState(false);
  const [composerMenu, setComposerMenu] = useState(null);
  const [slashCommandIndex, setSlashCommandIndex] = useState(0);
  const [slashCommandsDismissed, setSlashCommandsDismissed] = useState(false);
  const [cookingActivityMessageId, setCookingActivityMessageId] = useState(null);
  const [selectedActionProposal, setSelectedActionProposal] = useState(null);
  const transcriptRef = useRef(null);
  const textareaRef = useRef(null);
  const attachmentInputRef = useRef(null);
  const selectedIdRef = useRef(selectedId);
  const locallyCreatedConversationIdsRef = useRef(new Set());
  const generationTaskRef = useRef(0);
  const activeGenerationRef = useRef(null);
  const { settingsRequest, openNotifications, closeNotifications, setWorkspaceMode } = useShell();
  const agentMode = workspaceMode === "agent";
  const modeDetails = workspaceModeDetails(workspaceMode);
  const workspaceModeRef = useRef(workspaceMode);
  workspaceModeRef.current = workspaceMode;


  const [researchMode, setResearchMode] = useState(Boolean(freshDraft.researchMode));
  const [dismissedAgentTask, setDismissedAgentTask] = useState("");


  const [fullAccessRequest, setFullAccessRequest] = useState(null);
  const [grantingFullAccess, setGrantingFullAccess] = useState(false);
  const fullAccessUpgradeCheckedRef = useRef(false);


  const agentModeRef = useRef(false);
  const researchModeRef = useRef(false);
  useEffect(() => {
    agentModeRef.current = agentMode;
  }, [agentMode]);
  useEffect(() => {
    researchModeRef.current = researchMode;
  }, [researchMode]);
  const serialGenerationTransitionRef = useRef(null);
  if (!serialGenerationTransitionRef.current) {
    serialGenerationTransitionRef.current = createSerialGenerationExecutor();
  }
  const followsTranscriptRef = useRef(session.scroll.get(session.selectedId)?.following ?? true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const composerActionRef = useRef(null);
  const actionTriggerRefs = useRef(new Map());
  const actionMenuRefs = useRef(new Map());
  const composerMenuTriggerRefs = useRef(new Map());
  const composerMenuKeyboardRef = useRef(false);
  const cookingActivityTriggerRef = useRef(null);
  const readinessRefreshStartedRef = useRef(false);
  const { sidebarOpen, closeSidebar } = useShell();
  const slashCommands = useMemo(
    () => (slashCommandsDismissed ? [] : composerCommandSuggestions(draft)),
    [draft, slashCommandsDismissed],
  );
  const boundedSlashCommandIndex = Math.min(
    slashCommandIndex,
    Math.max(0, slashCommands.length - 1),
  );

  useEffect(() => {
    setSlashCommandIndex(0);
  }, [draft]);

  const chooseSlashCommand = useCallback((command) => {
    if (!command?.command) return;
    setDraft(`${command.command} `);
    setSlashCommandsDismissed(true);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

  const draftSnapshot = useRef(null);
  draftSnapshot.current = { draft, attachments, settings: generationSettings, selectedId, researchMode };
  useLayoutEffect(() => { if (conversation?.id) session.views.set(conversation); }, [conversation, session]);
  useLayoutEffect(() => {
    const scroll = transcriptRef.current;
    const saved = session.scroll.get(selectedId);
    if (scroll) scroll.scrollTop = saved?.top ?? scroll.scrollHeight;
    followsTranscriptRef.current = saved?.following ?? true;
    return () => {
      if (scroll && selectedId && String(selectedIdRef.current) === String(selectedId)) session.scroll.set(selectedId, { top: scroll.scrollTop, following: followsTranscriptRef.current });
    };
  }, [selectedId, session]);
  const restoreFailedDraft = useCallback((ownerId, content, files = []) => {
    const restored = recoverConversationDraft({ drafts: session.drafts, ownerId,
      selectedId: selectedIdRef.current, current: draftSnapshot.current, content, attachments: files });
    if (restored) { setDraft(restored.draft); setAttachments(restored.attachments); }
    void persistWorkspace(workspaceMode, session).catch(() => {});
  }, [session, workspaceMode]);
  useEffect(() => () => {
    const latest = draftSnapshot.current;
    session.drafts.set(latest.selectedId || "new", latest);
    session.selectedId = latest.selectedId;
    session.settings = latest.settings;
    void persistWorkspace(workspaceMode, session).catch(() => {});
    generationTaskRef.current += 1;
  }, [session]);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const latest = draftSnapshot.current;
      session.drafts.set(latest.selectedId || "new", latest);
      session.selectedId = latest.selectedId; session.settings = latest.settings;
      void persistWorkspace(workspaceMode, session).catch(() => notify({ id: "workspace-save", message: "Workspace changes could not be saved. Keep the app open and try again.", kind: "error" }));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [draft, generationSettings, selectedId, session, workspaceMode, notify, researchMode]);
  const selectConversation = useCallback((conversationId) => {
    if (String(conversationId) === String(selectedIdRef.current)) return;
    session.drafts.set(selectedIdRef.current || "new", draftSnapshot.current);
    const scroll = transcriptRef.current;
    if (scroll && selectedIdRef.current) session.scroll.set(selectedIdRef.current, { top: scroll.scrollTop, following: followsTranscriptRef.current });
    const next = session.drafts.get(conversationId || "new") || {};
    setDraft(next.draft || ""); setAttachments(next.attachments || []);
    setResearchMode(Boolean(next.researchMode));
    setCookingActivityMessageId(null); setResponseDetails(null); setComposerMenu(null);
    if (next.settings) setGenerationSettingsState(next.settings);
    setConversation(session.views.get(conversationId));
    session.selectedId = conversationId;
    return synchronizeConversationSelection(selectedIdRef, setSelectedId, conversationId);
  }, [session]);

  const openConversation = useCallback(async (conversationId) => {
    const wanted = String(conversationId || "");
    if (!wanted) return;
    selectConversation(wanted);

    setConversation(session.views.get(wanted));
    setLoadingConversation(!session.views.get(wanted));
    try {
      const payload = await api.getConversation(wanted);
      if (String(selectedIdRef.current) !== wanted) return;
      const fetched = payload?.conversation || payload;
      if ((fetched.workspace_mode || "chat") !== workspaceMode) { selectConversation(null); setConversation(null); return; }
      const localDraft = conversationInstructionDraftsRef.current.get(wanted);
      setGenerationSettingsState((current) => ({
        ...current,
        system_prompt: localDraft === undefined
          ? String(fetched?.system_prompt || "")
          : localDraft,
      }));
      setConversation(fetched);
    } catch (error) {
      if (String(selectedIdRef.current) !== wanted) return;
      if (Number(error?.status || 0) === 404) {
        locallyCreatedConversationIdsRef.current.delete(wanted);
        const fallback = conversations.find(
          (item) => String(item.id) !== wanted,
        );
        selectConversation(fallback?.id || null);
        return;
      }
      reportError(error, `conversation:${wanted}`);
    } finally {
      if (String(selectedIdRef.current) === wanted) {
        setLoadingConversation(false);
      }
    }
  }, [conversations, notify, reportError, selectConversation]);

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
    let cancelled = false;
    if (session.labels && Date.now() - session.labelsReadAt < 30_000) return;
    api
      .listConversationLabels(workspaceMode)
      .then((payload) => {
        const labels = asList(payload, ["labels"]);
        session.labels = labels; session.labelsReadAt = Date.now();
        if (!cancelled) setLabels(labels);
      })
      .catch(() => {

      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setCookingActivityMessageId(null);
    setComposerMenu(null);
    setActionsFor(null);



    setResponseDetails(null);
  }, [selectedId]);

  useEffect(() => {
    if (
      cookingActivityMessageId === "active" &&
      (!sending || generationTerminal(activeGeneration))
    ) {
      const finished = conversation?.messages?.findLast((message) => message.role === "assistant" && !message.pending);
      setCookingActivityMessageId(finished?.id || null);
    }
  }, [activeGeneration, cookingActivityMessageId, sending, conversation?.messages]);

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
      if (persisted) {
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
    if (chatStatus) return;
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
      const persistentSettings = { ...generationSettings };
      delete persistentSettings.system_prompt;
      window.localStorage.setItem(
        settingsKey,
        JSON.stringify(persistentSettings),
      );
    } catch {

    }
  }, [generationSettings]);

  useEffect(() => {


    const localDraft = selectedId
      ? conversationInstructionDraftsRef.current.get(String(selectedId))
      : undefined;
    setGenerationSettingsState((current) => ({
      ...current,
      system_prompt: localDraft === undefined ? "" : localDraft,
    }));
  }, [selectedId]);

  const refreshPlugins = useCallback(async () => {
    setPluginLoadError("");
    try {
      sharedPlugins.value = await api.getPlugins(); sharedPlugins.readAt = Date.now();
      setPluginState(sharedPlugins.value);
    } catch (error) {
      setPluginState(null);
      setPluginLoadError(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    if (!sharedPlugins.value || Date.now() - sharedPlugins.readAt > 30_000) void refreshPlugins();
  }, [refreshPlugins]);

  useEffect(() => {
    if (selectedId && allConversations.some((item) => String(item.id) === String(selectedId) && (item.workspace_mode || "chat") !== workspaceMode)) { selectConversation(null); setConversation(null); return; }
    if (!selectedId && conversations.length && session.selectedId !== null) {
      selectConversation(conversations[0].id);
      return;
    }
    if (
      selectedId
      && conversations.some((item) => String(item.id) === String(selectedId))
    ) {
      locallyCreatedConversationIdsRef.current.delete(String(selectedId));
    }
  }, [conversations, selectConversation, selectedId]);

  useEffect(() => {
    try {
      if (selectedId) {
        window.sessionStorage.setItem(selectionKey, selectedId);
      } else {
        window.sessionStorage.removeItem(selectionKey);
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
    setLoadingConversation(!session.views.get(selectedId));
    api
      .getConversation(selectedId)
      .then((payload) => {
        if (!cancelled) {
          const fetched = payload?.conversation || payload;
          if ((fetched.workspace_mode || "chat") !== workspaceMode) { selectConversation(null); setConversation(null); return; }
          const localDraft = conversationInstructionDraftsRef.current.get(
            String(selectedId),
          );
          setGenerationSettingsState((current) => ({
            ...current,
            system_prompt: localDraft === undefined
              ? String(fetched?.system_prompt || "")
              : localDraft,
          }));
          setConversation((current) =>
            mergeFetchedConversationWithPending(fetched, current),
          );
        }
      })
      .catch((error) => {
        if (!cancelled) {
          if (Number(error?.status || 0) === 404) {
            locallyCreatedConversationIdsRef.current.delete(String(selectedId));
            const fallback = conversations.find(
              (item) => String(item.id) !== String(selectedId),
            );
            selectConversation(fallback?.id || null);
            setConversation(null);
            return;
          }
          reportError(error, `conversation:${selectedId}`);
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingConversation(false);
      });
    return () => {
      cancelled = true;
    };
  }, [notify, reportError, selectConversation, selectedId, workspaceMode]);

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
    const transcript = transcriptRef.current;
    const list = transcript?.querySelector(".message-list");
    if (!list) return;
    const observer = new ResizeObserver(() => {
      if (followsTranscriptRef.current) transcript.scrollTop = transcript.scrollHeight;
    });
    observer.observe(list);
    return () => observer.disconnect();
  }, [selectedId, loadingConversation, conversation?.messages?.length, sending]);

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
      if (!capabilities.length) notify({ id: "authority-unavailable", message: "No computer capabilities are available in this build.", kind: "warning" });
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
      if (enabled.length !== request.capabilities.length) notify({ id: "authority-partial", message: `Only ${enabled.length} of ${request.capabilities.length} permissions were enabled. Review Computer access.`, kind: "warning" });
    } catch (error) {
      notify({ message: errorMessage(error), kind: "error" });
    } finally {
      setGrantingFullAccess(false);
    }
  }, [fullAccessRequest, notify, setGenerationSettings]);





  useEffect(() => {
    if (
      generationSettings.computer_authority_mode !== "full_access"
      || fullAccessUpgradeCheckedRef.current
    ) {
      return undefined;
    }
    fullAccessUpgradeCheckedRef.current = true;
    let cancelled = false;
    void AUTOMATION_ADAPTER.getStatus()
      .then(normaliseAutomationStatus)
      .then((status) => {
        if (cancelled) return;
        const capabilities = fullAccessCapabilities(status);
        const missing = capabilities.filter((item) => !item.alreadyGranted);
        if (missing.length) {
          setFullAccessRequest({ capabilities, missing, status });
        }
      })
      .catch((error) => {
        if (!cancelled) {
          notify({ message: errorMessage(error), kind: "error" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [generationSettings.computer_authority_mode, notify]);

  const sendExactMessage = useCallback(
    async (
      typed,
      requestedConversationId = selectedIdRef.current,
      turnAttachments = [],
    ) => {
      if (!typed || !typed.trim()) return;


      const { content, modes: commandModes, action } = readComposerCommand(typed);
      if (action?.type === "save_memory") {
        setSending(true);
        try {
          if (content) {
            await api.saveChatMemory(content);
          } else {
            const conversationId = requestedConversationId || selectedIdRef.current;
            if (!conversationId) {
              throw new Error("Start a conversation before saving its context.");
            }
            await api.saveConversationMemory(conversationId);
          }
          notify({
            message: content
              ? "Saved to global memory."
              : "Saved this conversation context to global memory.",
            kind: "success",
          });
        } catch (error) {
          restoreFailedDraft(requestedConversationId, typed);
          notify({ message: errorMessage(error), kind: "error" });
        } finally {
          setSending(false);
        }
        return;
      }
      const taskId = generationTaskRef.current + 1;
      generationTaskRef.current = taskId;
      const turnResearchMode = researchModeRef.current;
      setSending(true);
      let conversationId = requestedConversationId;
      try {
        let operation = await serialGenerationTransitionRef.current(async () => {
          if (!conversationId) {
            const createdPayload = await api.createConversation(workspaceMode);
            const created = createdPayload?.conversation || createdPayload;
            conversationId = created.id;
            locallyCreatedConversationIdsRef.current.add(String(created.id));
            if (String(selectedIdRef.current || "") === String(requestedConversationId || "")) {
              selectConversation(created.id);
              setResearchMode(turnResearchMode);
              setConversation({ ...created, messages: created.messages || [] });
            }
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


          const requestSettings = generationTurnSettingsForRequest(
            generationSettings,
            DEFAULT_GENERATION_SETTINGS,
            {
              agentMode: agentModeRef.current,
              codeMode: workspaceModeRef.current === "code",
              workspaceMode: workspaceModeRef.current,
              researchMode: turnResearchMode,
              researchCommand: commandModes.research_mode,
              imageCommand: commandModes.image_mode,
            },
          );
          const submitted = await api.sendMessage(
            conversationId,
            content,
            api.makeRequestKey(),
            requestSettings,
            turnAttachments
              .map((item) => item.inspection)
              .filter(Boolean),
          );
          activeGenerationRef.current = submitted;
          if (String(selectedIdRef.current) === String(conversationId)) setConversation(previous => ({ ...previous,
            messages: (previous?.messages || []).map(message => message.id === `pending-user-${taskId}` ? { ...message, pending: false } : message),
          }));
          applyOperations([submitted], { announceTransitions: false });
          return submitted;
        });
        if (ownsGenerationTask(generationTaskRef.current, taskId)) {
          setActiveGeneration(operation);
        }
        while (!generationTerminal(operation) && ownsGenerationTask(generationTaskRef.current, taskId)) {
          await waitFor(GENERATION_PREVIEW_POLL_MS);
          operation = await api.getOperation(operation.id);
          if (ownsGenerationTask(generationTaskRef.current, taskId)) {
            activeGenerationRef.current = operation;
            setActiveGeneration(operation);
          }
        }
        if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
        applyOperations([operation]);
        const refreshed = await api.getConversation(conversationId);
        if (String(selectedIdRef.current) === String(conversationId)) {
          setConversation(refreshed?.conversation || refreshed);
        }
        if (operation.state === "failed") {



          restoreFailedDraft(conversationId, content, turnAttachments);
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
        if (String(selectedIdRef.current) === String(conversationId)) setConversation((previous) => ({
          ...previous,
          messages: (previous?.messages || []).filter((message) => !message.pending),
        }));
        restoreFailedDraft(conversationId, content, turnAttachments);
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

  const analyzeSelectedImages = useCallback(
    async (
      typed,
      imageAttachments,
      fileAttachments = [],
      requestedConversationId = selectedIdRef.current,
    ) => {
      if (!typed?.trim() || !imageAttachments?.length) return;
      const { content, modes: commandModes } = readComposerCommand(typed);
      const originalAttachments = [...imageAttachments, ...fileAttachments];
      const turnResearchMode = researchModeRef.current;
      const taskId = generationTaskRef.current + 1;
      generationTaskRef.current = taskId;
      setSending(true);
      let conversationId = requestedConversationId;
      try {
        let operation = await serialGenerationTransitionRef.current(async () => {
          if (!conversationId) {
            const createdPayload = await api.createConversation(workspaceMode);
            const created = createdPayload?.conversation || createdPayload;
            conversationId = created.id;
            locallyCreatedConversationIdsRef.current.add(String(created.id));
            if (String(selectedIdRef.current || "") === String(requestedConversationId || "")) {
              selectConversation(created.id);
              setResearchMode(turnResearchMode);
              setConversation({ ...created, messages: created.messages || [] });
            }
            setResources((previous) => ({
              ...previous,
              conversations: [created, ...previous.conversations.filter((item) => item.id !== created.id)],
            }));
          }
          const contactSheet = await buildVisionContactSheet(imageAttachments);
          const staged = await api.stageVisionInput(contactSheet);
          const requestSettings = generationTurnSettingsForRequest(
            generationSettings,
            DEFAULT_GENERATION_SETTINGS,
            {
              agentMode: agentModeRef.current,
              codeMode: workspaceModeRef.current === "code",
              workspaceMode: workspaceModeRef.current,
              researchMode: turnResearchMode,
              researchCommand: commandModes.research_mode,
              imageCommand: commandModes.image_mode,
            },
          );
          const submitted = await api.analyzeVisionInput(
            conversationId,
            content,
            staged.vision_input_token,
            api.makeRequestKey(),
            256,
            requestSettings,
            fileAttachments.map((item) => item.inspection).filter(Boolean),
          );
          activeGenerationRef.current = submitted;
          applyOperations([submitted], { announceTransitions: false });
          return submitted;
        });
        if (ownsGenerationTask(generationTaskRef.current, taskId)) setActiveGeneration(operation);
        while (!generationTerminal(operation) && ownsGenerationTask(generationTaskRef.current, taskId)) {
          await waitFor(GENERATION_PREVIEW_POLL_MS);
          operation = await api.getOperation(operation.id);
          if (ownsGenerationTask(generationTaskRef.current, taskId)) {
            activeGenerationRef.current = operation;
            setActiveGeneration(operation);
          }
        }
        if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
        applyOperations([operation]);
        const refreshed = await api.getConversation(conversationId);
        if (String(selectedIdRef.current) === String(conversationId)) {
          setConversation(refreshed?.conversation || refreshed);
        }
        if (operation.state === "failed") {
          restoreFailedDraft(conversationId, content, originalAttachments);
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
        restoreFailedDraft(conversationId, content, originalAttachments);
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

  function newChat() {
    closeCompactSidebar();
    selectConversation(null);
    session.selectedId = null;
    setConversation(null);
    setDraft(""); setAttachments([]);
    setGenerationSettingsState((current) => ({ ...current, system_prompt: "" }));
    setInspectorOpen(false);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  }

  function submit(event) {
    event.preventDefault();
    if (!canStartChatSubmission({
      sending: sending || Boolean(globalActiveGeneration),
      content: draft,
    })) return;
    const imageAttachments = attachments.filter((attachment) => attachment.kind === "image");
    const fileAttachments = attachments.filter((attachment) => attachment.kind !== "image");
    const composerCommand = readComposerCommand(draft);
    if (composerCommand.action?.type === "save_memory") {
      setDraft("");
      void sendExactMessage(draft);
      return;
    }
    if (imageAttachments.length) {
      setDraft("");
      setAttachments([]);
      void analyzeSelectedImages(draft, imageAttachments, fileAttachments);
      return;
    }
    if (!["ready", "preparing"].includes(readiness.key)) {
      if (readiness.blockedMessage) notify({ message: readiness.blockedMessage, kind: "error" });
      return;
    }
    setDraft("");
    setAttachments([]);
    void sendExactMessage(draft, selectedIdRef.current, fileAttachments);
  }

  async function attachFiles(event) {
    const ownerId = selectedIdRef.current;
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
        totalBytes += visionValidation.size;
        continue;
      }
      const validation = validateAttachment(
        file,
        totalBytes,
        attachments.length + accepted.length,
      );
      if (!validation.accepted) {
        rejected.push(validation.message);
        continue;
      }
      try {
        const inspection = await api.inspectChatAttachment(file);
        accepted.push({
          kind: "file",
          name: validation.name,
          size: validation.size,
          type: String(file.type || inspection.media_type || ""),
          inspection,
        });
        totalBytes += validation.size;
      } catch (error) {
        rejected.push(`${validation.name}: ${errorMessage(error)}`);
      }
    }
    if (accepted.length) {
      if (String(ownerId || "") === String(selectedIdRef.current || "")) {
        setAttachments((current) => [...current, ...accepted]);
      } else {
        const saved = session.drafts.get(ownerId || "new") || {};
        session.drafts.set(ownerId || "new", { ...saved, attachments: [...(saved.attachments || []), ...accepted] });
      }
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
        const current = candidate;
        if (!current?.id || generationTerminal(current)) return current;
        let acknowledged = await api.stopOperation(
          current.id,
          api.makeRequestKey(),
        );
        applyOperations([acknowledged]);
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
        applyOperations([terminal]);
        setSending(false);
        setActiveGeneration(null);
        activeGenerationRef.current = null;
        const owner = terminal.target_id;
        if (
          owner &&
          String(selectedIdRef.current) === String(owner)
        ) {
          const refreshed = await api.getConversation(owner);
          if (String(selectedIdRef.current) === String(owner)) setConversation(refreshed?.conversation || refreshed);
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
    if (!selectedId || generationActionBusy || (globalActiveGeneration && !ownsConversationGeneration(globalActiveGeneration, selectedId))) return;
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
          generationTurnSettingsForRequest(
            generationSettings,
            DEFAULT_GENERATION_SETTINGS,
            { agentMode, codeMode: workspaceMode === "code", workspaceMode, researchMode },
          ),
        );
        activeGenerationRef.current = submitted;
        applyOperations([submitted], { announceTransitions: false });
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
      }, () => ownsGenerationTask(generationTaskRef.current, taskId));
      if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
      applyOperations([operation]);
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
      }, () => ownsGenerationTask(generationTaskRef.current, taskId));
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










  const openExternalLink = useCallback((event) => {
    const href = externalLinkFromEvent(event);
    if (!href) return;
    event.preventDefault();
    api.openExternal(href).catch((error) => reportError(error, "open-link"));
  }, [reportError]);

  async function renameConversationTo(item, title) {


    const checked = validConversationTitle(title);
    if (!checked) return;
    try {
      await api.renameConversation(item.id, checked);
      setResources((previous) => ({
        ...previous,
        conversations: previous.conversations.map((row) =>
          String(row.id) === String(item.id) ? { ...row, title: checked } : row,
        ),
      }));
      if (String(selectedId) === String(item.id)) {
        setConversation((previous) =>
          previous ? { ...previous, title: checked } : previous,
        );
      }
    } catch (error) {
      reportError(error, "rename-conversation");
    }
  }

  async function reloadOrganisation() {
    try {
      const [conversationPayload, labelPayload] = await Promise.all([
        api.listConversations(),
        api.listConversationLabels(workspaceMode),
      ]);
      setResources((previous) => ({
        ...previous,
        conversations: asList(conversationPayload, ["conversations"]),
      }));
      setLabels(asList(labelPayload, ["labels"]));
    } catch (error) {
      reportError(error, "chat-organisation");
    }
  }

  async function toggleConversationPin(item, pinned) {
    try {
      await api.setConversationPinned(item.id, pinned);
      await reloadOrganisation();
    } catch (error) {
      reportError(error, "pin-conversation");
    }
  }




  async function moveConversationToFolder(item, folderId) {
    try {
      for (const current of item.labels || []) {
        if (String(current.id) !== String(folderId)) {
          await api.setConversationLabel(item.id, current.id, false);
        }
      }
      if (folderId) {
        await api.setConversationLabel(item.id, folderId, true);
      }
      await reloadOrganisation();
    } catch (error) {
      reportError(error, "move-conversation");
    }
  }

  async function createFolderForConversation(name, item) {
    try {
      const created = await api.createConversationLabel(name, "neutral", workspaceMode);
      const folder = created?.label || created;
      if (item && folder?.id) {
        await moveConversationToFolder(item, folder.id);
        return;
      }
      await reloadOrganisation();
    } catch (error) {
      reportError(error, "create-folder");
    }
  }

  async function renameFolder(folder, name) {
    try {
      await api.updateConversationLabel(folder.id, { name });
      await reloadOrganisation();
    } catch (error) {
      reportError(error, "rename-folder");
    }
  }

  async function removeFolder(folder) {
    try {
      await api.deleteConversationLabel(folder.id);
      await reloadOrganisation();
    } catch (error) {
      reportError(error, "delete-folder");
    }
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
      locallyCreatedConversationIdsRef.current.delete(String(deletedId));
      session.drafts.delete(String(deletedId));
      session.views.delete(deletedId); session.scroll.delete(deletedId);
      session.instructions.delete(String(deletedId));
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
  const selectedImageCanvas = imageCanvasForSettings(
    generationSettings.image_resolution,
    generationSettings.image_aspect_ratio,
  );
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




  const pickerSections = useMemo(
    () => composerPickerSections(pluginState),
    [pluginState],
  );
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
  const showAgentActivity = Boolean(agentActivity) && (agentRunning || !cookingActivityOpen);
  const showCookingActivity = cookingActivityOpen && !agentRunning;
  const activityWorkspaceOpen = showAgentActivity || showCookingActivity;

  useEffect(() => {
    if (agentRunning) setResponseDetails(null);
  }, [agentRunning]);

  const openComposerMenu = (menuId, event) => {
    closeNotifications();
    composerMenuKeyboardRef.current = event?.detail === 0;
    setComposerMenu((current) => toggleComposerMenu(current, menuId));
  };
  const closeComposerMenu = () => setComposerMenu(null);
  const openSettings = (view) => {
    closeNotifications();
    setComposerMenu(null);
    setCookingActivityMessageId(null);
    setSettingsView(view);
    setInspectorOpen(true);
  };
  useEffect(() => {
    if (settingsRequest?.close) { setInspectorOpen(false);setComposerMenu(null);setCookingActivityMessageId(null); }
    else if (settingsRequest) openSettings(settingsRequest.section);
  }, [settingsRequest]);
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



    setResponseDetails(null);
    if (agentRunning) {
      setDismissedAgentTask(null);
      setCookingActivityMessageId(null);
      return;
    }
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
    <div data-workspace-mode={workspaceMode} className={`chat-page ${sidebarOpen ? "chat-page--sidebar-open" : ""} ${
      activityWorkspaceOpen ? "chat-page--activity-open" : ""
    } ${responseDetailsFor ? "chat-page--details-open" : ""}`}>
      {sidebarOpen ? (
        <button
          type="button"
          className="chat-sidebar-backdrop"
          aria-label="Close chat sidebar"
          onClick={closeSidebar}
        />
      ) : null}
      <ConversationSidebar
        open={sidebarOpen}
        conversations={conversations}
        folders={labels}
        selectedId={selectedId}
        creating={creating}
        onNewChat={newChat}
        onSelect={(conversationId) => void openConversation(conversationId)}
        onAfterSelect={closeCompactSidebar}
        onRename={(item, title) => renameConversationTo(item, title)}
        onDelete={(item) => {
          setManagedConversation(item);
          setManagementError("");
          setManagementMode("delete");
        }}
        onTogglePin={toggleConversationPin}
        onMoveToFolder={moveConversationToFolder}
        onCreateFolder={createFolderForConversation}
        onRenameFolder={renameFolder}
        onDeleteFolder={removeFolder}
        mode={workspaceMode}
        onSettings={openSettings}
        onTraining={() => onNavigate("training-center")}
        onNotifications={openNotifications}
      />

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
        <div
          ref={transcriptRef}
          className="message-scroll"
          onScroll={onTranscriptScroll}
          onClick={openExternalLink}
        >

          {loadingConversation && !messages.length && !selectedConversationGenerating ? (
            <div className="message-loading" role="status" aria-label="Opening conversation">
              <span className="message-loading__mark" aria-hidden="true" />
              <span>Opening conversation</span>
            </div>
          ) : !messages.length && !selectedConversationGenerating ? (
            <div className="chat-welcome">
              <h1>{modeDetails.heading}</h1>
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
                  retryBusy={generationActionBusy || Boolean(globalActiveGeneration && !ownsConversationGeneration(globalActiveGeneration, selectedId))}
                  onRetry={() => retryUserMessage(message.id)}
                  cookingOpen={String(cookingActivityMessageId) === String(message.id)}
                  onOpenCooking={(event) => toggleCookingActivity(message.id, event)}
                  onReviewAction={reviewHostAction}
                  actionOperation={actionOperationByMessageId[String(message.id)] || null}
                  actionBusy={generationActionBusy}
                  onConfirmAction={(proposal, settings) =>
                    confirmHostActionProposal(message, proposal, settings)}
                  onStopAction={stopGeneration}
                  onOpenDetails={(details) => {
                    setCookingActivityMessageId(null);
                    if (agentActivity) setDismissedAgentTask(agentActivityKey);
                    setResponseDetails(details);
                  }}
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



                    {!agentRunning ? <LiveResponse key={activeGeneration?.id} operation={activeGeneration} /> : null}
                    <ResearchProgress
                      details={
                        activeGeneration?.result || activeGeneration?.details
                      }
                    />
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

        {globalActiveGeneration && !ownsConversationGeneration(globalActiveGeneration, selectedId) ? <div className="background-generation-note"><span>A response is running in another conversation.</span><button type="button" onClick={() => {
          const owner = globalActiveGeneration.result?.conversation_id || globalActiveGeneration.progress?.conversation_id || globalActiveGeneration.target_id;
          const item = allConversations.find((row) => String(row.id) === String(owner));
          if (!item) return;
          const mode = item.workspace_mode || "chat";
          if (mode === workspaceMode) selectConversation(item.id);
          else { sessionFor(mode).selectedId = item.id; setWorkspaceMode(mode); }
        }}>Go to conversation</button></div> : null}
        <form className="composer" onSubmit={submit}>
          <div className="composer__surface">
            <label>
            <span className="sr-only">Message Salty Steak</span>
            <textarea
              ref={textareaRef}
              rows="1"
              value={draft}
              placeholder={modeDetails.placeholder}
              onChange={(event) => {
                setDraft(event.target.value);
                setSlashCommandsDismissed(false);
              }}
              onCut={() => window.requestAnimationFrame(resizeComposer)}
              onPaste={() => window.requestAnimationFrame(resizeComposer)}
              onKeyDown={(event) => {
                if (slashCommands.length && event.key === "ArrowDown") {
                  event.preventDefault();
                  setSlashCommandIndex((current) => (current + 1) % slashCommands.length);
                  return;
                }
                if (slashCommands.length && event.key === "ArrowUp") {
                  event.preventDefault();
                  setSlashCommandIndex((current) =>
                    (current - 1 + slashCommands.length) % slashCommands.length);
                  return;
                }
                if (slashCommands.length && event.key === "Escape") {
                  event.preventDefault();
                  setSlashCommandsDismissed(true);
                  return;
                }
                if (
                  slashCommands.length
                  && event.key === "Enter"
                  && !event.shiftKey
                  && draft.trim().toLocaleLowerCase()
                    !== slashCommands[boundedSlashCommandIndex]?.command.toLocaleLowerCase()
                ) {
                  event.preventDefault();
                  chooseSlashCommand(slashCommands[boundedSlashCommandIndex]);
                  return;
                }
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            </label>
            {slashCommands.length ? (
              <div
                className="slash-command-menu"
                role="listbox"
                aria-label="Slash commands"
              >
                <span className="slash-command-menu__label">Commands</span>
                {slashCommands.map((command, index) => (
                  <button
                    key={command.command}
                    type="button"
                    role="option"
                    aria-selected={index === boundedSlashCommandIndex}
                    className={index === boundedSlashCommandIndex ? "is-selected" : ""}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => chooseSlashCommand(command)}
                  >
                    <code>{command.command}</code>
                    <span>{command.description}</span>
                  </button>
                ))}
              </div>
            ) : null}
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
                  onChange={attachFiles}
                />
                <div className="composer-control composer-control--add">
                  <button
                    ref={(element) => composerMenuTriggerRefs.current.set("attachments", element)}
                    type="button"
                    className="composer-tool composer-tool--icon"
                    aria-label="Add files"
                    title="Add images, documents or code"
                    onClick={() => attachmentInputRef.current?.click()}
                  >
                    <Plus aria-hidden="true" />
                  </button>
                </div>
                <div className="composer-control composer-control--research">
                  <button
                    type="button"
                    className={`composer-tool composer-tool--research ${
                      researchMode ? "composer-tool--research-on" : ""
                    }`}
                    aria-pressed={researchMode}
                    aria-label="Web research mode"
                    title={
                      researchMode
                        ? cookingMode === "cooking"
                          ? "Cooking Research: iterative validation in the background, with a hard four-hour ceiling"
                          : "Instant Research: focused multi-source research for up to five minutes"
                        : "Research depth is off; Salty Steak may still run a bounded verification when a current claim needs it"
                    }
                    onClick={() => setResearchMode((current) => !current)}
                  >
                    <Globe aria-hidden="true" />
                    <span>Research</span>
                  </button>
                </div>
                <div className="composer-control composer-control--authority">
                  <button
                    ref={(element) => composerMenuTriggerRefs.current.set("authority", element)}
                    type="button"
                    className={`composer-tool composer-tool--authority ${
                      generationSettings.computer_authority_mode === "full_access"
                        ? "composer-tool--authority-full"
                        : ""
                    }`}
                    aria-label={`Computer authority: ${computerAuthorityLabel(
                      generationSettings.computer_authority_mode,
                    )}`}
                    aria-haspopup="menu"
                    aria-expanded={composerMenu === "authority"}
                    aria-controls="composer-authority-menu"
                    aria-describedby="permissions-tooltip"
                    onClick={(event) => openComposerMenu("authority", event)}
                  >
                    {generationSettings.computer_authority_mode === "full_access"
                      ? <ShieldCheck aria-hidden="true" />
                      : <ShieldQuestion aria-hidden="true" />}
                    <span>{computerAuthorityLabel(generationSettings.computer_authority_mode)}</span>
                    <ChevronDown aria-hidden="true" />
                  </button>
                  <span id="permissions-tooltip" role="tooltip" className="composer-control-tooltip">Change permissions</span>
                  {composerMenu === "authority" ? (
                    <ComposerPopover
                      id="composer-authority-menu" autoFocus={composerMenuKeyboardRef.current}
                      label="Permissions"
                      onClose={closeComposerMenu}
                    >
                      {COMPUTER_AUTHORITY_MODES.map((mode) => (
                        <ComposerMenuItem
                          key={mode.id}
                          icon={mode.id === generationSettings.computer_authority_mode
                            ? Check
                            : mode.id === "full_access" ? ShieldCheck : ShieldQuestion}
                          label={mode.label}
                          description={mode.id === "full_access" ? "Run actions without asking" : "Ask before each action"}
                          selected={mode.id === generationSettings.computer_authority_mode}
                          radio
                          onSelect={() => {
                            closeComposerMenu();
                            void chooseAuthorityMode(mode.id);
                          }}
                        />
                      ))}
                    </ComposerPopover>
                  ) : null}
                </div>
                <div className="composer-control">
                  <button
                    ref={(element) => composerMenuTriggerRefs.current.set("plugins", element)}
                    type="button"
                    className="composer-tool"
                    aria-label="Tools and connected apps"
                    aria-haspopup="menu"
                    aria-expanded={composerMenu === "plugins"}
                    aria-controls="composer-plugins-menu"
                    onClick={(event) => openComposerMenu("plugins", event)}
                  >
                    <Plug aria-hidden="true" />
                    <span>Tools</span>
                  </button>
                  {composerMenu === "plugins" ? (
                    <ComposerPopover
                      id="composer-plugins-menu" autoFocus={composerMenuKeyboardRef.current}
                      label="Tools and connected apps"
                      onClose={closeComposerMenu}
                    >
                      <div className="plugin-picker">
                        {pickerSections.map((section) => (
                          <div className="plugin-picker__section" key={section.id}>
                            <p className="plugin-picker__section-title">
                              {section.id === "connected_apps" ? "Connections" : section.title}
                            </p>
                            {section.rows.map((row) => {
                              const RowIcon =
                                row.kind === "connected_app"
                                  ? connectedAppIcon(row)
                                  : pluginIcon(row);
                              return (
                                <button
                                  key={`${section.id}:${row.id}`}
                                  type="button"
                                  role="menuitem"
                                  className="plugin-picker__row"
                                  disabled={Boolean(row.disabled)}
                                  title={row.detail}
                                  onClick={() => {
                                    closeComposerMenu();
                                    if (row.kind === "connected_app") {
                                      setPluginSetup({
                                        connectorId: row.id,
                                        mode: row.action,
                                      });
                                      return;
                                    }
                                    if (row.kind === "memory") {
                                      openSettings("memory");
                                      return;
                                    }
                                    openSettings("plugins");
                                  }}
                                >
                                  <span className="plugin-picker__icon" aria-hidden="true">
                                    <RowIcon />
                                  </span>
                                  <span className="plugin-picker__text">
                                    <span className="plugin-picker__name">{row.id === "mcp" ? "MCP server" : row.name}</span>
                                  </span>
                                  <span
                                    className={`plugin-picker__state${
                                      row.attention ? " plugin-picker__state--attention" : ""
                                    }${
                                      row.state === "Connected"
                                        ? " plugin-picker__state--connected"
                                        : ""
                                    }`}
                                  >
                                    {row.state}
                                  </span>
                                </button>
                              );
                            })}
                          </div>
                        ))}
                        {!pickerSections.length ? (
                          <p className="plugin-picker__empty">
                            No tools or connected apps are registered in this build.
                          </p>
                        ) : null}
                      </div>
                      <button
                        type="button"
                        role="menuitem"
                        className="plugin-picker__manage"
                        onClick={() => {
                          closeComposerMenu();
                          openSettings("plugins");
                        }}
                      >
                        <span>Connection settings</span><ChevronRight aria-hidden="true" />
                      </button>
                    </ComposerPopover>
                  ) : null}
                </div>
                <div className="composer-control composer-control--model">
                  <button
                    ref={(element) => composerMenuTriggerRefs.current.set("model", element)}
                    type="button"
                    className="composer-selector"
                    aria-haspopup="dialog"
                    aria-expanded={inspectorOpen && settingsView === "response"}
                    aria-label={`Model: ${activeModelLabel}`}
                    onClick={() => openSettings("response")}
                  >
                    <Bot aria-hidden="true" />
                    <span>{activeModelLabel}</span>
                    <ChevronDown aria-hidden="true" />
                  </button>
                </div>
                {workspaceMode === "chat" && chatStatus?.image_generation?.available ? (
                  <div className="composer-control composer-control--image">
                    <button
                      type="button"
                      className="composer-selector composer-selector--image"
                      aria-haspopup="dialog"
                      aria-expanded={inspectorOpen && settingsView === "image"}
                      aria-label={`Image settings: ${selectedImageCanvas.width} by ${selectedImageCanvas.height}, ${generationSettings.image_steps} steps`}
                      title="Image model, aspect ratio, resolution, and quality"
                      onClick={() => openSettings("image")}
                    >
                      <Image aria-hidden="true" />
                      <span>{selectedImageCanvas.width}×{selectedImageCanvas.height}</span>
                      <ChevronDown aria-hidden="true" />
                    </button>
                  </div>
                ) : null}
                <div className="composer-control composer-control--cooking">
                  <button
                    ref={(element) => composerMenuTriggerRefs.current.set("cooking", element)}
                    type="button"
                    className="composer-selector composer-selector--cooking"
                    aria-haspopup="menu"
                    aria-expanded={composerMenu === "cooking"}
                    aria-controls="composer-cooking-menu"
                    aria-label={`Cooking mode: ${cookingModeLabel(cookingMode)}`}
                    title={chatStatus?.runtime_controls?.reasoning_control?.reason}
                    onClick={(event) => openComposerMenu("cooking", event)}
                  >
                    <ChefHat aria-hidden="true" />
                    <span>{cookingModeLabel(cookingMode)}</span>
                    <ChevronDown aria-hidden="true" />
                  </button>
                  {composerMenu === "cooking" ? (
                    <ComposerPopover id="composer-cooking-menu" autoFocus={composerMenuKeyboardRef.current} label="Response mode" onClose={closeComposerMenu} align="right">
                      {COOKING_MODES.map((mode) => (
                        <button
                          key={mode.id}
                          type="button"
                          role="menuitemradio"
                          aria-checked={mode.id === cookingMode}
                          className="response-mode-option"
                          title={mode.description}
                          onClick={() => {
                            setGenerationSettings((current) => ({ ...current, reasoning_mode: mode.id }));
                            closeComposerMenu();
                          }}
                        >
                          {mode.id === "instant" ? <Zap aria-hidden="true" /> : <ChefHat aria-hidden="true" />}
                          <span><strong>{mode.label}</strong><small>{mode.id === "instant" ? "Quick replies" : "More time to reason"}</small></span>
                          {mode.id === cookingMode ? <Check className="response-mode-option__check" aria-hidden="true" /> : <span />}
                        </button>
                      ))}
                    </ComposerPopover>
                  ) : null}
                </div>
              </div>
              <div className="composer__actions">
            {selectedConversationBusy ? (
              <button
                ref={composerActionRef}
                type="button"
                className="composer-action composer-action--stop"
                disabled={generationActionBusy}
                aria-label={selectedImageGenerating ? "Stop image generation" : "Stop response"}
                title={selectedImageGenerating ? "Stop image generation" : "Stop response"}
                onClick={stopGeneration}
              >
                <Square aria-hidden="true" />
              </button>
            ) : (
              <button
                ref={composerActionRef}
                type="submit"
                className="send-button"
                disabled={
                  !draft.trim() || !["ready", "preparing"].includes(readiness.key)
                  || sending || Boolean(globalActiveGeneration)
                }
                aria-label="Send message"
              >
                <Send aria-hidden="true" />
              </button>
            )}
              </div>
            </div>
          </div>
          {readiness.key !== "no_version" ? (
            <p className="composer__hint">
              {selectedConversationBusy
                ? activeGeneration?.phase ||
                  (selectedImageGenerating ? "Generating image" : "Generating response")
                : readiness.key === "preparing"
                ? "Your message will wait until the local model is ready."
                : readiness.key === "unavailable"
                ? `${chatStatus?.selected_model_label || "The selected model"} is registered, but native activation has not passed.`
                : "Enter to send · Shift+Enter for a new line"}
            </p>
          ) : (
            <p className="composer__hint">Drafting is available. Select a saved version in Training to send.</p>
          )}
        </form>
      </section>
      )}

      {activityWorkspaceOpen && !showAbout ? (
        <>
          <button
            type="button"
            className="cooking-activity-backdrop"
            aria-label="Close activity"
            onClick={showAgentActivity
              ? () => setDismissedAgentTask(agentActivityKey)
              : closeCookingActivity}
          />
          {showAgentActivity ? (
            <AgentActivityPanel
              task={agentActivity}
              running={agentRunning}
              onStop={stopGeneration}
              onClose={() => setDismissedAgentTask(agentActivityKey)}
            />
          ) : (
            <CookingActivityPanel
              message={cookingActivityMessage}
              active={cookingActivityMessageId === "active"}
              mode={cookingActivityMessage
                ? messageReasoningMode(cookingActivityMessage)
                : activeReasoningMode}
              operation={activeGeneration}
              onClose={closeCookingActivity}
              onStop={stopGeneration}
              stopBusy={generationActionBusy}
            />
          )}
        </>
      ) : null}

      {responseDetailsFor && !showAbout ? (
        <>


          <button
            type="button"
            className="response-details-scrim"
            aria-label="Close response details"
            onClick={() => setResponseDetails(null)}
          />
          <ResponseDetails
            details={responseDetailsFor}
            onClose={() => setResponseDetails(null)}
            onOpenExternal={openExternalLink}
          />
        </>
      ) : null}

      {inspectorOpen && !showAbout ? (
        <div
          className="chat-settings-layer"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setInspectorOpen(false);
          }}
        >
          <WorkspaceSettings section={settingsView} onSectionChange={setSettingsView} onClose={() => setInspectorOpen(false)} active={!pluginSetup}>
          {settingsView === "general" ? <GeneralSettings onSectionChange={setSettingsView} onNavigate={(destination) => { setInspectorOpen(false); onNavigate(destination); }} />
          : settingsView === "companion" ? <CompanionSettings />
          : settingsView === "about" ? <AboutPage onBack={() => setSettingsView("general")} />
          : settingsView === "plugins" ? (
            <PluginsSettingsSheet embedded active={false} title="Connections" onClose={() => setInspectorOpen(false)}>
              <PluginsPanel
                connections={pluginConnections}
                automationAdapter={AUTOMATION_ADAPTER}
                proposedInvocation={selectedActionProposal}
                onAnalyzeCapture={async (captureResult) => {
                  if (!chatStatus?.vision?.application_available) {
                    notify({ message: chatStatus?.vision?.reason || "Image analysis is unavailable.", kind: "error" });
                    return;
                  }
                  const prompt = draft.trim();
                  if (!prompt) {
                    notify({ message: "Write what you want Base Steak 2.0 to analyze in this capture.", kind: "error" });
                    return;
                  }
                  if (sending || globalActiveGeneration) return;
                  const captureOwner = selectedIdRef.current;
                  let conversationId = captureOwner;
                  const taskId = ++generationTaskRef.current;
                  setSending(true);
                  try {
                    const staged = await api.stageScreenCaptureForVision(captureResult.audit_record_id);
                    if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
                    setInspectorOpen(false);
                    if (String(selectedIdRef.current || "") === String(captureOwner || "") && draftSnapshot.current.draft === prompt) setDraft("");
                    if (!conversationId) {
                      const createdPayload = await api.createConversation(workspaceMode);
                      const created = createdPayload?.conversation || createdPayload;
                      conversationId = created.id;
                      locallyCreatedConversationIdsRef.current.add(String(created.id));
                      if (String(selectedIdRef.current || "") === String(captureOwner || "")) {
                        selectConversation(created.id);
                        setConversation({ ...created, messages: created.messages || [] });
                      }
                      setResources((previous) => ({
                        ...previous,
                        conversations: [
                          created,
                          ...previous.conversations.filter(
                            (item) => String(item.id) !== String(created.id),
                          ),
                        ],
                      }));
                    }
                    let operation = await api.analyzeVisionInput(
                      conversationId,
                      prompt,
                      staged.vision_input_token,
                      api.makeRequestKey(),
                      128,
                      generationTurnSettingsForRequest(generationSettings, DEFAULT_GENERATION_SETTINGS, { agentMode, codeMode: workspaceMode === "code", workspaceMode, researchMode }),
                    );
                    applyOperations([operation], { announceTransitions: false });
                    activeGenerationRef.current = operation;
                    setActiveGeneration(operation);
                    while (!generationTerminal(operation) && ownsGenerationTask(generationTaskRef.current, taskId)) {
                      await waitFor(GENERATION_PREVIEW_POLL_MS);
                      operation = await api.getOperation(operation.id);
                      activeGenerationRef.current = operation;
                      setActiveGeneration(operation);
                    }
                    if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
                    applyOperations([operation]);
                    const refreshed = await api.getConversation(conversationId);
                    if (String(selectedIdRef.current) === String(conversationId)) setConversation(refreshed?.conversation || refreshed);
                    if (operation.state === "failed") {
                      restoreFailedDraft(conversationId, prompt);
                      notify({
                        message: operation.error?.message || "Salty Steak could not analyze this capture.",
                        kind: "error",
                      });
                    }
                    await Promise.all([
                      refreshDomain("conversations", { quiet: true }),
                      refreshDomain("chat", { quiet: true }),
                    ]);
                  } catch (error) {
                    if (!ownsGenerationTask(generationTaskRef.current, taskId)) return;
                    restoreFailedDraft(conversationId, prompt);
                    notify({ message: errorMessage(error), kind: "error" });
                  } finally {
                    if (ownsGenerationTask(generationTaskRef.current, taskId)) {
                      activeGenerationRef.current = null;
                      setActiveGeneration(null);
                      setSending(false);
                    }
                  }
                }}
                loading={!pluginState && !pluginLoadError}
                error={pluginLoadError}
                onRetry={refreshPlugins}
                busyId={pluginBusyId}
                onConnect={(connectorId) => {
                  setPluginError("");
                  setPluginSetup({ connectorId, mode: "connect" });
                }}
                onManage={(connectorId) => {
                  setPluginError("");
                  setPluginSetup({ connectorId, mode: "manage" });
                }}
                onAddMcpServer={() => {
                  setPluginError("");
                  setPluginSetup({ connectorId: "mcp", mode: "connect" });
                }}
              />
            </PluginsSettingsSheet>
          ) : settingsView === "memory" ? (
            <PluginsSettingsSheet embedded active={false}
              title="Memory"
              subtitle="Only context you explicitly saved"
              closeLabel="Close memory"
              onClose={() => setInspectorOpen(false)}
            >
              <MemoryPanel />
            </PluginsSettingsSheet>
          ) : (
            <ResponseSettingsSheet embedded key={settingsView}
              title={
                settingsView === "image"
                  ? "Image generation"
                  : modelActivationBusy
                    ? "Loading model..."
                    : "Model & response"
              }
              models={modelOptions}
              imageModels={chatStatus?.image_generation?.models || []}
              selectedModelId={activeVersionId ? String(activeVersionId) : ""}
              modelLabel={activeModelLabel}
              modelDetail={readiness.key === "ready" ? "Loaded for this conversation" : readiness.label}
              modelReady={readiness.key === "ready"}
              modelStatusLabel={modelActivationBusy ? "Loading" : undefined}
              settings={generationSettings}
              onSettingsChange={setGenerationSettings}
              onModelChange={selectConversationModel}
              onOpenModels={() => {
                setInspectorOpen(false);
                onNavigate("versions");
              }}
              onReset={() => setGenerationSettings(normaliseGenerationSettingsSnapshot({
                ...(chatStatus?.generation_defaults || DEFAULT_GENERATION_SETTINGS),
                web_search_enabled: generationSettings.web_search_enabled,
              }, DEFAULT_GENERATION_SETTINGS))}
              onClose={() => setInspectorOpen(false)}
              initialSection={settingsView}
            />
          )}
          </WorkspaceSettings>
        </div>
      ) : null}

      {pluginSetup && !showAbout ? (
        <PluginConnectionDialog
          connectorId={pluginSetup.connectorId}
          connector={pluginConnections[pluginSetup.connectorId] || { status: "disconnected" }}
          busy={pluginBusyId === pluginSetup.connectorId}
          error={pluginError}
          onConnect={connectPlugin}
          onRecheck={recheckPlugin}
          onDisconnect={disconnectPlugin}
          onClose={() => {
            if (!pluginBusyId) {
              setPluginError("");
              setPluginSetup(null);
            }
          }}
        />
      ) : null}

      <Dialog
        open={managementMode === "rename"}
        title="Rename conversation"
        onClose={closeManagement}
      >
        <form className="stack-form" onSubmit={renameConversation}>
          <Field label="Title" error={managementError}>
            <input
              autoFocus
              type="text"
              maxLength="80"
              value={renameTitle}
              onChange={(event) => {
                setRenameTitle(event.target.value);
                setManagementError("");
              }}
            />
          </Field>
          <FormActions>
            <Button disabled={managementBusy} onClick={closeManagement}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              busy={managementBusy}
              disabled={!validConversationTitle(renameTitle)}
            >
              Save
            </Button>
          </FormActions>
        </form>
      </Dialog>

      <Dialog
        open={managementMode === "delete"}
        title="Delete conversation"
        description={managedConversation?.title || ""}
        onClose={closeManagement}
      >
        <InlineNotice kind="warning" title="Permanent deletion">
          This conversation and every message in it will be removed. It cannot be recovered.
        </InlineNotice>
        {managementError ? (
          <InlineNotice kind="error" title="Conversation not deleted">
            {managementError}
          </InlineNotice>
        ) : null}
        <FormActions>
          <Button disabled={managementBusy} onClick={closeManagement}>
            Cancel
          </Button>
          <Button
            variant="danger"
            icon={Trash2}
            busy={managementBusy}
            onClick={deleteConversation}
          >
            Delete
          </Button>
        </FormActions>
      </Dialog>
      <Dialog
        open={Boolean(fullAccessRequest)}
        title="Turn on full access"
        description="Salty Steak will be able to use your computer without asking each time."
        onClose={() => setFullAccessRequest(null)}
      >
        <InlineNotice kind="warning" title="This enables real capabilities">
          Full access is not only about confirmation. Enabling it grants the
          capabilities below, and they stay granted until you revoke them in
          Computer control. Every use is recorded in the audit log. Actions that
          need administrator rights may still show the secure Windows UAC prompt;
          Salty Steak does not bypass that operating-system boundary.
        </InlineNotice>
        <ul className="capability-consent-list">
          {(fullAccessRequest?.capabilities || []).map((capability) => (
            <li key={capability.id}>
              <strong>{capability.name}</strong>
              <span>{capability.description}</span>
              {capability.alreadyGranted ? <em>Already enabled</em> : null}
            </li>
          ))}
        </ul>
        <FormActions>
          <Button
            disabled={grantingFullAccess}
            onClick={() => setFullAccessRequest(null)}
          >
            Cancel
          </Button>
          <Button
            variant="primary"
            icon={ShieldCheck}
            busy={grantingFullAccess}
            onClick={confirmFullAccess}
          >
            {`Enable ${fullAccessRequest?.missing?.length || 0} and turn on full access`}
          </Button>
        </FormActions>
      </Dialog>
    </div>
  );
}

function ComposerPopover({ id, label, align = "left", autoFocus = false, children }) {
  const menuRef = useRef(null);

  useEffect(() => {
    if (!autoFocus) return;
    const firstItem = menuRef.current?.querySelector(
      '[role="menuitem"]:not(:disabled),[role="menuitemradio"]:not(:disabled)',
    );
    window.requestAnimationFrame(() => firstItem?.focus());
  }, []);

  function moveFocus(event) {
    const direction = ({
      ArrowDown: "next",
      ArrowUp: "previous",
      Home: "first",
      End: "last",
    })[event.key];
    if (!direction) return;
    const items = Array.from(menuRef.current?.querySelectorAll(
      '[role="menuitem"],[role="menuitemradio"]',
    ) || []);
    const enabledItems = items.map((item) => !item.disabled);
    const currentIndex = items.indexOf(document.activeElement);
    const nextIndex = nextEnabledMenuIndex(currentIndex, direction, enabledItems);
    if (nextIndex < 0) return;
    event.preventDefault();
    items[nextIndex]?.focus();
  }

  return (
    <div
      ref={menuRef}
      id={id}
      className={`composer-popover composer-popover--${align}`}
      role="menu"
      aria-label={label}
      onKeyDown={moveFocus}
    >
      <div className="composer-popover__title" aria-hidden="true">{label}</div>
      {children}
    </div>
  );
}

function ComposerMenuItem({
  icon: Icon,
  label,
  description,
  status = "",
  selected = false,
  radio = false,
  disabled = false,
  onSelect,
}) {
  return (
    <button
      type="button"
      className={`composer-menu-item ${selected ? "is-selected" : ""}`}
      role={radio ? "menuitemradio" : "menuitem"}
      aria-checked={radio ? selected : undefined}
      disabled={disabled}
      onClick={onSelect}
    >
      <span className="composer-menu-item__icon"><Icon aria-hidden="true" /></span>
      <span className="composer-menu-item__copy">
        <strong>{label}</strong>
        <small>{description}</small>
      </span>
      {status ? <span className="composer-menu-item__status">{status}</span> : null}
    </button>
  );
}

function ReadinessState({ readiness, chatStatus }) {
  return (
    <div className={`readiness readiness--${readiness.key}`}>
      <Status value={readiness.statusValue} label={readiness.label} />
      {chatStatus?.active_version_label ? <span>{chatStatus.active_version_label}</span> : null}
    </div>
  );
}

function PluginsSettingsSheet({
  children,
  onClose,
  active = true,
  title = "Plugins",
  subtitle = "Connections and permissions",
  closeLabel = "Close plugins",
  embedded = false,
}) {
  const sheetRef = useModalFocusTrap({ active, onClose });
  return (
    <aside ref={sheetRef} className={`plugins-settings-sheet ${embedded ? "settings-embedded" : ""}`} role={embedded ? undefined : "dialog"} aria-modal={embedded ? undefined : "true"} aria-labelledby="plugins-settings-title" tabIndex="-1">
      <header className="plugins-settings-sheet__chrome">
        <div>
          <strong id="plugins-settings-title">{title}</strong>
          <span>{subtitle}</span>
        </div>
        <button type="button" aria-label={closeLabel} onClick={onClose}>
          <X aria-hidden="true" />
        </button>
      </header>
      <div className="plugins-settings-sheet__body">{children}</div>
    </aside>
  );
}

function activeCookingLabel(phase, mode = "instant") {
  if (mode !== "cooking") return "Responding";
  const value = String(phase || "").toLowerCase();
  if (value.includes("queue") || value.includes("wait")) return "Waiting to respond";
  if (value.includes("search")) return "Searching sources";
  if (value.includes("load") || value.includes("runtime")) return "Preparing runtime";
  if (value.includes("prepar") || value.includes("prefill") || value.includes("prompt")) return "Preparing response";
  if (value.includes("sav") || value.includes("final")) return "Finishing response";
  if (value.includes("generat") || value.includes("cook")) return "Cooking";
  if (value.includes("stop") || value.includes("cancel")) return "Stopping response";
  return "Cooking";
}

function operationReasoningMode(operation, fallback = "instant") {
  const details = operation?.result || operation?.details || {};
  const value = String(
    details.reasoning_mode_effective ?? details.reasoning_mode ?? fallback,
  ).trim().toLowerCase();
  return value === "cooking" ? "cooking" : "instant";
}

function messageReasoningMode(message) {
  const details = message?.technical_details || message?.details || {};
  const value = String(
    details.reasoning_mode_effective ?? details.reasoning_mode ?? "",
  ).trim().toLowerCase();
  return value === "cooking" ? "cooking" : "instant";
}

function generationTerminal(operation) {
  return ["completed", "failed", "interrupted", "cancelled"].includes(
    String(operation?.state || "").toLowerCase(),
  );
}

function waitFor(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function awaitTerminalOperation(operation, onUpdate, stillWatching = () => true) {
  let current = operation;
  while (!generationTerminal(current) && stillWatching()) {
    await waitFor(GENERATION_PREVIEW_POLL_MS);
    if (!stillWatching()) break;
    current = await api.getOperation(current.id);
    onUpdate?.(current);
  }
  return current;
}

function getReadiness(status) {
  const explicit = normaliseToken(status?.readiness || status?.state);
  const activeId = status?.active_saved_version_id || status?.saved_version_id;
  const selectedModelId = status?.selected_model_id;
  const selectedTargetId = activeId || selectedModelId;
  const runtimeReady =
    status?.runtime_ready === true ||
    explicit === "ready" ||
    normaliseToken(status?.runtime_state) === "ready";

  if (selectedTargetId && runtimeReady) {
    return {
      key: "ready",
      label: "Ready",
      statusValue: "ready",
      blockedMessage: "",
    };
  }
  if (
    selectedTargetId &&
    [
      "preparing",
      "loading",
      "queued",
      "running",
      "preparing_salty_potato",
      "native_runtime_cold",
      "ready_cold_load",
      "",
    ].includes(
      explicit,
    )
  ) {
    return {
      key: "preparing",
      label: "Preparing Salty Steak",
      statusValue: "preparing_salty_potato",
      blockedMessage: "",
    };
  }
  if (!selectedTargetId && ["", "no_saved_version_selected", "no_version"].includes(explicit)) {
    return {
      key: "no_version",
      label: "No saved version selected",
      statusValue: "not_selected",
      blockedMessage: "Choose a saved version before sending a message.",
    };
  }
  if (selectedModelId && explicit === "engine_build_required") {
    return {
      key: "unavailable",
      label: "Native engine setup required",
      statusValue: "warning",
      blockedMessage:
        status?.message || "The selected base model is registered but its native engine has not passed activation.",
    };
  }
  return {
    key: "unavailable",
    label: "Unavailable",
    statusValue: "unavailable",
    blockedMessage: status?.message || "Salty Steak is currently unavailable.",
  };
}
