import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { AppShell } from "./components/AppShell.jsx";
import { ChatPage } from "./pages/ChatPage.jsx";
import { SystemPage } from "./pages/SystemPage.jsx";
import { TrainPageR11 } from "./pages/TrainPageR11.jsx";
import { VersionsPageR11 } from "./pages/VersionsPageR11.jsx";
import {
  DEFAULT_TRAINING_PAGE,
  hashForPage,
  isTrainingPage,
  pageFromHash,
  validTrainingPage,
} from "./workflows/navigation.mjs";

const DataPage = lazy(() =>
  import("./pages/DataPage.jsx").then((module) => ({ default: module.DataPage })),
);
const EvaluatePage = lazy(() =>
  import("./pages/EvaluatePage.jsx").then((module) => ({ default: module.EvaluatePage })),
);
const ProjectPage = lazy(() =>
  import("./pages/ProjectPage.jsx").then((module) => ({ default: module.ProjectPage })),
);
const LAST_TRAINING_PAGE_KEY = "salty-potato:last-training-page";

function readLastTrainingPage() {
  try {
    return validTrainingPage(window.localStorage.getItem(LAST_TRAINING_PAGE_KEY));
  } catch {
    return DEFAULT_TRAINING_PAGE;
  }
}

export default function App() {
  const [route, setRoute] = useState(() => window.location.hash || "#chat");
  const page = pageFromHash(route);
  const routeQuery = new URLSearchParams(String(route).split("?")[1] || "");
  const aboutFrom = routeQuery.get("from");
  const aboutOrigin = isTrainingPage(aboutFrom) ? aboutFrom : "chat";
  const lastTrainingPageRef = useRef(
    isTrainingPage(page) ? page : readLastTrainingPage(),
  );

  useEffect(() => {
    if (!window.location.hash) {
      window.history.replaceState(null, "", "#chat");
    }
    const update = () => setRoute(window.location.hash || "#chat");
    window.addEventListener("hashchange", update);
    window.addEventListener("popstate", update);
    return () => {
      window.removeEventListener("hashchange", update);
      window.removeEventListener("popstate", update);
    };
  }, []);

  useEffect(() => {
    if (!isTrainingPage(page)) return;
    lastTrainingPageRef.current = page;
    try {
      window.localStorage.setItem(LAST_TRAINING_PAGE_KEY, page);
    } catch {

    }
  }, [page]);

  const navigate = useCallback((nextPage, parameters = {}) => {
    const targetPage =
      nextPage === "training-center" ? lastTrainingPageRef.current : nextPage;
    const nextHash = hashForPage(targetPage, parameters);
    if (window.location.hash === nextHash) {
      setRoute(nextHash);
      return;
    }
    window.location.hash = nextHash;
  }, []);

  let content;
  switch (page) {
    case "about":
      content = aboutOrigin === "chat"
        ? (
          <ChatPage
            onNavigate={navigate}
            showAbout
            onCloseAbout={() => navigate("chat")}
          />
        )
        : <SystemPage />;
      break;
    case "data":
      content = <DataPage />;
      break;
    case "train":
      content = <TrainPageR11 onNavigate={navigate} />;
      break;
    case "evaluate":
      content = <EvaluatePage onNavigate={navigate} />;
      break;
    case "versions":
      content = <VersionsPageR11 onNavigate={navigate} />;
      break;
    case "system":
      content = <SystemPage />;
      break;
    case "project":
      content = <ProjectPage />;
      break;
    case "chat":
    default:
      content = <ChatPage onNavigate={navigate} />;
      break;
  }

  return (
    <AppShell
      page={page}
      aboutFrom={aboutOrigin}
      onNavigate={navigate}
    >
      <Suspense
        fallback={
          <div className="route-loading" role="status">
            Opening view
          </div>
        }
      >
        <div key={page === "chat" || (page === "about" && aboutOrigin === "chat") ? "chat-workspace" : page}>
          {content}
        </div>
      </Suspense>
    </AppShell>
  );
}
