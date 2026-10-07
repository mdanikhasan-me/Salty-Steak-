import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import { FRONTEND_BUILD_ID, verifyBuildIdentity } from "./api/client.js";
import { AppStateProvider } from "./state/AppState.jsx";
import "./styles/global.css";
import "./styles/approved-workspace.css";
import "./styles/workspace-polish.css";
import "./styles/charming-palette.css";
import "./styles/response-experience.css";
import './styles/work-activity.css';

const root = ReactDOM.createRoot(document.getElementById("root"));

verifyBuildIdentity()
  .then(() => {
    root.render(
      <React.StrictMode>
        <AppStateProvider>
          <App />
        </AppStateProvider>
      </React.StrictMode>,
    );
  })
  .catch((error) => {
    root.render(
      <main className="build-identity-error" role="alert">
        <h1>Salty Steak could not open this interface</h1>
        <p>
          The local interface and backend do not have the same sealed build identity.
          No fallback assets were loaded.
        </p>
        <dl>
          <div>
            <dt>Frontend build</dt>
            <dd>{FRONTEND_BUILD_ID}</dd>
          </div>
          <div>
            <dt>Reason</dt>
            <dd>{error?.message || "Build identity unavailable"}</dd>
          </div>
        </dl>
      </main>,
    );
  });
