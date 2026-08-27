<div align="center">
  <img src="docs/assets/salty-steak-arcade.svg" width="100%" alt="Animated pixel-art Salty Steak workstation">

  <br>

  <img alt="Windows" src="https://img.shields.io/badge/Windows-desktop-36A9E1?style=flat-square&logo=windows11&logoColor=white">
  <img alt="WebView2" src="https://img.shields.io/badge/WebView2-native_host-0EA5E9?style=flat-square&logo=microsoftedge&logoColor=white">
  <img alt=".NET 8" src="https://img.shields.io/badge/.NET-8-7C3AED?style=flat-square&logo=dotnet&logoColor=white">
  <img alt="React 18" src="https://img.shields.io/badge/React-18-06B6D4?style=flat-square&logo=react&logoColor=082F49">
  <img alt="Python backend" src="https://img.shields.io/badge/Python-local_service-FACC15?style=flat-square&logo=python&logoColor=172554">
  <img alt="SQLite WAL" src="https://img.shields.io/badge/SQLite-WAL-22C55E?style=flat-square&logo=sqlite&logoColor=white">
  <img alt="GGUF runtime" src="https://img.shields.io/badge/GGUF-native_worker-F97316?style=flat-square">
  <img alt="Context window" src="https://img.shields.io/badge/context-32K_to_262K-EC4899?style=flat-square">
  <img alt="Source available" src="https://img.shields.io/badge/license-source_available-E11D48?style=flat-square">
  <img alt="Loopback API" src="https://img.shields.io/badge/API-loopback_only-14B8A6?style=flat-square">
  <img alt="Maximum output" src="https://img.shields.io/badge/output-up_to_32K-A855F7?style=flat-square">
  <img alt="Attachment support" src="https://img.shields.io/badge/files-32_per_turn-F59E0B?style=flat-square">
  <img alt="Research ceiling" src="https://img.shields.io/badge/research-up_to_4h-3B82F6?style=flat-square">
  <img alt="Windows UI Automation" src="https://img.shields.io/badge/Windows-UI_Automation-F43F5E?style=flat-square">

  <br><br>

  <strong>Base Steak 2.0 by MD Anik Hasan (Sawlper)</strong><br>
  Windows software for running my local model as a chat assistant, researcher, image workstation, and computer operator.
</div>

## The short version, before we open the engine bay

I started Salty Steak because I wanted one local model to sit behind an ordinary Windows application and do more than produce chat text. The model decides what kind of job it is looking at. The software supplies everything a model cannot safely improvise: conversation boundaries, durable memory, registered tools, permission checks, cancellation, recovery, and a record of what really happened.

Three pieces do most of the work. A native .NET host owns the desktop window and child processes. React owns the interface. Python owns application state and the execution machinery behind Chat, Research, Agent, Images, Memory, and Models & training. Large model runtimes live behind worker interfaces, so the UI does not need to know how a particular local bundle stores weights or places memory.

This repository contains that software harness. It does **not** contain model weights, tokenizers, learned adapters, projectors, image models, private native binaries, conversations, credentials, generated files, or installed packages. A public clone can build the application, but Chat remains cold until a compatible private runtime is provisioned. `npm ci` does many useful things; telepathy is not currently one of them.

The design rule underneath all of this is fairly plain: the model may propose, choose, and reason; the application must execute, remember, constrain, and verify. That split is why a normal answer, a four-hour research run, and a Windows automation plan can share one interface without pretending they are the same operation.

## What wakes up when Salty Steak opens

The installed application is not Electron and it is not a browser pointed at a cloud chat page. The main process is a .NET 8 Windows Forms host in `app/desktop/native/`. It owns the window, WebView2 lifecycle, single-instance behavior, startup reporting, process cleanup, and the bridge between the page and Windows.

The host starts the local Python application service and loads the compiled React interface into WebView2. The service binds to loopback only. A non-loopback host is rejected rather than quietly exposing the API to the local network.

Startup then proceeds in a fixed order:

1. `app/backend/system/config.py` loads `config/defaults.toml` and applies ignored machine-local overrides.
2. `app/backend/system/files.py` creates and validates the workspace layout.
3. `app/backend/database/control.py` opens the control database, migrates the schema, enables SQLite WAL mode, and reconciles interrupted operations.
4. `app/backend/runtime/model_bundles.py` inspects registered `model.json` manifests and selects the active bundle for each role.
5. `app/backend/application.py` checks the text model, companion artifacts, checksums, runtime family, and native library directory before it creates a model worker.
6. A background warm-up starts for the selected text bundle. Vision integrity verification may run separately because a vision projector has a different loading contract.
7. Chat, memory, research, imaging, automation, datasets, training, evaluation, versions, notifications, and plugins are attached to the same application service.

The UI can appear before every large model file is warm. That is why readiness has real states such as cold, preparing, and ready. It is also why a cold first turn is not a fair measurement of steady-state generation speed.

## The process map I use while debugging

```text
Salty Steak.exe
  native window + WebView2
  starts and supervises the Python service
    loopback HTTP API
    control database and operation manager
    ChatService
      persistent text worker over anonymous pipes
      isolated vision process when an image must be read
      isolated image worker when an image must be rendered
      research loop and source ledger
      automation broker
        browser host
        UI Automation host
        elevated terminal worker when required
```

The text worker does not listen on a port. `app/backend/runtime/salty_native_worker.py` is a private child process connected by UTF-8 JSON lines over anonymous stdin/stdout pipes. The parent assigns it to a Windows job object, so closing the application also closes the worker tree. Requests carry an ID; streamed events and the final response come back with that ID; cancellation uses a dedicated signal path and restarts a worker that refuses to acknowledge the stop deadline.

The structured browser and Windows UI Automation hosts are separate native processes. A browser crash should not take the chat window with it, and a broken accessibility tree should not be mistaken for model failure. Less glamorous than one giant process, much easier to debug at 3 AM.

## How the harness hosts a local text runtime

The public source defines a runtime interface; it does not publish a particular set of weights. A machine-local `model.json` selects a registered text role, names the runtime family, points to ignored artifacts, declares supported context settings, and records the checksums the loader must verify. The application rejects missing files, changed digests, unsupported runtime families, and incomplete companion-artifact sets before Chat becomes ready.

The selected text runtime stays in one supervised worker instead of being loaded again for every message. The worker accepts structured generation requests, streams typed events back to Chat, supports cancellation, and reports readiness separately from response generation. Runtime-specific loading and memory placement remain behind the adapter boundary, so they are not coupled to React, the HTTP routes, or conversation storage.

The software contract supports a 32,768-token default context, adaptive selections through 262,144 tokens, and a manual output ceiling up to 32,768 tokens when it fits inside the selected context. The interface receives its available context presets from the registered local manifest. Selecting a large ceiling does not force every short request to allocate or consume that entire amount.

Optional learned components can be registered for narrowly scoped routes. Their files, training data, weight layout, and evaluation artifacts remain private. The public code is responsible only for selection, checksum binding, activation boundaries, restoration of the ordinary runtime state, and a failure path when the declared component cannot be loaded safely.

## Follow one turn from Send to the final message

Suppose a user sends a message with two files, chooses Cooking, leaves output allocation on Automatic, and enables Agent mode.

### 1. The frontend commits the turn

`app/frontend/src/pages/ChatPage.jsx` creates the user message immediately, keeps it tied to the active conversation ID, and sends the selected model settings, authority mode, attachments, and idempotency key through `app/frontend/src/api/client.js`. The idempotency key prevents a retry from quietly creating a second external operation.

Conversation state is keyed by conversation, not by whichever sidebar row happens to be visible when a late fetch returns. That sounds obvious because it is obvious. It still needed regression tests.

### 2. The API validates the request

`app/backend/api/router.py` validates the route, body, multipart uploads, IDs, and limits before calling the application. Uploads first enter an isolated temporary location. The service hashes and inspects them before the chat turn may claim them.

The request can include at most 32 attachments. Each file is limited to 512 MiB. Text, source code, CSV, JSON, office documents, ZIP/TAR archives, and single-stream compressed files receive bounded local inspection. Archive traversal is rejected, individual members are capped, total decompression is capped, and unknown binary data is represented by metadata rather than imaginary contents.

### 3. Chat context is assembled

`app/backend/chat/service.py` loads messages only from the requested conversation. It budgets the recent transcript against the selected context, reserves output space, incorporates bounded attachment excerpts, and retrieves global memory only when the memory feature is enabled and relevance warrants it.

Large Agent missions do not keep appending every screenshot and tool response to the chat prompt. Their compact state retains the goal, relevant world observations, completed effects, open requirements, failures, and verification evidence. The visible chat and the working task state have different jobs, so they have different retention rules.

### 4. The model chooses a job shape

The prepared prompt goes to Base Steak 2.0. `app/backend/chat/orchestrator.py` accepts six top-level job families:

- `respond` for a normal user-facing answer;
- `action` for one registered capability call;
- `plan` for a dependency-checked multi-step task;
- `research` for evidence collection and synthesis;
- `generate_image` for a new render;
- `revise_image` for a change to an existing render brief.

The model may name only registered capabilities. Returning JSON does not grant permission, and returning prose that claims a tool ran does not make the claim true. `app/backend/chat/dispatch.py` checks mode availability before selecting the runner.

### 5. A runner does the actual work

A direct answer returns to the model worker. Research enters the research loop. Image jobs enter the image orchestrator. Actions and plan nodes go through the automation broker. Long work receives a task context with cancellation, deadlines, checkpoints, live activity events, and stale-progress detection.

### 6. The requested outcome is checked again

`app/backend/chat/goal_state.py` turns the user's request into observable predicates. After execution, `app/backend/chat/verification.py` compares those predicates with newly observed state. A step saying “deleted file.txt” is evidence about that step; it is not evidence that every requested file was found, nor that an excluded file survived.

The final response is stored with timing, token counts, route, sources, tool records, artifacts, and verification state. The activity panel renders events produced during the run. It is not supposed to invent twelve tidy-looking steps after the answer has already finished.

## Instant and Cooking change the generation contract

Instant closes the model's private thinking boundary and is intended for direct answers. Cooking opens the model's reasoning path and preserves the private channel until a final answer is available. The visible response contains the answer; the activity view contains concise progress summaries and technical measurements, not the raw hidden reasoning transcript.

Maximum output can be Automatic or Manual. Automatic allocates from the request, selected context, and remaining budget. Manual exposes 256, 512, 1K, 2K, 4K, 8K, 16K, and 32K presets. Natural end-of-generation remains enabled, so selecting 32K does not force the model to turn “hi” into a novella.

## Three kinds of memory, because one giant bucket went badly

These are three separate systems because mixing them produced exactly the sort of ghost instructions nobody enjoys debugging.

**Conversation context** lives in the control database. Messages from Chat A stay in Chat A. A new conversation starts without inheriting another conversation's style request, image preference, or unfinished instruction.

**Global memory** lives in `salty-memory.db`. `app/backend/memory/store.py` uses SQLite WAL and an FTS5 index. `/save mem` is an explicit user command; saved entries can be listed and removed in the Memory panel. Retrieval combines text search with bounded relevance scoring before any memory is inserted into a prompt. Turning automatic memory off stops silent collection without deleting entries the user intentionally saved.

**Mission memory** uses the same database file but different tables for principals, locations, items, actions, and events. It lets a long computer task remember which object was seen, what was attempted, and what changed without treating a 200-step operation log as prose to reread every turn.

No amount of context-window marketing replaces either database. A KV cache is fast temporary working memory. SQLite is durable state. They are friends, not substitutes.

## Research from the first query to the cited answer

`app/backend/research/loop.py` does not hand the first search snippet to the answer model and hope the URL looks respectable. A research run owns a `ResearchLedger` containing queries, source records, extracted observations, claims, publisher identity, contradictions, confidence, and unresolved questions.

Sources are classified and checked for independence. Several pages repeating the same publisher do not become three independent confirmations. A claim may remain disputed; the ledger keeps both sides and tells synthesis that the disagreement exists.

The current profiles are:

- **Verification**: up to 3 minutes, 8 queries, 16 sources, one validation round.
- **Instant Research**: up to 5 minutes, 24 queries, 64 sources, one validation round.
- **Cooking Research**: a hard 4-hour ceiling, up to 1,024 queries and 4,096 sources, two validation rounds.

Those are ceilings, not quotas. Research stops early when coverage and independent-source requirements are satisfied, when repeated searches are barren, when the user cancels, or when the evidence cannot support the requested claim. Fewer defensible results beat ten Discord invite links that expire the moment somebody clicks them.

The final answer receives the ledger, not an unlabelled bag of web text. Sources shown in Chat retain their URLs and claim relationship so the user can inspect where an answer came from.

## Agent mode: the model plans, the broker touches Windows

`app/backend/automation/capability_registry.py` defines the operations the model can request. The current broker can expose filesystem management, terminal execution, application launch, window control, input control, screen capture, Windows UI Automation, the isolated browser, and read-oriented Discord inspection.

Before a task starts, the broker snapshots the granted capabilities and selected authority. Each invocation is schema-validated, risk-classified, bounded, written to the audit log, and returned as a structured observation. The model cannot add a capability by spelling a convincing new name in JSON.

Plans are validated as a whole before the first node runs. `app/backend/workflow/plan.py` checks node shape, dependencies, cycles, connector names, and capability slots. The live runner allows up to 40 nodes in one accepted plan and can replan from observed failure, but it caps depth, repeated attempts, retained steps, observation size, and parse failures. The Agent loop has an eight-hour mission ceiling and 8,192 iteration guard. Reaching a high number is not the goal; completing the predicates is.

The policy engine separates consequence from capability. Reading a UI tree is not the same as clicking Send. Local writes, destructive operations, external messages, and financial actions receive different treatment. Approval is bound to the exact action arguments; approval for one target cannot be replayed against another.

The automation broker also watches recent human input. If the user takes back the mouse or keyboard, foreground automation yields until the desktop has been idle for the configured interval. The validation tools use monitor 2 when instructed, because a development assistant that steals the active monitor is technically functional and practically unbearable.

## Image jobs and vision inputs take separate routes

Image generation is a separate runtime role loaded by an isolated local worker. The public interface exposes 512, 768, and 1024 pixel long-edge presets; 1:1, 4:3, 3:4, 16:9, and 9:16 aspect ratios; and 4, 8, 12, or 20 quality steps. The image weights and their private manifest are not part of this repository.

The text model first writes a durable `RenderBrief`. `app/backend/imaging/brief.py` separates subject, purpose, composition, style, colour, required elements, optional elements, and negative constraints. Revisions merge into the original brief instead of replacing it with the last six words the user typed. Negative constraints go to negative conditioning rather than being copied into the positive prompt where the diffusion model might faithfully draw the thing it was told not to draw.

`app/backend/runtime/steak_gen_engine.py` verifies that the selected private bundle matches the public runtime contract before loading it. Image jobs are persisted, cancellable, and reconciled after restart. Generation time shown in Chat comes from the actual job lifecycle, not only the final GPU call.

Vision uses a different isolated path with a text model, projector, one-use input permission, size-bound staging area, and verified runtime manifest. Attaching a picture does not automatically authorize a screen capture, and a capture permission cannot be reused for a later unrelated image.

## Datasets, training runs, evaluations, and versions

The Models & training workbench is backed by the same operation system as Chat. Dataset import supports TXT, JSONL, JSON, CSV, and Parquet. Source inspection records schema and lineage before preparation. Packing, truncation, train/validation splits, tokenizer identity, and prepared-artifact policies are stored rather than inferred later from filenames.

Training writes recovery state at configured intervals. Finalisation runs checkpoint inspection, isolated load, integrity checks, evaluation, policy checks, and version creation as separate stages. A saved version keeps lineage to its source data and training operation. Promotion is a decision backed by evidence; renaming a folder to `final-final-really-final` is not a promotion strategy.

Identity post-training has its own training, holdout, retention, natural-language acceptance, and live-promotion reports. Dataset contents, learned parameters, checkpoints, and private evaluation results are not published here. Cloning the harness does not reproduce a private training run.

## What survives a restart

The control database stores conversations, messages, operations, operation events, datasets, prepared artifacts, training runs, versions, runtime state, image jobs, chat artifacts, notifications, connector configuration, grants, and automation audit records. Schema migration happens at startup inside the application, not in the frontend.

Operations have durable states and heartbeats. On restart, incomplete work is reconciled instead of simply disappearing from the UI. Training and image jobs keep their own recovery rules because “resume generation” and “resume optimiser state” are not the same operation.

Generated artifacts are written beneath the ignored workspace and attached to their conversation records. The native file download path serves the exact stored bytes; it does not reconstruct a file from whatever happens to be visible in a code block.

## A walk through the source tree

`app/frontend/` contains the React 18 interface, state, accessible components, chat workflows, and CSS.

`app/backend/api/` contains the loopback HTTP adapter and response contracts. `app/backend/chat/` contains context assembly, routing, runners, goal predicates, task state, live activity, and verification.

`app/backend/runtime/` contains the text, vision, and image worker clients plus the native model adapter. `app/backend/automation/` contains the capability broker, policy engine, grants, audit records, browser/UIA clients, filesystem operations, and elevated terminal worker.

`app/backend/research/`, `memory/`, `imaging/`, `datasets/`, `training/`, `evaluation/`, and `versions/` contain the corresponding subsystems. `app/backend/database/control.py` owns the central schema.

`app/desktop/native/` is the main WebView2 shell. `app/desktop/browser/` is the structured browser process. `app/desktop/uia/` is the Windows accessibility process.

`config/defaults.toml` is the source default file. `config/local.toml`, `.codex/`, `workspace/`, model files, build output, evidence, caches, and installed packages are local-only or ignored as appropriate.

## Build the public source on Windows

The checkout is intended for Windows. Install Node.js, Python, the .NET 8 SDK with Windows Desktop support, WebView2 Runtime, and NuGet access.

From PowerShell in the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm ci
```

Build the React interface and compile-check the Python tree:

```powershell
npm run build
npm run check:python
```

Run the development service and Vite in separate terminals:

```powershell
npm run server
```

```powershell
npm run dev
```

The default service URL is `http://127.0.0.1:8080`; Vite uses `http://127.0.0.1:5173` and proxies API calls locally.

Build all three Windows hosts:

```powershell
npm run build:hosts
```

Or build one host while working on it:

```powershell
npm run build:desktop
npm run build:browser-host
npm run build:uia-host
```

These commands produce developer output. They do not recreate my signed or sealed installed package, and they do not provision the private workspace.

## Bring a private runtime without publishing it

Model roles are registered under the ignored workspace with a `model.json` manifest. The manifest names the role, artifact, checksum, runtime family, context policy, companion artifacts, and activation state. The referenced large files must already exist locally.

Machine paths and secrets belong in ignored configuration or the credential store. Do not put access tokens in `config/defaults.toml`, commit a personal workspace, or paste a full runtime manifest into a public issue. The repository `.gitignore` excludes conversations, screenshots, logs, databases, weights, checkpoints, adapters, tokenizers, model bundles, local Codex state, native packages, and validation evidence.

## The checks I run, and the claims they cannot make

The public source gates are:

```powershell
npm ci
npm audit
npm run build
npm run check:python
npm run build:hosts
```

They prove that dependencies install, the frontend compiles, Python modules compile, and the three .NET hosts build from the published tree. They do not prove that a private weight bundle loads, that 262K allocation fits a particular machine, that an installed package matches its manifest, that the image model meets a speed target, or that a desktop automation completed its real task.

Those claims need different evidence: checksum audits for packages, native generation probes for runtime limits, rendered screenshots for UI acceptance, measured jobs for performance, and observed postconditions for automation. A green toast is pleasant. It is not a lab report.

## Why the older Git dates say “reconstructed”

I began this project in January 2026, before this GitHub repository existed. The source survived a drive failure and a move to another computer; the original local Git timestamps did not. The January-to-August dates on the older commits are therefore reconstructed estimates. Their parent order and file changes are preserved, so they remain useful for answering “what changed before this?”, but the exact day and clock time should not be treated as forensic evidence. Work committed after the repository was established keeps its normal timestamp.

## License and authorship

The [Salty Steak Source-Available License](LICENSE) permits viewing the source and running an unmodified copy for the uses it describes. Modification, rebranding, redistribution, sublicensing, selling, hosted-service use, or removal of attribution requires written permission from **MD Anik Hasan (Sawlper)**.

Salty Steak, its emblem, and the Base Steak 2.0 identity belong to MD Anik Hasan (Sawlper). Third-party packages retain their own licenses. No rights are granted to model weights, checkpoints, datasets, tokenizers, private runtimes, or other artifacts that are not included in this repository.

<div align="center">
  <img src="app/frontend/public/assets/salty-potato-symbol.svg" width="54" alt="Salty Steak emblem"><br>
  <sub>MD Anik Hasan (Sawlper) / Salty Steak</sub>
</div>
