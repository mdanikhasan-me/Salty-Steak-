# Salty Steak

Salty Steak is a local-first desktop application for chat, research, image workflows, and computer automation. The public repository contains the application source and the native desktop host only.

The public identity is **Base Steak 2.0**, by **MD Anik Hasan (Sawlper)**. The repository does not publish model weights, checkpoints, datasets, private runtime bundles, installed binaries, conversations, screenshots, or machine-local state.

## Repository layout

```text
app/backend/       Python service, chat orchestration, memory, research, and tools
app/frontend/      React and Vite desktop interface
app/desktop/       Windows WebView2 and UI Automation hosts
config/            Safe defaults; private overrides stay local
```

## Local development

Use Windows with Python 3.12 or newer, Node.js, npm, and the .NET 8 Windows Desktop SDK.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm ci
npm run build
.venv\Scripts\python.exe -m compileall app
```

The application expects a separately provisioned local model and runtime. Those artifacts are intentionally excluded from this repository. Set machine-local paths in `config/local.toml` or through the runtime environment; never commit credentials or private workspace data.

## Privacy and data boundary

Conversation databases, memory, attachments, generated images, logs, model files, validation captures, package output, and installed applications remain on the local machine. The source tree does not contain a chat transcript or user account tokens. Network connectors are used only when explicitly configured by the local operator.

## License and attribution

This source is available under the accompanying `LICENSE`. It preserves authorship and attribution for MD Anik Hasan (Sawlper). The license does not grant rights to copy the Base Steak identity, imply endorsement, or redistribute excluded model and runtime artifacts. Third-party dependencies retain their own licenses.

![Salty Steak emblem](app/frontend/public/assets/salty-potato-symbol.svg)
