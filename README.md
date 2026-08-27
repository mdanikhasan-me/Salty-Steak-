<div align="center">
  <img src="docs/assets/salty-steak-arcade.svg" width="100%" alt="Animated pixel-art Salty Steak banner">

  <br>

  <img alt="Windows desktop" src="https://img.shields.io/badge/Windows-desktop-36A9E1?style=for-the-badge&logo=windows11&logoColor=white">
  <img alt="Python backend" src="https://img.shields.io/badge/Python-backend-FFD166?style=for-the-badge&logo=python&logoColor=17324D">
  <img alt="React 18" src="https://img.shields.io/badge/React-18-61DAFB?style=for-the-badge&logo=react&logoColor=11212B">
  <img alt="Vite 6" src="https://img.shields.io/badge/Vite-6-9B72FF?style=for-the-badge&logo=vite&logoColor=white">
  <img alt=".NET 8" src="https://img.shields.io/badge/.NET-8-7B4DFF?style=for-the-badge&logo=dotnet&logoColor=white">
  <img alt="Source available" src="https://img.shields.io/badge/license-source_available-F06D55?style=for-the-badge">
  <img alt="Model files not included" src="https://img.shields.io/badge/model_files-not_in_repo-20263D?style=for-the-badge">

  <br><br>

  <strong>Base Steak 2.0 by MD Anik Hasan (Sawlper)</strong><br>
  The Windows desktop application, local service, and automation layer I use around my local model.
</div>

### What is in this repository

Salty Steak is a Windows desktop application built around a local text model. The desktop window is native C# and WebView2, the interface is React, and the application service is Python. The model is responsible for deciding whether a request needs a normal answer, research, an image job, one computer action, or a longer plan. The application owns execution, permissions, persistence, and verification.

This public repository contains the software source. It does not contain my model weights, tokenizer, checkpoints, training data, private runtime binaries, chat database, browser profile, generated files, installed build, or credentials. Those stay on my machine. There is no secret download command in the setup section; cloning a repository cannot magically produce a 9B model, however confident the terminal looks.

The model presented by the application is **Base Steak 2.0**, trained by **MD Anik Hasan (Sawlper)**.

### What the application currently contains

Chat keeps separate conversation histories, attachments, folders, search, response timing, source links, and a technical activity view. Conversation context belongs to one chat. A fresh chat does not quietly inherit instructions from another one.

Global memory is separate and explicit. A user can choose what to save, inspect the saved entries, and remove them. Retrieval is relevance-ranked and bounded before it is added to a prompt.

Research runs through a source ledger instead of handing raw search snippets directly to the answer model. Pages are classified, observations retain their URL, contradictory findings can coexist, and final claims can be checked against the evidence that produced them.

Agent mode connects the model to a capability broker. The broker exposes only the capabilities granted for that turn, applies the selected authority level, records each invocation, and returns structured observations. Browser control, files, terminal commands, windows, input, screen capture, and UI Automation are implemented as separate capabilities rather than one unrestricted shell-shaped mystery box.

Image work is routed to a separately provisioned image runtime. The text model writes the brief, image settings remain explicit, and revisions keep their own job state. The same workbench also contains dataset import, preparation, training, evaluation, and model-version screens.

If an action ran but did not produce the requested result, that is not success. If no action ran at all, the assistant does not get to write a victory speech about it.

### How a request is handled

The frontend sends the latest message, conversation identifier, selected model settings, mode, authority, and attachments to the loopback API. The Python service assembles the conversation context and any explicitly retrieved memory, then calls the local text runtime.

The model returns one of the job shapes understood by `app/backend/chat/orchestrator.py`: a response, action, plan, research request, image generation, or image revision. Parsing a shape does not grant it authority. `app/backend/chat/dispatch.py` checks whether that job family is available for the current mode before selecting a runner.

Actions and plan nodes go through the automation broker. Research uses its own bounded loop and evidence ledger. Image work goes through the image orchestrator and persistent job store. Long tasks receive cancellation, elapsed-time limits, stagnation detection, checkpoints, and compacted task state rather than an ever-growing transcript of every tool call.

After execution, verification compares the requested outcome with newly observed state. The result and its evidence are persisted to SQLite before the frontend renders the final answer and activity record.

The short version is this:

```text
React UI
  to local Python API
  to Base Steak routing decision
  to the selected runner
  to verification and SQLite
  back to Chat with the answer and evidence
```

### Repository map

`app/frontend/` contains the React 18 interface and Vite configuration.

`app/backend/api/` contains request routing and response contracts. `app/backend/chat/` contains prompt assembly, orchestration, runners, task state, and goal verification.

`app/backend/automation/` contains the capability broker, grants, policy, filesystem operations, browser and UIA clients, credential references, and native Discord inspection.

`app/backend/research/`, `memory/`, `imaging/`, `training/`, and `runtime/` contain their respective services and runtime adapters.

`app/desktop/native/` is the main Windows Forms and WebView2 host. `app/desktop/browser/` is the isolated browser host. `app/desktop/uia/` is the isolated Windows UI Automation process.

`config/defaults.toml` defines source defaults. `config/local.toml` is ignored and is the place for machine-specific overrides.

`.codex/config.toml` stores the preferred Codex settings for this repository. They are requests, not a way around account or provider limits.

### Requirements

The source checkout is intended for Windows. You need:

- Node.js and npm
- Python compatible with the pinned packages in `requirements.txt`
- .NET 8 SDK with Windows Desktop support
- WebView2 Runtime
- NuGet access while restoring the native projects
- a separately provisioned Salty Steak workspace if you expect model-backed features to run

CUDA, model files, image runtimes, and native inference binaries are not installed by npm or pip. Their exact requirements depend on the runtime bundle being used.

### Set up the source checkout

From PowerShell in the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm ci
```

Check that both source halves compile:

```powershell
npm run build
npm run check:python
```

The frontend build is written to `dist/`. Python compilation checks the modules under `app/` without starting the model runtime.

### Run the development interface

Start the backend in one PowerShell terminal:

```powershell
npm run server
```

Start Vite in another:

```powershell
npm run dev
```

The backend listens on `127.0.0.1:8080` by default. Vite serves the development UI at `http://127.0.0.1:5173` and proxies API requests to the backend.

The server reads `config/defaults.toml`, then applies machine-local configuration. Its default workspace path is `workspace/` below the repository root. The public checkout intentionally does not provide a ready workspace, so startup may report missing model or runtime artifacts until you supply them.

### Build the Windows hosts

Build the main desktop shell, structured browser host, and UI Automation host together:

```powershell
npm run build:hosts
```

Or build them separately while working on one host:

```powershell
npm run build:desktop
npm run build:browser-host
npm run build:uia-host
```

The projects target `net8.0-windows` and `win-x64`. The main shell depends on WebView2 and pythonnet; the browser host depends on WebView2; the UIA host uses WPF automation APIs. These commands produce developer build output under each project's `bin/` and `obj/` directories. They do not recreate my sealed installed package.

### Supplying local models and runtimes

Runtime paths are resolved from the workspace and model bundle metadata. A local bundle is described by `model.json`; the large files referenced by that manifest remain ignored by Git.

The application distinguishes text generation, vision, and image generation roles. A missing image runtime should disable image generation without pretending the text model can render pixels. A missing text runtime prevents model-backed chat from becoming ready.

Keep credentials and machine paths in ignored local configuration or the application's credential store. Do not add tokens to `config/defaults.toml`, commit them to a manifest, or paste them into an issue.

### Context and memory

The selected context size is a runtime allocation, not a promise that every old message is copied into every request. The service trims and budgets conversation history around the current turn and reserves room for output. Agent tasks maintain a smaller task state containing the goal, observations, completed effects, remaining work, and verification evidence.

Saved global memory is another input source. It is queried only when relevant and remains inspectable. This separation is deliberate: a long context window is useful, but it is not a database and should not be treated like one.

### Permissions and computer control

Computer operations are available only through registered capabilities. The interface exposes Agent mode and an authority selector; the backend enforces both again. Approval records are tied to one task and one action shape so approval for one operation cannot be reused for a different target.

Read-only observation and external mutation are treated differently. Sending a message, changing an account, deleting a file, running an administrator command, or entering data into another application carries a different consequence than reading a page or listing a folder. Execution results include structured evidence so later verification can tell what actually changed.

This repository provides the mechanism. Anyone adapting it is responsible for using services and applications within the authorization they actually have.

### Network and privacy boundary

The application service rejects non-loopback binds. Conversations, memory, operation records, browser state, logs, generated images, and local model files live under the ignored workspace on the user's machine.

That does not mean Salty Steak is permanently offline. Research fetches public pages. Browser automation visits sites. Configured connectors and MCP servers can use the network. External links open the default browser. These operations occur only when the corresponding feature is configured and selected for the task.

Credentials are referenced by identifier and resolved at execution time. They should not be inserted into model prompts, activity summaries, screenshots, or Git history.

### What the public checks prove

The repeatable source checks are:

```powershell
npm ci
npm audit
npm run build
npm run check:python
npm run build:hosts
```

Passing them proves dependency installation, frontend compilation, Python syntax compilation, and .NET host compilation for the public tree. It does not prove that a private model bundle is good, that an installed package matches a manifest, or that automation succeeded in a live desktop application. Those require separate runtime, package, and rendered acceptance evidence.

### License and authorship

This is source-available software, not open-source software. The [Salty Steak Source-Available License](LICENSE) permits viewing and running an unmodified copy for the uses it describes. Modification, rebranding, redistribution, sublicensing, selling, hosted-service use, or removal of attribution requires written permission from MD Anik Hasan (Sawlper).

The Salty Steak name, emblem, and Base Steak 2.0 identity remain the work of MD Anik Hasan (Sawlper). Third-party packages keep their own licenses. This repository grants no rights to model weights or other artifacts that are not included here.

<div align="center">
  <img src="app/frontend/public/assets/salty-potato-symbol.svg" width="54" alt="Salty Steak emblem"><br>
  <sub>MD Anik Hasan (Sawlper) · Salty Steak</sub>
</div>
