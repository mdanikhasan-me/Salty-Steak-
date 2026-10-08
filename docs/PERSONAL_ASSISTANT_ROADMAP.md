# Salty Steak: personal assistant direction

Updated 7 October 2026 from the owner's instructions.

## Goal

A capable private daily assistant that can research unfamiliar questions, operate the owner's computer, and implement and verify code through the application's own tools. Quality, factual grounding, reliable actions, and efficient execution take priority over rushing or merely imitating another product's interface. This is a personal system, not a hosted service product.

## Current work order

1. Research: supplied links, search, navigation, source discovery, page/PDF reading, extraction, provenance, failure recovery, grounded answers, and measured efficiency.
2. Computer use: observe actual state, act on current controls, verify outcomes, recover from errors, and avoid repeating completed actions.
3. Coding: generate runnable artifacts, execute behavioral checks, inspect failures, repair source against stable tests, and rerun. Compilation or a model's claim is not proof of correctness.
4. Data management: Settings actions for deleting chat sessions and clearing application-owned data. Preserve the application and all model weights; preview scope before destructive operations. Never sweep a drive or delete unrelated user files.

Use coherent reusable architecture and clear tool contracts. Avoid benchmark-specific answers, arbitrary prompt patches, and claims that a universal task has been solved by one successful example. Permission boundaries, cancellation, provenance, and truthful failure reporting are correctness requirements, not substitutes for capability.

## Storage and version control

Push real source history and useful regression tests to the existing Git remote. Do not fabricate historical commits or dates. Do not store model weights, databases, secrets, personal conversations, generated browser profiles, or full binary builds in ordinary Git history. Remove obsolete candidate packages and redundant recovery copies after identifying exact targets and preserving a working installation/recovery path.

Install an updated native build only at a verified milestone: source checks, complete application execution, candidate audit, data preservation, rendered native acceptance, and post-install checks. Tell the owner clearly when changes are source-only.

## Fine-tuning: planned after the tools stabilize

The owner wants the existing 27B model to reach its potential using this harness. This is a future objective, not authorization to begin training immediately.

Recommended sequence: stabilize and version the tool contracts; collect successful and failed real tasks with outcomes; create held-out evaluations; then test a small reversible adapter against the unchanged base model. Reward correct tool selection, arguments, observations, recovery, and honest verification. Keep private data and credentials out of training examples. A fine-tune cannot repair a broken tool or compensate for missing evidence.

## Future supervised self-improvement

The assistant may eventually propose and implement improvements to its own application. Start with isolated branches/workspaces, executable tests, reviewable diffs, and reversible releases. It must not equate modifying its code with proving that the modification is better. Preserve the owner's authority over deployment, destructive actions, and access to external accounts.

## Honest acceptance

No claim of superiority to ChatGPT, Claude Code, or Antigravity without an appropriate same-task comparison. No promise to access every Internet resource: unavailable servers, authentication, paywalls, verification challenges, and unreadable formats remain explicit limits. Track both answer/action quality and elapsed time; do not improve speed by silently dropping requirements or sources.

## Remaining work register (8 October 2026)

- Research: broaden real-host and difficult-format evaluations; fix incomplete
  evidence coverage and invalid structured reviews; reduce measured retrieval,
  review and answer latency without dropping requested facets. Separate reading,
  factual support, citation membership and final-answer correctness in reports.
- Computer use: validate complete tasks beyond the passing browser action matrix,
  including state refresh, action recovery and the real installed native interface.
- Coding: expand unseen behavioral tasks and failure repairs beyond current samples;
  preserve fixed tests across repairs and clearly display actual execution evidence.
- UI/vision: retain screenshot previews, file diffs and theme work; obtain native
  visual acceptance, and distinguish available vision weights from demonstrated
  image understanding and end-to-end screenshot-driven action quality.
- Data/storage: r212 repairs the owner's interrupted cleanup and HTTP501; keep
  model/app preservation and scope regressions. Improve the generic busy message
  during model warm-up. Obsolete-package cleanup remains subject to exact targets
  and the earlier execution-control rejection; do not bypass that rejection.
- Fine-tuning: not started. Stabilize the harness and held-out evaluations before
  considering a reversible adapter. Supervised self-improvement remains future work.

The installed release and acceptance boundaries are recorded in RELEASE_STATUS.md;
an individual passed milestone never means this whole register is complete.
