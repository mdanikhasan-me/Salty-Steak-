"""Re-entrant tool loop for local Windows automation.

Ordinary chat turns are single-shot: the model answers once and the turn ends.
An automation task needs the opposite shape, because the model cannot know what
a click did until it looks again.  This module runs the observe/act cycle,
feeding every tool result back into the conversation until the model reports
that the task is finished.

The model still has no execution authority.  Each step is validated here and
performed by ``AutomationBroker``, which independently enforces its own grants
and writes its own audit record.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..automation.capability_registry import (
    APPLICATION_LAUNCH_CAPABILITY,
    BROWSER_CAPABILITY,
    FILES_CAPABILITY,
    INPUT_CONTROL_CAPABILITY,
    SCREEN_CAPTURE_CAPABILITY,
    TERMINAL_CAPABILITY,
    UI_AUTOMATION_CAPABILITY,
    WINDOW_CONTROL_CAPABILITY,
    get_capability_descriptor,
)
from ..automation.routing import (
    execution_route_record,
    ordered_capabilities,
    resolve_execution,
)
from ..automation.credentials import redact
from ..automation.policy import (
    DENY,
    REQUIRE_APPROVAL,
    PolicyEngine,
    await_approval,
)
from .actions import _looks_destructive
from .task_runtime import (
    EXECUTING,
    OBSERVING,
    PLANNING,
    TaskCancelled,
    TaskContext,
)


AGENT_SCHEMA = "salty-steak-agent-task-v1"
RESPOND_ACTION = "respond"
MAX_ITERATIONS = 20
MAX_PARSE_FAILURES = 3
MAX_OBSERVATION_CHARACTERS = 2_000
MAX_INSTRUCTION_CHARACTERS = 4_000
STALE_ANALYSIS_CHARACTERS = 160



MAX_IDENTICAL_ATTEMPTS = 3









MAX_IDLE_REPEATS = 2
VISION_PROMPT = (
    "Describe this screen for an automation agent. List the visible windows, "
    "buttons, menus, text fields, and any readable text, and say roughly where "
    "each one sits on screen. Be factual and specific."
)
VISION_OUTPUT_TOKENS = 256

AGENT_RULES_HEAD = (
    "Rules:\n"
    "- The tools above are listed best route first. Always take the highest one "
    "that can do the job. Reading structured information beats looking at "
    "pixels, and looking at pixels beats moving the mouse.\n"
    "- Prefer the most direct capability that accomplishes the task. Do not add "
    "steps the task does not need.\n"




    "- A question the computer can answer directly — free space, a path, a "
    "version, whether something is installed, what is running — is one "
    "terminal.execute call. Do not photograph the screen or walk an interface "
    "tree to find something a command prints.\n"

    "- Every step must change something or learn something new. If you have "
    "just read a page or taken a screenshot, the next step acts on what you "
    "saw; reading it again tells you nothing you do not already have.\n"
)

AGENT_RULES_TAIL = (
    "- Use respond only when the task is fully complete or you have confirmed "
    "it is impossible.\n"
    "- Never ask the user clarifying questions mid-task. Make a reasonable "
    "decision and continue.\n"
    "- If an action fails, try a different approach before giving up.\n"
    "- Describe each action briefly in the reason field so the user can follow "
    "your progress.\n\n"
    "Output format — reply with exactly this JSON object and nothing else:\n"
    '{"action": "<tool name>", "reason": "<one sentence: why this action now>", '
    '"arguments": { }, "answer": "<only when action is respond>"}'
)

SERVICE_WORKFLOW_RULES = {
    BROWSER_CAPABILITY: (
        "- Gmail: use browser.control with https://mail.google.com in the "
        "Salty-owned profile. Query real page elements. If the account needs "
        "sign-in, call show_window and stop for the user to authenticate; never "
        "request or store a password. Discord web is https://discord.com/app.\n"
        "- An opaque browser element handle carries no meaning by itself. When "
        "acting on one, repeat its observed accessible name in the name argument.\n"
        "- Never request, extract, paste, log, or store a raw Discord user token. "
        "Programmatic Discord access must use an official scoped OAuth2 grant or "
        "a dedicated bot account; normal-user login challenges stay visible for "
        "the user to complete.\n"
    ),
    APPLICATION_LAUNCH_CAPABILITY: (
        "- For Discord, prefer application.launch for the installed Discord app. "
        "If it is unavailable, use another granted structured route.\n"
        "- Switching Discord accounts uses Discord's visible Account Switcher. "
        "Choose only an already listed account automatically; adding or signing "
        "into an account waits for the user at password, passkey, CAPTCHA, or 2FA.\n"
    ),
    UI_AUTOMATION_CAPABILITY: (
        "- After Discord is open, use ui.automation to find channels, message "
        "fields, and buttons as controls rather than pixels. Repeat the observed "
        "accessible name beside an opaque element handle.\n"
        "- A Discord server inventory comes from fresh observed server controls. "
        "Scroll and deduplicate what is actually visible; never invent hidden "
        "servers or scrape member/message data that was not requested.\n"
        "- Before joining Discord voice, resolve one exact account, server, and "
        "voice channel from fresh observations and read the current voice state. "
        "If that exact channel is already joined, do not click again. Otherwise "
        "invoke the observed channel once, then verify the joined state before "
        "responding. Never loop or retry a successful/pending join.\n"
    ),
}

MESSAGE_WORKFLOW_RULES = (
    "- A request to draft a Gmail or Discord message stops after the fields are "
    "filled and verified. It never presses Send. For any later send, repeat the "
    "observed control name in the action arguments (for example name=Send) so "
    "the policy can require explicit approval. Never claim a message was sent "
    "until a fresh page or control readback proves it.\n"
)


class AgentTaskError(RuntimeError):
    """Raised when an automation task cannot be run at all."""


def build_system_prompt(capabilities: Sequence[str]) -> str:
    """Describe only the tools whose grants are currently enabled.

    Advertising a capability the broker will refuse makes the model plan around
    a door it cannot open, so an ungranted tool is simply absent.  The ones that
    remain are listed in ladder order, because the order the model reads them in
    is itself a recommendation.
    """

    ordered = ordered_capabilities(capabilities)
    lines = [
        "You are a local Windows automation agent running inside Salty Steak on "
        "this computer.",
        "",
        "You have these tools, best route first:",
    ]
    for capability in ordered:
        try:
            description = get_capability_descriptor(capability).model_instructions
        except KeyError:
            continue
        lines.append(description)
    lines.append(
        "respond — Give your final answer when the task is complete or "
        "confirmed impossible.\n"
        '  Arguments: {"answer": "your message to the user"}'
    )
    rules = AGENT_RULES_HEAD
    for capability in ordered:
        try:
            rules += get_capability_descriptor(capability).agent_rules
        except KeyError:
            continue
        rules += SERVICE_WORKFLOW_RULES.get(capability, "")
    if BROWSER_CAPABILITY in ordered or UI_AUTOMATION_CAPABILITY in ordered:
        rules += MESSAGE_WORKFLOW_RULES
    rules += AGENT_RULES_TAIL
    lines.extend(["", rules])
    return "\n".join(lines)


def parse_agent_action(text: str, allowed: Sequence[str]) -> dict[str, Any]:
    """Validate one whole-JSON action from the model.

    Only a complete JSON object is accepted.  Scraping an action out of
    surrounding prose would let stray model text act as an instruction.
    """

    raw = str(text or "").strip()
    if raw.startswith("```"):

        newline = raw.find("\n")
        raw = raw[newline + 1 :] if newline >= 0 else ""
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3].strip()
    if not raw.startswith("{") or not raw.endswith("}"):
        raise ValueError("Reply with exactly one JSON object and no other text.")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"That was not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("The JSON value must be an object.")

    action = str(parsed.get("action") or "").strip().casefold().replace("_", ".")
    if action == "respond":
        answer = str(parsed.get("answer") or "").strip()
        if not answer:
            arguments = parsed.get("arguments")
            if isinstance(arguments, Mapping):
                answer = str(arguments.get("answer") or "").strip()
        if not answer:
            raise ValueError("A respond action needs a non-empty answer.")
        return {
            "action": RESPOND_ACTION,
            "reason": str(parsed.get("reason") or "").strip()[:500],
            "arguments": {},
            "answer": answer,
        }
    if action not in set(allowed):
        raise ValueError(
            "Unknown action. Choose one of: " + ", ".join([*allowed, RESPOND_ACTION])
        )
    arguments = parsed.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("The arguments field must be a JSON object.")
    return {
        "action": action,
        "reason": str(parsed.get("reason") or "").strip()[:500],
        "arguments": dict(arguments),
        "answer": None,
    }


def effect_key(capability: str, arguments: Mapping[str, Any]) -> str | None:
    """What a call would *achieve*, independent of which rung achieves it.

    Exact-fingerprint stagnation only catches a call repeated verbatim. The
    live failure was subtler and worse: `application.launch` to youtube.com,
    then `browser.control open_url` to youtube.com, then `application.launch`
    again — three different calls, one effect, already true after the first.

    Normalising the effect is deliberately narrow. It maps a destination to the
    place it reaches, not a request to an intent; the model still decides what
    it wants and why. A call whose effect cannot be stated plainly returns None
    and is never suppressed.
    """

    def host_of(value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        lowered = text.casefold()
        if lowered.startswith(("http://", "https://")):
            from urllib.parse import urlsplit

            host = (urlsplit(lowered).hostname or "").removeprefix("www.")
            path = urlsplit(lowered).path.rstrip("/")
            return f"visit:{host}{path}" if host else None
        return None

    if capability == "application.launch":
        target = arguments.get("target")
        return host_of(target) or (
            f"launch:{str(target).strip().casefold()}" if target else None
        )
    if capability == "browser.control":
        command = str(arguments.get("command") or "").casefold()
        if command in {"open_url", "navigate"}:
            return host_of(arguments.get("url"))
        if command == "show_window":
            return "browser:visible"
    if capability == "window.control":
        action = str(arguments.get("action") or "").casefold()
        title = str(arguments.get("title") or "").strip().casefold()
        if action == "focus" and title:
            return f"focus:{title}"
    if capability == "ui.automation":
        command = str(arguments.get("command") or "").casefold()
        if command in {"invoke", "select", "toggle"}:
            element = str(arguments.get("element") or "").strip().casefold()
            name = str(arguments.get("name") or "").strip().casefold()
            if element or name:
                return f"uia:{command}:{element}:{name}"
    if capability == "browser.control":
        command = str(arguments.get("command") or "").casefold()
        if command in {"click", "select"}:
            element = str(arguments.get("element") or "").strip().casefold()
            name = str(arguments.get("name") or "").strip().casefold()
            if element or name:
                return f"browser:{command}:{element}:{name}"
    return None


def step_fingerprint(
    capability: str,
    arguments: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> str:
    """Identify a step by what it did and what came back.

    Volatile fields are excluded so that "the same thing happened again" is not
    hidden by a changing path or timestamp.
    """

    stable = {
        key: value
        for key, value in observation.items()
        if key not in {"screenshot_path", "duration_ms", "audit_record_id"}
    }
    return json.dumps(
        {"capability": capability, "arguments": dict(arguments), "observation": stable},
        sort_keys=True,
        default=str,
    )


def is_destructive(action: str, arguments: Mapping[str, Any]) -> bool:
    """Report whether a step needs review before it can run unattended."""

    if action != TERMINAL_CAPABILITY:
        return False
    argv = arguments.get("argv")
    if not isinstance(argv, list) or not argv:
        return False
    return _looks_destructive([str(item) for item in argv])





MAX_LISTED_PATHS = 40
MAX_OBSERVATION_ITEMS = 40
MAX_OBSERVATION_FIELDS = 50
MAX_OBSERVATION_DEPTH = 6


def _bounded_paths(value: Any) -> list[str]:
    return [str(item) for item in (value or [])][:MAX_LISTED_PATHS]


def _bounded_observation_value(value: Any, *, depth: int = 0) -> Any:
    """Keep structured tool data useful without turning it into a context dump."""

    value = redact(value)
    if depth >= MAX_OBSERVATION_DEPTH:
        return "[nested value omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_observation_value(item, depth=depth + 1)
            for key, item in list(value.items())[:MAX_OBSERVATION_FIELDS]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_observation_value(item, depth=depth + 1)
            for item in list(value)[:MAX_OBSERVATION_ITEMS]
        ]
    if isinstance(value, str):
        return value[:MAX_OBSERVATION_CHARACTERS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_OBSERVATION_CHARACTERS]


def summarise_observation(
    action: str,
    result: Mapping[str, Any],
    *,
    describe_screenshot: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Reduce a broker result to what the model needs for its next decision."""

    status = str(result.get("status") or "unknown")
    observation: dict[str, Any] = {"action": action, "status": status}
    if action == SCREEN_CAPTURE_CAPABILITY:
        artifact = dict(result.get("artifact") or {})
        observation.update(
            {
                "screenshot_path": artifact.get("path"),
                "image_width": artifact.get("width"),
                "image_height": artifact.get("height"),
                "screen_width": artifact.get("source_width"),
                "screen_height": artifact.get("source_height"),
                "scale_divisor": artifact.get("scale_divisor"),
            }
        )



        path = str(artifact.get("path") or "")
        if describe_screenshot is not None and path:
            try:
                description = describe_screenshot(path)
            except Exception as error:
                observation["visual_analysis_error"] = (
                    f"{type(error).__name__}: {error}"[:MAX_OBSERVATION_CHARACTERS]
                )
            else:
                if description:
                    observation["visual_analysis"] = description[
                        :MAX_OBSERVATION_CHARACTERS
                    ]
    elif action == TERMINAL_CAPABILITY:
        observation.update(
            {
                "exit_code": result.get("exit_code"),
                "stdout": str((result.get("stdout") or {}).get("text") or "")[
                    :MAX_OBSERVATION_CHARACTERS
                ],
                "stderr": str((result.get("stderr") or {}).get("text") or "")[
                    :MAX_OBSERVATION_CHARACTERS
                ],
            }
        )
    elif action == FILES_CAPABILITY:







        observation.update(
            {
                "operation": result.get("operation"),
                "mutating": bool(result.get("mutating")),
                "matched_paths": _bounded_paths(result.get("matched_paths")),
                "affected_paths": _bounded_paths(result.get("affected_paths")),
                "preserved_paths": _bounded_paths(result.get("preserved_paths")),
                "failed_paths": list(result.get("failed_paths") or [])[:MAX_LISTED_PATHS],
            }
        )
        after = result.get("after_state")
        if isinstance(after, Mapping):
            observation["after_state"] = {
                "still_present": _bounded_paths(after.get("still_present")),
                "preserved_present": _bounded_paths(after.get("preserved_present")),
            }
    elif action == APPLICATION_LAUNCH_CAPABILITY:
        observation["target"] = result.get("target")
    elif action == INPUT_CONTROL_CAPABILITY:
        observation["performed"] = result.get("action")






    try:
        descriptor = get_capability_descriptor(action)
    except KeyError:
        descriptor = None
    if descriptor is not None:
        for field_name in descriptor.observation_fields:



            if action == SCREEN_CAPTURE_CAPABILITY and field_name == "artifact":
                continue
            if field_name in result and field_name not in observation:
                observation[field_name] = _bounded_observation_value(
                    result[field_name]
                )
    return observation


class AgentLoop:
    """Drive observe/act iterations until the model reports completion."""

    def __init__(
        self,
        *,
        broker: Any,
        generate: Callable[[list[dict[str, str]]], str],
        capabilities: Sequence[str],
        authority_mode: str = "ask_every_time",
        max_iterations: int = MAX_ITERATIONS,
        on_step: Callable[[dict[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        describe_screenshot: Callable[[str], str] | None = None,
        task: TaskContext | None = None,
        memory: Any = None,
        approve: Callable[[Mapping[str, Any]], bool] | None = None,
        policy: PolicyEngine | None = None,
        established: str = "",
    ) -> None:
        self.broker = broker
        self.generate = generate
        self.describe_screenshot = describe_screenshot
        self.memory = memory





        self.established = str(established or "")


        self.task = task or TaskContext()
        self.capabilities = list(capabilities)
        self.authority_mode = (
            "full_access" if authority_mode == "full_access" else "ask_every_time"
        )


        self.approve = approve
        self.policy = policy or PolicyEngine(authority_mode=self.authority_mode)
        self.max_iterations = max(1, min(int(max_iterations), MAX_ITERATIONS))
        self.on_step = on_step


        self.should_stop = should_stop
        self.steps: list[dict[str, Any]] = []


        self.model_turns = 0

        self._attempts: dict[str, int] = {}

        self._effects: set[str] = set()

        self._idle = 0
        self._step_started = time.monotonic()

    def _recall(self, task: str) -> str:
        """Remembered context for this task, or nothing at all.

        Memory is an aid, never a dependency: a store that is missing or
        failing must not stop a task the user asked for.
        """

        if self.memory is None:
            return ""
        try:
            return self.memory.briefing(task)
        except Exception:
            return ""

    def run(self, instruction: str, *, opening: str = "") -> dict[str, Any]:
        task = str(instruction or "").strip()
        if not task or len(task) > MAX_INSTRUCTION_CHARACTERS:
            raise ValueError(
                f"An automation task must contain 1 to {MAX_INSTRUCTION_CHARACTERS} characters"
            )
        if not self.capabilities:
            raise AgentTaskError(
                "No computer-control capability has been granted, so there is "
                "nothing this task can do."
            )
        system_prompt = build_system_prompt(self.capabilities)



        remembered = self._recall(task)
        if remembered:
            system_prompt = f"{system_prompt}\n\n{remembered}"



        if self.established:
            system_prompt = f"{system_prompt}\n{self.established}"
        transcript: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        if opening:


            transcript.append({"role": "user", "content": opening})
        parse_failures = 0

        for iteration in range(1, self.max_iterations + 1):
            self.task.current_step = iteration

            if self._stopped():
                return self._cancelled()
            self.task.transition(PLANNING, step=iteration)
            started = time.monotonic()
            reply = self.generate(list(transcript))
            self.task.note_model_call(time.monotonic() - started)
            self.model_turns += 1

            if self._stopped():
                return self._cancelled()
            transcript.append({"role": "assistant", "content": reply})
            try:
                action = parse_agent_action(reply, self.capabilities)
            except ValueError as error:
                parse_failures += 1
                self.task.metrics.parse_failures += 1
                if parse_failures >= MAX_PARSE_FAILURES:
                    return self._finish(
                        "failed",
                        "The model did not produce a usable action after "
                        f"{parse_failures} attempts.",
                    )


                transcript.append(
                    {"role": "user", "content": json.dumps({"error": str(error)})}
                )
                continue
            parse_failures = 0

            if action["action"] == RESPOND_ACTION:
                self._record(iteration, action, {"status": "completed"})
                return self._finish("completed", str(action["answer"]))




            achieved = effect_key(action["action"], action["arguments"])
            if achieved is not None and achieved in self._effects:
                self.task.metrics.stagnation_breaks += 1
                self.task.record_event("already_satisfied", effect=achieved)
                self._record(
                    iteration,
                    action,
                    {"status": "already_satisfied", "effect": achieved},
                )
                transcript.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "status": "already_satisfied",
                                "effect": achieved,
                                "note": (
                                    "This is already true from an earlier step. "
                                    "Do something that moves the task forward, "
                                    "or respond if the goal is met."
                                ),
                            }
                        ),
                    }
                )
                self._idle += 1
                if self._idle >= MAX_IDLE_REPEATS:




                    return self._finish("completed", self._what_was_achieved())
                continue





            action["arguments"] = self._semantic_action_arguments(
                action["action"], action["arguments"]
            )
            decision = self.policy.evaluate(action["action"], action["arguments"])
            if decision.outcome == DENY:
                self._record(
                    iteration, action, {"status": "blocked", "reason": decision.reason}
                )
                return self._finish("needs_review", decision.reason)
            if decision.outcome == REQUIRE_APPROVAL:
                approved = (
                    await_approval(decision, ask=self.approve, task=self.task)
                    if self.approve is not None
                    else False
                )
                if not approved:
                    self._record(
                        iteration,
                        action,
                        {"status": "blocked", "reason": decision.reason},
                    )
                    return self._finish(
                        "needs_review",
                        f"{decision.reason} It needs your review before it can "
                        "continue: " + json.dumps(redact(action["arguments"])),
                    )


            if self._stopped():
                return self._cancelled()



            route = resolve_execution(
                action["action"], action["arguments"], self.capabilities
            )
            self.task.current_capability = route.capability
            self._step_started = time.monotonic()
            self.task.transition(EXECUTING, capability=route.capability)
            self.task.note_tool_call(route.capability, route.tier_name)
            if route.capability == SCREEN_CAPTURE_CAPABILITY:
                self.task.note_screenshot()
            try:




                from ..automation.invocation import invoke_capability

                result = invoke_capability(
                    self.broker,
                    route.capability,
                    route.arguments,
                    authority_mode=self.authority_mode,
                    granted=self.capabilities,
                    repair=self._repair_arguments,
                )
                observation = summarise_observation(
                    route.capability,
                    result,
                    describe_screenshot=self.describe_screenshot,
                )
            except Exception as error:




                kind = getattr(error, "kind", None) or type(error).__name__
                observation = {
                    "action": route.capability,
                    "status": "failed",
                    "error": f"{kind}: {error}"[:MAX_OBSERVATION_CHARACTERS],
                }
                result = {"status": "failed"}

            if self._stopped():
                return self._cancelled()
            self.task.transition(OBSERVING, capability=route.capability)
            self._record(iteration, action, observation, route=route)


            self.task.world_state.absorb(route.capability, result)





            if str(observation.get("status") or "") == "succeeded":
                landed = effect_key(route.capability, route.arguments)
                if landed is not None:
                    self._effects.add(landed)






                self._idle = 0

            fingerprint = step_fingerprint(
                route.capability, route.arguments, observation
            )
            self._attempts[fingerprint] = self._attempts.get(fingerprint, 0) + 1
            if self._attempts[fingerprint] >= MAX_IDENTICAL_ATTEMPTS:
                self.task.metrics.stagnation_breaks += 1
                self.task.record_event(
                    "stagnation",
                    capability=route.capability,
                    attempts=self._attempts[fingerprint],
                )
                remaining = [
                    name
                    for name in ordered_capabilities(self.capabilities)
                    if name != route.capability
                ]
                if not remaining:
                    return self._finish(
                        "failed",
                        f"{route.capability} produced the same result "
                        f"{self._attempts[fingerprint]} times and no other "
                        "capability is available.",
                    )


                self.task.metrics.escalations += 1
                transcript.append(
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "error": (
                                    f"{route.capability} has returned the same "
                                    f"result {self._attempts[fingerprint]} times. "
                                    "It is not making progress. Use a different "
                                    "capability or respond explaining what is "
                                    "blocking the task."
                                ),
                                "available": remaining,
                            }
                        ),
                    }
                )
                continue

            self._compact_screenshots(transcript)
            message = dict(observation)


            known = self.task.world_state.briefing()
            if known:
                message["known_state"] = known
            transcript.append(
                {"role": "user", "content": json.dumps(message, sort_keys=True)}
            )






        done = [
            str(step.get("reason") or step.get("action") or "")
            for step in self.steps
            if step.get("status") == "succeeded" and step.get("action") != "respond"
        ]
        if done:
            summary = (
                "I ran out of steps before finishing this, so treat it as "
                "incomplete. What did run: " + "; ".join(done)
            )

        else:
            summary = (
                "I ran out of steps before finishing this, and nothing I tried "
                "succeeded."
            )
        return self._finish("exhausted", summary)

    def _what_was_achieved(self) -> str:
        """What the task actually did, for a task that has finished doing it."""

        done = [
            str(step.get("reason") or step.get("action") or "")
            for step in self.steps
            if step.get("status") == "succeeded" and step.get("action") != "respond"
        ]
        if not done:
            return "There was nothing left to do — this was already the case."
        return "Done. " + "; ".join(done)

    @staticmethod
    def _compact_screenshots(transcript: list[dict[str, str]]) -> None:
        """Reduce older captures so only the newest view stays in full.

        Every capture would otherwise accumulate, and a twenty-step task can
        exhaust the window before it finishes.  Earlier descriptions are kept
        as a one-line trace rather than dropped outright, because they are the
        record of what the agent has already seen and tried.
        """

        for message in transcript:
            if message["role"] != "user":
                continue
            if '"screenshot_path"' not in message["content"]:
                continue
            try:
                payload = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or "screenshot_path" not in payload:
                continue
            payload["screenshot_path"] = "(superseded by a newer screenshot)"
            previous = str(payload.get("visual_analysis") or "")
            if len(previous) > STALE_ANALYSIS_CHARACTERS:
                payload["visual_analysis"] = (
                    previous[:STALE_ANALYSIS_CHARACTERS].rstrip() + "… (earlier view)"
                )
            message["content"] = json.dumps(payload, sort_keys=True)

    def _stopped(self) -> bool:
        """One cancellation check for every checkpoint in the loop.

        Reads the in-memory token first so a stop already accepted costs
        nothing, and folds in a caller-supplied predicate exactly once.
        """

        if self.task.stop_requested:
            return True
        if self.should_stop is not None and self.should_stop():
            self.task.request_stop("user_requested")
            return True
        return False

    def _repair_arguments(self, brief: str) -> Mapping[str, Any] | None:
        """One correction, from the model, given the capability's contract."""

        reply = self.generate(
            [
                {
                    "role": "system",
                    "content": (
                        "That tool call was rejected. Reply with one JSON "
                        "object holding only the corrected arguments - no "
                        "prose, no capability name, no explanation."
                    ),
                },
                {"role": "user", "content": brief},
            ]
        )
        from .actions import _whole_json_object
        from .orchestrator import strip_reasoning

        parsed = _whole_json_object(strip_reasoning(str(reply)))
        if not isinstance(parsed, Mapping):
            return None
        inner = parsed.get("arguments")
        return inner if isinstance(inner, Mapping) else parsed

    def _semantic_action_arguments(
        self,
        capability: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Carry an observed control name beside an otherwise opaque handle.

        The approval policy cannot know that ``web-el-7`` is the Gmail Send
        button. The structured read that produced the handle did know, and the
        task world state retained that fact. Reattaching the observed name
        closes the gap without guessing from coordinates or trusting a model to
        classify the consequence of its own action.
        """

        checked = dict(arguments)
        if checked.get("name") or not checked.get("element"):
            return checked
        state_name = (
            "page_elements"
            if capability == BROWSER_CAPABILITY
            else "ui_elements"
            if capability == UI_AUTOMATION_CAPABILITY
            else None
        )
        if state_name is None:
            return checked
        world_state = getattr(self.task, "world_state", None)
        if world_state is None:
            return checked
        try:
            observed = world_state.get(state_name) or []
        except Exception:
            return checked
        handle = str(checked["element"])
        for item in observed:
            if not isinstance(item, Mapping) or str(item.get("element") or "") != handle:
                continue
            name = str(item.get("name") or "").strip()
            if name:
                checked["name"] = name
            break
        return checked

    def _cancelled(self) -> dict[str, Any]:
        self.task.finish_stopped()
        return self._finish("cancelled", "The task was stopped before it finished.")

    def _record(
        self,
        iteration: int,
        action: Mapping[str, Any],
        observation: Mapping[str, Any],
        *,
        route: Any | None = None,
    ) -> None:
        step = {
            "step": iteration,
            "action": action["action"],
            "reason": action.get("reason") or "",
            "arguments": dict(
                route.arguments if route is not None else action.get("arguments") or {}
            ),
            "status": observation.get("status"),
            "observation": dict(observation),

            "route": execution_route_record(route) if route is not None else None,


            "duration_ms": round((time.monotonic() - self._step_started) * 1000, 1),
            "started_at": self._step_started,
        }
        self.steps.append(step)
        if self.on_step is not None:
            self.on_step(dict(step))

    def _finish(self, state: str, answer: str) -> dict[str, Any]:

        if not self.task.finished:
            self.task.finish(
                {
                    "completed": "completed",
                    "cancelled": "stopped",
                    "failed": "failed",
                    "needs_review": "waiting",
                    "exhausted": "failed",
                }.get(state, "completed"),
                failure=answer if state in {"failed", "exhausted"} else None,
            )
        return {
            "schema": AGENT_SCHEMA,
            "state": state,
            "answer": answer,
            "steps": list(self.steps),
            "step_count": len(self.steps),
            "authority_mode": self.authority_mode,
            "capabilities": ordered_capabilities(self.capabilities),


            "model_turns": self.model_turns,
            "tiers_used": sorted(
                {
                    str((step.get("route") or {}).get("tier"))
                    for step in self.steps
                    if step.get("route")
                }
            ),
            "task": self.task.snapshot(),
            "metrics": self.task.metrics.to_dict(),
        }
