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
