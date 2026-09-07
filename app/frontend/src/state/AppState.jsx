import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { api, asList, asOperation } from "../api/client.js";
import {
  domainForOperation,
  isActive,
  isTerminal,
  mergeOperations,
  nextPollDelay,
  notificationForTransition,
  normaliseToken,
} from "../workflows/operations.mjs";
import {
  notificationReducer,
} from "../workflows/notifications.mjs";
import { companionIsWorking } from "../workflows/companionActivity.mjs";

const AppStateContext = createContext(null);

const initialResources = {
  datasets: [],
  versions: [],
  evaluations: [],
  conversations: [],
  chatStatus: null,
  trainingSetup: null,
  trainingStatus: null,
  project: null,
};

function extractOperationList(payload) {
  return asList(payload, ["operations", "active_operations"]);
}

export function AppStateProvider({ children }) {
  const [resources, setResources] = useState(initialResources);
  const [operations, setOperations] = useState({});
  const operationsRef = useRef(operations);
  const [notifications, dispatchNotification] = useReducer(notificationReducer, []);
  const [connection, setConnection] = useState({
    loading: true,
    online: false,
    error: null,
    lastCheckedAt: null,
  });
  const [checkingRequests, setCheckingRequests] = useState({});
  const mountedRef = useRef(true);
  const pollDelayRef = useRef(750);
  const companionWorking = companionIsWorking(operations, connection.online);
  useEffect(() => {
    const bridge=window.chrome?.webview;
    if(!bridge)return undefined;
    const notify=()=>bridge.postMessage({type:"companion_work",active:companionWorking});
    notify();
    if(!companionWorking)return undefined;
    const heartbeat=window.setInterval(notify,1500);
    return ()=>window.clearInterval(heartbeat);
  },[companionWorking]);

  useEffect(() => {
    operationsRef.current = operations;
  }, [operations]);

  const addNotification = useCallback((notification) => {
    if (!notification?.message) return;
    dispatchNotification({ type: "add", notification: { ...notification, id: notification.id || api.makeRequestKey() } });
  }, []);

  const dismissNotification = useCallback((id) => {
    dispatchNotification({ type: "remove", id });
  }, []);

  const pauseNotification = useCallback((id) => {
    dispatchNotification({ type: "pause", id, now: Date.now() });
  }, []);

  const resumeNotification = useCallback((id) => {
    dispatchNotification({ type: "resume", id, now: Date.now() });
  }, []);

  const hasTimedNotifications = notifications.some((notification) => !notification.persistent);
  useEffect(() => {
    if (!hasTimedNotifications) return undefined;
    const timer = window.setInterval(() => {
      dispatchNotification({ type: "expire", now: Date.now() });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [hasTimedNotifications]);

  const reportError = useCallback(
    (error, id = `error:${Date.now()}`) => {
      addNotification({
        id,
        kind: "error",
        message: error?.message || String(error || "Something went wrong."),
        persistent: true,
      });
    },
    [addNotification],
  );

  const applyOperations = useCallback(
    (incoming, { announceTransitions = true } = {}) => {
      const list = (incoming || []).filter((operation) => operation?.id);
      if (!list.length) return;

      setOperations((previous) => {
        if (announceTransitions) {
          for (const operation of list) {
            const notification = notificationForTransition(previous[operation.id], operation);
            if (notification) addNotification(notification);
          }
        }
        return mergeOperations(previous, list);
      });
    },
    [addNotification],
  );

  const refreshDomain = useCallback(async (domain, { quiet = false } = {}) => {
    const loaders = {
      datasets: async () => ({ datasets: asList(await api.listDatasets(), ["datasets"]) }),
      versions: async () => ({ versions: asList(await api.listVersions(), ["versions"]) }),
      evaluations: async () => ({
        evaluations: asList(await api.listEvaluations(), ["evaluations"]),
      }),
      conversations: async () => ({
        conversations: asList(await api.listConversations(), ["conversations"]),
      }),
      chat: async () => {
        const [status, conversationsPayload] = await Promise.all([
          api.getChatStatus(),
          api.listConversations(),
        ]);
        return {
          chatStatus: status,
          conversations: asList(conversationsPayload, ["conversations"]),
        };
      },
      training: async () => {
        const [trainingSetup, trainingStatus] = await Promise.all([
          api.getTrainingSetup(),
          api.getTrainingStatus(),
        ]);
        return { trainingSetup, trainingStatus };
      },
      project: async () => ({ project: await api.getProject() }),
    };
    if (!loaders[domain]) return null;
    try {
      const update = await loaders[domain]();
      if (mountedRef.current) {
        setResources((previous) => ({ ...previous, ...update }));
        setConnection((previous) => ({
          ...previous,
          online: true,
          error: null,
          lastCheckedAt: Date.now(),
        }));
      }
      return update;
    } catch (error) {
      if (!quiet && mountedRef.current) {
        setConnection((previous) => ({
          ...previous,
          online: false,
          error,
          lastCheckedAt: Date.now(),
        }));
      }
      throw error;
    }
  }, []);

  const refreshAll = useCallback(async ({ quiet = false } = {}) => {
    if (!quiet) setConnection((previous) => ({ ...previous, loading: true }));
    const jobs = [
      ["datasets", api.listDatasets(), ["datasets"]],
      ["versions", api.listVersions(), ["versions"]],
      ["evaluations", api.listEvaluations(), ["evaluations"]],
      ["conversations", api.listConversations(), ["conversations"]],
      ["chatStatus", api.getChatStatus()],
      ["trainingSetup", api.getTrainingSetup()],
      ["trainingStatus", api.getTrainingStatus()],
      ["project", api.getProject()],
      ["operations", api.listOperations({ active: true }), ["operations", "active_operations"]],
    ];

    const results = await Promise.allSettled(jobs.map(([, promise]) => promise));
    const update = {};
    let firstError = null;
    results.forEach((result, index) => {
      const [key, , listKeys] = jobs[index];
      if (result.status === "fulfilled") {
        if (key === "operations") {
          applyOperations(extractOperationList(result.value), { announceTransitions: false });
        } else {
          update[key] = listKeys ? asList(result.value, listKeys) : result.value;
        }
      } else if (!firstError) {
        firstError = result.reason;
      }
    });
    if (mountedRef.current) {
      setResources((previous) => ({ ...previous, ...update }));
      setConnection({
        loading: false,
        online: results.some((result) => result.status === "fulfilled"),
        error: firstError,
        lastCheckedAt: Date.now(),
      });
    }
    return update;
  }, [applyOperations]);

  useEffect(() => {
    mountedRef.current = true;
    api.listOperations({ active: true })
      .then((payload) => {
        if (!mountedRef.current) return;
        applyOperations(extractOperationList(payload), {
          announceTransitions: false,
        });
        setConnection({
          loading: false,
          online: true,
          error: null,
          lastCheckedAt: Date.now(),
        });
      })
      .catch((error) => {
        if (!mountedRef.current) return;
        setConnection({
          loading: false,
          online: false,
          error,
          lastCheckedAt: Date.now(),
        });
      });
    return () => {
      mountedRef.current = false;
    };
  }, [applyOperations]);

  const refreshAfterOperation = useCallback(
    async (operation) => {
      const domain = domainForOperation(operation?.type);
      const domains = new Set();
      if (domain) domains.add(domain);
      if (domain === "training") {
        domains.add("versions");
        domains.add("datasets");
      }
      if (domain === "versions") {
        domains.add("chat");
      }
      if (domain === "datasets") domains.add("training");
      await Promise.allSettled(
        [...domains].map((name) => refreshDomain(name, { quiet: true })),
      );
    },
    [refreshDomain],
  );

  const hasActiveOperations = Object.values(operations).some(isActive);
  useEffect(() => {
    let cancelled = false;
    let timer;

    async function poll() {
      const current = operationsRef.current;
      const active = Object.values(current).filter(isActive);
      try {
        let incoming;
        if (active.length) {
          const settled = await Promise.allSettled(
            active.map((operation) => api.getOperation(operation.id)),
          );
          incoming = settled
            .filter((result) => result.status === "fulfilled")
            .map((result) => asOperation(result.value))
            .filter(Boolean);
        } else {
          const payload = await api.listOperations({ active: true });
          incoming = extractOperationList(payload);
        }
        if (!cancelled) {
          applyOperations(incoming);
          const completed = incoming.filter(
            (operation) => isTerminal(operation) && isActive(current[operation.id]),
          );
          await Promise.allSettled(completed.map(refreshAfterOperation));
          setConnection((previous) => ({
            ...previous,
            online: true,
            error: null,
            lastCheckedAt: Date.now(),
          }));
        }
      } catch (error) {
        if (!cancelled) {
          setConnection((previous) => ({
            ...previous,
            online: false,
            error,
            lastCheckedAt: Date.now(),
          }));
        }
      }
      pollDelayRef.current = nextPollDelay(
        pollDelayRef.current,
        operationsRef.current,
        { initial: 750, maximum: 15000 },
      );
      if (!cancelled) timer = window.setTimeout(poll, pollDelayRef.current);
    }

    timer = window.setTimeout(poll, hasActiveOperations ? 200 : 750);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [applyOperations, refreshAfterOperation, hasActiveOperations]);

  const startOperation = useCallback(
    async ({ launch, type, targetId, requestKey: suppliedRequestKey }) => {
      const requestKey = suppliedRequestKey || api.makeRequestKey();
      setCheckingRequests((previous) => ({
        ...previous,
        [requestKey]: { type, targetId, state: "submitting" },
      }));
      try {
        let payload;
        try {
          payload = await launch(requestKey);
        } catch (error) {
          if (!error?.isTimeout) throw error;
          setCheckingRequests((previous) => ({
            ...previous,
            [requestKey]: { type, targetId, state: "checking_status" },
          }));
          payload = await api.reconcileOperation(requestKey);
          if (!payload) {
            throw new Error("The request timed out and no persisted operation was found.");
          }
        }
        const operation = asOperation(payload);
        if (!operation?.id) {
          throw new Error("The local service did not return an operation ID.");
        }
        applyOperations([operation], { announceTransitions: false });
        if (isTerminal(operation)) {
          const notification = notificationForTransition(
            { ...operation, state: "running" },
            operation,
          );
          if (notification) addNotification(notification);
          await refreshAfterOperation(operation);
        }
        return operation;
      } finally {
        setCheckingRequests((previous) => {
          const next = { ...previous };
          delete next[requestKey];
          return next;
        });
      }
    },
    [addNotification, applyOperations, refreshAfterOperation],
  );

  const stopOperation = useCallback(
    async (operationId) => {
      const current = operationsRef.current[operationId];
      if (!current || normaliseToken(current.state) === "stop_requested") return current;
      const requestKey = api.makeRequestKey();
      try {
        let payload;
        try {
          payload = await api.stopOperation(operationId, requestKey);
        } catch (error) {
          if (!error?.isTimeout) throw error;
          payload = await api.getOperation(operationId);
        }
        const operation = asOperation(payload);
        applyOperations([operation]);
        return operation;
      } catch (error) {
        const persisted = await api.getOperation(operationId);
        const operation = asOperation(persisted);
        applyOperations([operation]);
        if (normaliseToken(operation?.state) !== "stop_requested" && !isTerminal(operation)) {
          throw error;
        }
        return operation;
      }
    },
    [applyOperations],
  );

  const value = useMemo(
    () => ({
      ...resources,
      operations,
      notifications,
      connection,
      checkingRequests,
      addNotification,
      dismissNotification,
      pauseNotification,
      resumeNotification,
      reportError,
      refreshAll,
      refreshDomain,
      startOperation,
      stopOperation,
      applyOperations,
      setResources,
    }),
    [
      resources,
      operations,
      notifications,
      connection,
      checkingRequests,
      addNotification,
      dismissNotification,
      pauseNotification,
      resumeNotification,
      reportError,
      refreshAll,
      refreshDomain,
      startOperation,
      stopOperation,
      applyOperations,
    ],
  );

  return <AppStateContext.Provider value={value}>{children}</AppStateContext.Provider>;
}

export function useAppState() {
  const context = useContext(AppStateContext);
  if (!context) throw new Error("useAppState must be used inside AppStateProvider.");
  return context;
}
