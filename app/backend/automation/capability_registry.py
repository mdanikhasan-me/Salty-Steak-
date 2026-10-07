"""One discoverable contract for every local computer capability.

The broker, router, agent prompt, repair path, and status API all need to know
the same facts about a capability.  Keeping separate lists of ids, accepted
fields, prose affordances, and tool instructions made those views drift.  This
registry is the source of truth; the execution code remains in the broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .browser_client import BROWSER_COMMANDS
from .uia_client import UIA_COMMANDS

TERMINAL_CAPABILITY = "terminal.execute"
FILES_CAPABILITY = "files.manage"
SCREEN_CAPTURE_CAPABILITY = "screen.capture"
INPUT_CONTROL_CAPABILITY = "input.control"
APPLICATION_LAUNCH_CAPABILITY = "application.launch"
WINDOW_CONTROL_CAPABILITY = "window.control"
UI_AUTOMATION_CAPABILITY = "ui.automation"
BROWSER_CAPABILITY = "browser.control"
DISCORD_INSPECT_CAPABILITY = "discord.inspect"

FILE_OPERATIONS = frozenset(
    {
        "list",
        "inspect",
        "stat",
        "exists",
        "search",
        "read",
        "write",
        "create_directory",
        "copy",
        "move",
        "rename",
        "delete",
    }
)


@dataclass(frozen=True)
class CapabilityDescriptor:
    """The model-facing and runtime-facing contract of one primitive."""

    capability_id: str
    display_name: str
    description: str
    affordance: str
    argument_types: dict[str, str]
    required_arguments: tuple[str, ...]
    operations: tuple[str, ...]
    side_effect_class: str
    batch_capable: bool
    verification: str
    cancellation: str
    model_instructions: str
    agent_rules: str = ""
    output_types: dict[str, str] = field(default_factory=dict)
    observation_fields: tuple[str, ...] = ()
    supported_observations: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    permission: str = "explicit_grant_and_task_confirmation"

    @property
    def optional_arguments(self) -> tuple[str, ...]:
        required = set(self.required_arguments)
        return tuple(sorted(name for name in self.argument_types if name not in required))

    def contract(self) -> dict[str, Any]:
        """Compact schema supplied to validation and one-shot model repair."""

        return {
            "capability": self.capability_id,
            "description": self.description,
            "accepts": sorted(self.argument_types),
            "required": sorted(self.required_arguments),
            "optional": list(self.optional_arguments),
            "argument_types": dict(self.argument_types),
            "input_schema": {
                "type": "object",
                "properties": dict(self.argument_types),
                "required": sorted(self.required_arguments),
            },
            "output_schema": {
                "type": "object",
                "properties": dict(self.output_types),
            },
            "operations": list(self.operations),
            "side_effect_class": self.side_effect_class,
            "batch_capable": self.batch_capable,
            "verification": self.verification,
            "supported_observations": list(self.supported_observations),
            "requirements": list(self.requirements),
            "permission": self.permission,
            "cancellation": self.cancellation,
        }


CAPABILITY_REGISTRY: dict[str, CapabilityDescriptor] = {
    TERMINAL_CAPABILITY: CapabilityDescriptor(
        capability_id=TERMINAL_CAPABILITY,
        display_name="Terminal command",
        description="Execute one argv-only local process and capture bounded output.",
        affordance="run a command and read its output",
        argument_types={
            "argv": "array[string]",
            "working_directory": "absolute_path",
            "timeout_seconds": "number",
            "elevated": "boolean",
        },
        required_arguments=("argv",),
        operations=("execute",),
        side_effect_class="system_change",
        batch_capable=False,
        verification="exit status and captured process output; external effects need readback",
        cancellation="in_flight",
        model_instructions=(
            "terminal.execute — Run a terminal command and capture its output.\n"
            '  Arguments: {"argv": ["executable", "arg1"], "timeout_seconds": 10, '
            '"elevated": false}. Set elevated=true only when the task genuinely '
            "requires administrator rights and Full access is selected."
        ),
        output_types={
            "status": "string",
            "exit_code": "integer|null",
            "stdout": "bounded_text_capture",
            "stderr": "bounded_text_capture",
            "timed_out": "boolean",
            "duration_ms": "number",
        },
        observation_fields=("exit_code", "stdout", "stderr", "timed_out"),
        supported_observations=("process_status", "process_output"),
        requirements=("resolvable executable", "argv-only invocation", "bounded timeout"),
    ),
    FILES_CAPABILITY: CapabilityDescriptor(
        capability_id=FILES_CAPABILITY,
        display_name="Files and folders",
        description="Observe and change local files and folders under an explicit path.",
        affordance="look at and change files and folders on this computer",
        argument_types={
            "operation": "string",
            "path": "absolute_path",
            "paths": "array[absolute_path]",
            "pattern": "glob",
            "recursive": "boolean",
            "destination": "absolute_path",
            "permanent": "boolean",
            "content": "string",
            "overwrite": "boolean",
            "expected_sha256": "string",
        },
        required_arguments=("operation", "path"),
        operations=tuple(sorted(FILE_OPERATIONS)),
        side_effect_class="mixed",
        batch_capable=True,
        verification="matched, affected, preserved, failed, and resulting filesystem state",
        cancellation="between_calls",
        model_instructions=(
            "files.manage — Look at and change files and folders by path. Use this "
            "for anything about files: listing, finding, reading, copying, moving, "
            "renaming, deleting, making a folder, writing a UTF-8 text file.\n"
            "  It reports which paths matched, which it changed, and which it left "
            "alone, so you can confirm the right ones were affected.\n"
            '  Arguments: {"operation": "list|search|read|stat|exists|copy|move|'
            'rename|delete|create_directory|write", "path": "C:\\\\absolute\\\\path", '
            '"paths": ["C:\\\\exact\\\\observed.log"], "pattern": "*.log", '
            '"recursive": false, "destination": "C:\\\\absolute\\\\path", '
            '"permanent": false}\n'
            "  path is an absolute folder or file. pattern selects inside a folder; "
            "paths is an exact observed batch for copy, move, or delete. Deletes go "
            "to the Recycle Bin unless permanent is true. For write, supply content; "
            "the parent folder must exist. Creating a file uses overwrite=false. "
            "Replacing a file requires overwrite=true and its expected_sha256 from "
            "a prior read to avoid overwriting a concurrent change. Max content 1 MiB."
        ),
        output_types={
            "status": "string",
            "operation": "string",
            "path": "absolute_path",
            "entries": "array[path_metadata]",
            "exists": "boolean",
            "content": "bounded_text",
            "sha256": "string",
            "readback_verified": "boolean",
            "change": "bounded_file_diff",
            "truncated": "boolean",
            "matched_paths": "array[absolute_path]",
            "affected_paths": "array[absolute_path]",
            "preserved_paths": "array[absolute_path]",
            "failed_paths": "array[error]",
            "after_state": "filesystem_state",
        },
        observation_fields=(
            "operation",
            "path",
            "entries",
            "exists",
            "content",
            "sha256",
            "readback_verified",
            "change",
            "truncated",
            "matched_paths",
            "affected_paths",
            "preserved_paths",
            "failed_paths",
            "after_state",
        ),
        supported_observations=(
            "path_metadata",
            "directory_entries",
            "file_content",
            "filesystem_state",
        ),
        requirements=("absolute declared path", "bounded selection"),
    ),
    SCREEN_CAPTURE_CAPABILITY: CapabilityDescriptor(
        capability_id=SCREEN_CAPTURE_CAPABILITY,
        display_name="Primary-screen screenshot",
        description="Capture the primary display as a bounded image artifact.",
        affordance="look at the screen",
        argument_types={"screen": "string(primary)"},
        required_arguments=(),
        operations=("capture",),
        side_effect_class="read",
        batch_capable=False,
        verification="captured artifact metadata and pixel dimensions",
        cancellation="between_calls",
        model_instructions=(
            "screen.capture — Take a screenshot of the primary display. Use this to "
            "observe the current state.\n  Arguments: {}"
        ),
        agent_rules=(
            "- A screenshot observation carries a visual_analysis field describing "
            "what is on screen. Read it before deciding where to click; it is your "
            "sight.\n"
            "- Screen coordinates are real screen pixels. If a screenshot reports a "
            "scale_divisor above 1, multiply the coordinates you read off the image "
            "by that number before using them.\n"
        ),
        output_types={"status": "string", "artifact": "image_artifact"},
        observation_fields=("artifact",),
        supported_observations=("screen_pixels", "image_artifact"),
        requirements=("interactive Windows desktop",),
    ),
    INPUT_CONTROL_CAPABILITY: CapabilityDescriptor(
        capability_id=INPUT_CONTROL_CAPABILITY,
        display_name="Mouse and keyboard control",
        description="Inject one bounded mouse or keyboard action into the local session.",
        affordance="move the mouse and type",
        argument_types={
            "action": "string",
            "x": "integer",
            "y": "integer",
            "button": "string",
            "double": "boolean",
            "clicks": "integer",
            "key": "string",
            "text": "string",
            "combo": "string",
            "post_action_delay_ms": "integer",
            "expected_window_handle": "integer",
            "expected_process_id": "integer",
        },
        required_arguments=("action",),
        operations=(
            "key_combo",
            "key_press",
            "mouse_click",
            "mouse_move",
            "mouse_scroll",
            "type_text",
        ),
        side_effect_class="system_change",
        batch_capable=False,
        verification="requires a subsequent screen or application-state observation",
        cancellation="between_calls",
        model_instructions=(
            "input.control — Control the mouse and keyboard.\n"
            '  Arguments: {"action": "mouse_move"|"mouse_click"|"mouse_scroll"|'
            '"key_press"|"type_text"|"key_combo", ...action-specific arguments}\n'
            '  mouse_move/mouse_click/mouse_scroll take "x" and "y" in real screen '
            'pixels. mouse_click also takes "button" and optional "double". '
            'mouse_scroll takes "clicks" (negative scrolls down). key_press takes '
            '"key". type_text takes "text". key_combo takes "combo" such as "Ctrl+S".'
            ' When a window was observed, include its "expected_window_handle" '
            'and "expected_process_id" so input is refused if focus moves.'
        ),
        agent_rules=(
            "- Take a screenshot before input.control, and only then. You need to "
            "see the screen to know where to click or type; you do not need to see "
            "it to launch something or to run a command.\n"
            "- After a click or keystroke changes the screen, take one screenshot "
            "to confirm the result, then continue.\n"
        ),
        output_types={
            "status": "string",
            "action": "string",
            "x": "integer|null",
            "y": "integer|null",
            "button": "string|null",
            "combo": "string|null",
            "character_count": "integer|null",
            "duration_ms": "number",
            "foreground_window": "window|null",
        },
        observation_fields=(
            "action",
            "x",
            "y",
            "button",
            "combo",
            "character_count",
            "foreground_window",
        ),
        supported_observations=("input_dispatch_status",),
        requirements=("interactive Windows desktop", "focused target for keyboard input"),
    ),
    APPLICATION_LAUNCH_CAPABILITY: CapabilityDescriptor(
        capability_id=APPLICATION_LAUNCH_CAPABILITY,
        display_name="Application and link launch",
        description="Launch an installed application, file, or web address.",
        affordance="open an application, a file, or a web address",
        argument_types={
            "target": "string|absolute_path|url",
            "arguments": "string",
            "wait_ms": "integer",
            "elevate": "boolean",
        },
        required_arguments=("target",),
        operations=("launch",),
        side_effect_class="system_change",
        batch_capable=False,
        verification="process or window observation is required for goal completion",
        cancellation="between_calls",
        model_instructions=(
            "application.launch — Launch an installed application or open a link.\n"
            '  Arguments: {"target": "app name, https URL, or absolute file path", '
            '"wait_ms": optional milliseconds to wait after launching, '
            '"elevate": false}. Set elevate=true only for an application that '
            "genuinely requires administrator rights while Full access is selected."
        ),
        agent_rules=(
            "- Opening a website or an application is a single application.launch "
            "call. Do it straight away, without observing the screen first.\n"
        ),
        output_types={
            "status": "string",
            "target": "string",
            "target_resolution": "string",
            "process_id": "integer|null",
        },
        observation_fields=("target", "target_resolution", "process_id"),
        supported_observations=("launch_status", "resolved_target"),
        requirements=("installed handler or resolvable application",),
    ),
    WINDOW_CONTROL_CAPABILITY: CapabilityDescriptor(
        capability_id=WINDOW_CONTROL_CAPABILITY,
        display_name="Window listing and focus",
        description="List, focus, or close visible top-level Windows windows.",
        affordance="find, focus and close windows",
        argument_types={"action": "string", "title": "string", "handle": "integer"},
        required_arguments=("action",),
        operations=("close", "focus", "list"),
        side_effect_class="mixed",
        batch_capable=False,
        verification="window enumeration and foreground-window readback",
        cancellation="between_calls",
        model_instructions=(
            "window.control — List, focus, or close real windows by their titles. "
            "Windows already knows what is open, so use this instead of hunting for "
            "a window in a screenshot.\n"
            '  Arguments: {"action": "list"|"focus"|"close", "title": "part of '
            'the window title"}'
        ),
        agent_rules=(
            "- To reach an application that is already open, use window.control "
            "focus. Do not screenshot the desktop looking for it.\n"
        ),
        output_types={
            "status": "string",
            "action": "string",
            "window_count": "integer",
            "windows": "array[window]",
            "window": "window|null",
        },
        observation_fields=("action", "window_count", "windows", "window"),
        supported_observations=("open_windows", "window_identity"),
        requirements=("interactive Windows desktop",),
    ),
    UI_AUTOMATION_CAPABILITY: CapabilityDescriptor(
        capability_id=UI_AUTOMATION_CAPABILITY,
        display_name="Semantic control of application interfaces",
        description="Observe and operate Windows accessibility controls by stable handles.",
        affordance="read and operate the controls inside an application",
        argument_types={
            "command": "string",
            "window": "string",
            "process_id": "integer",
            "window_handle": "integer",
            "element": "string",
            "name": "string",
            "automation_id": "string",
            "control_type": "string",
            "class_name": "string",
            "pattern": "string",
            "exact": "boolean",
            "enabled_only": "boolean",
            "visible_only": "boolean",
            "limit": "integer",
            "depth": "integer",
            "max_nodes": "integer",
            "value": "string",
            "amount": "number",
            "horizontal": "boolean",
            "vertical_percent": "number",
            "post_action_delay_ms": "integer",
            "scope_name": "string",
            "scope_control_type": "string",
            "scope_exact": "boolean",
            "item_control_type": "string",
            "scroller_name": "string",
            "max_scrolls": "integer",
            "post_scroll_delay_ms": "integer",
            "reverse": "boolean",
            "timeout_ms": "integer",
        },
        required_arguments=("command",),
        operations=tuple(sorted(UIA_COMMANDS)),
        side_effect_class="mixed",
        batch_capable=False,
        verification="fresh accessibility-tree or control-property readback",
        cancellation="between_calls",
        model_instructions=(
            "ui.automation — Read and operate the controls inside an application "
            "through Windows accessibility: buttons, text boxes, lists and menus "
            "as real controls rather than pixels. Prefer this over looking at the "
            "screen.\n"
            '  Arguments: {"command": "get_active_window"|"get_windows"|"get_tree"|'
            '"find_control"|"collect_list"|"get_text"|"get_properties"|"focus"|"invoke"|'
            '"set_value"|"select"|"toggle"|"expand"|"collapse"|"scroll", ...}\n'
            '  Scope a query with "process_id" (exact) or "window" (title text). '
            'Search with "name", "control_type", "automation_id", or "pattern" '
            '("Value" finds something you can type into, "Invoke" something you '
            'can press). find_control returns an "element" handle; pass that '
            "handle to act on it. A top-level window may be focused directly by "
            'supplying its exact "process_id", "window_handle", or visible '
            '"window" title to the focus command. Mutating commands may include '
            '"post_action_delay_ms" before the next readback. scroll may use '
            '"vertical_percent" from 0 to 100 for a verified collection boundary.'
        ),
        agent_rules=(
            "- To press a button or fill a field inside an application, find it "
            "with ui.automation find_control and act on the handle it returns. "
            "Only look at the screen if the control is genuinely not exposed.\n"
            "- If ui.automation reports unsupported_pattern or the control is not "
            "found, that route is exhausted for this element: try a different "
            "control, or escalate to the screen.\n"
        ),
        output_types={
            "status": "string",
            "command": "string",
            "window": "window|null",
            "windows": "array[window]",
            "matches": "array[accessible_element]",
            "element": "accessible_element|null",
            "nodes": "array[accessible_element]",
            "tree": "accessible_element_tree|null",
            "text": "bounded_text|null",
            "properties": "object|null",
            "count": "integer|null",
            "node_count": "integer|null",
            "truncated": "boolean|null",
            "items": "array[accessible_element]",
            "pages_read": "integer|null",
            "scroll_boundary_reached": "boolean|null",
            "boundary_basis": "string|null",
            "last_vertical_percent": "number|null",
            "truncated_by_limit": "boolean|null",
        },
        observation_fields=(
            "command",
            "window",
            "windows",
            "matches",
            "element",
            "nodes",
            "tree",
            "text",
            "properties",
            "count",
            "node_count",
            "truncated",
            "items",
            "pages_read",
            "scroll_boundary_reached",
            "boundary_basis",
            "last_vertical_percent",
            "truncated_by_limit",
        ),
        supported_observations=(
            "active_window",
            "open_windows",
            "accessibility_tree",
            "control_properties",
            "control_text",
        ),
        requirements=("Windows UI Automation support", "non-elevated reachable target"),
    ),
    BROWSER_CAPABILITY: CapabilityDescriptor(
        capability_id=BROWSER_CAPABILITY,
        display_name="Structured web page reading and interaction",
        description="Observe and operate web pages in the Salty-owned browser session.",
        affordance="read and operate web pages in a browser",
        argument_types={
            "command": "string",
            "tab": "string",
            "url": "url",
            "element": "string",
            "role": "string",
            "name": "string",
            "text": "string",
            "href": "string",
            "selector": "string",
            "value": "string",
            "exact": "boolean",
            "editable": "boolean",
            "visible": "boolean",
            "enabled": "boolean",
            "limit": "integer",
            "text_limit": "integer",
            "offset": "integer",
            "text_offset": "integer",
            "table_offset": "integer",
            "wait_timeout_ms": "integer",
        },
        required_arguments=("command",),
        operations=tuple(sorted(BROWSER_COMMANDS)),
        side_effect_class="mixed",
        batch_capable=False,
        verification="fresh DOM, tab, URL, element, and visible-window readback",
        cancellation="between_calls",
        model_instructions=(
            "browser.control — Read and operate web pages structurally in a browser "
            "session Salty Steak owns. Use this for anything on the web: it reads "
            "the page as elements rather than pixels.\n"
            "  This session can be shown in the app's browser pane. Call "
            '"show_window" to present it whenever the point of the '
            "task is for them to watch or use the page, and whenever they ask to "
            "see something. Playing a video they cannot see is not playing it.\n"
            '  Arguments: {"command": "open_url"|"read_page"|"query"|"wait_for"|"get_element"|'
            '"click"|"set_value"|"select"|"submit"|"scroll"|"back"|"forward"|'
            '"reload"|"get_page"|"show_window"|"hide_window"|"get_media"|'
            '"play_media"|"pause_media"|"capture_preview"|"list_tabs"|"get_session_state", ...}\n'
            '  open_url takes "url". wait_for waits up to wait_timeout_ms (maximum 10000) for a '
            'matching visible enabled control, then returns fresh handles or timed_out=true. '
            'Use it for delayed page controls rather than repeatedly planning an empty query. '
            'query finds elements by "role" (button, link, '
            'textbox, checkbox), "name", "text", "href", or "editable": true, and '
            'returns an "element" handle for each match. Pass that handle to click '
            "or set_value. read_page returns bounded text plus up to 40 controls. "
            "Continue with text_offset=next_text_offset for more text or "
            "offset=next_offset for more controls, using the same tab. A null next "
            "offset means that part is finished; a partial read is not the whole page. "
            "text_limit is at most 8000. Use query to find a specific element directly. "
            "After an action, inspect after_state for the actual result. An action_dispatched flag "
            "does not prove the task succeeded. If observation_error is present, read_page again; "
            "do not repeat a potentially completed click. Element handles expire on navigation. "
            "For visual design or appearance, capture_preview captures the owned tab "
            "and returns its visual analysis when available. DOM text alone cannot prove appearance."
        ),
        agent_rules=(
            "- For anything on a website, use browser.control. Find elements with "
            "query and act on the handle it returns; never look for a link in a "
            "screenshot.\n"
            "- If query reports ambiguous with several matches, refine it with more "
            "of the name or surrounding text rather than picking one.\n"
            "- If the user asked to watch, play, "
            "read or see anything, call show_window so it is actually in front of "
            "them, and say that you have done so.\n"
            "- For audio or video, inspect get_media and use the returned element "
            "handle with play_media. Verify that playing is true and current_time "
            "advances; a successful click is not proof of playback.\n"
        ),
        output_types={
            "status": "string",
            "command": "string",
            "url": "url|null",
            "title": "string|null",
            "heading": "string|null",
            "summary": "bounded_text|null",
            "controls": "array[web_element]",
            "control_count": "integer|null",
            "total_control_count": "integer|null",
            "offset": "integer|null",
            "next_offset": "integer|null",
            "text_offset": "integer|null",
            "text_length": "integer|null",
            "table_rows": "array[bounded_text]",
            "total_table_rows": "integer|null",
            "next_table_offset": "integer|null",
            "next_text_offset": "integer|null",
            "matches": "array[web_element]",
            "count": "integer|null",
            "ambiguous": "boolean|null",
            "timed_out": "boolean|null",
            "waited_ms": "integer|null",
            "element": "web_element|null",
            "media": "array[media_element]",
            "tabs": "array[browser_tab]",
            "tab": "string|null",
            "active": "string|null",
            "active_tab": "string|null",
            "visible": "boolean|null",
            "artifact": "object|null",
            "failure_kind": "string|null",
            "action_dispatched": "boolean|null",
            "after_state": "object|null",
            "observation_error": "string|null",
            "error": "string|object|null",
        },
        observation_fields=(
            "command",
            "url",
            "title",
            "heading",
            "summary",
            "controls",
            "control_count",
            "total_control_count",
            "offset",
            "next_offset",
            "text_offset",
            "text_length",
            "table_rows",
            "total_table_rows",
            "next_table_offset",
            "next_text_offset",
            "matches",
            "count",
            "ambiguous",
            "timed_out",
            "waited_ms",
            "element",
            "media",
            "tabs",
            "tab",
            "active",
            "active_tab",
            "artifact",
            "visible",
            "failure_kind",
            "action_dispatched",
            "after_state",
            "observation_error",
            "error",
        ),
        supported_observations=(
            "page_identity",
            "page_content",
            "web_elements",
            "browser_tabs",
            "media_state",
            "surface_visibility",
        ),
        requirements=("Salty-owned browser session", "http or https target"),
    ),
    DISCORD_INSPECT_CAPABILITY: CapabilityDescriptor(
        capability_id=DISCORD_INSPECT_CAPABILITY,
        display_name="Discord structured inspection",
        description=(
            "Execute exactly one model-selected Discord observation or navigation "
            "operation through bounded, audited native controls."
        ),
        affordance="inspect Discord servers, channels, and visible messages",
        argument_types={
            "operation": "string",
            "query": "string",
            "channel": "string",
            "channels": "array[string]",
            "limit": "integer",
            "max_scrolls": "integer",
            "cursor": "integer",
            "batch_limit": "integer",
            "refresh": "boolean",
        },
        required_arguments=("operation",),
        operations=(
            "current_account",
            "find_channels",
            "inventory",
            "list_servers",
            "open_channel",
            "read_channel",
            "scan_batch",
        ),
        side_effect_class="mixed",
        batch_capable=True,
        verification="fresh Discord window, scoped control, and message-list readback",
        cancellation="between_calls",
        model_instructions=(
            "discord.inspect — Use Discord's native Quick Switcher and Windows "
            "accessibility as one reliable operation. Prefer this over composing "
            "window, keyboard, and UI-tree calls yourself. It never sends, reacts, "
            "joins, logs in, or reads a raw token. Call it directly for Discord "
            "inspection: it locates, launches when absent, and focuses the installed "
            "Discord app itself.\n"
            "  Use only fields needed by the selected operation. Examples:\n"
            '  complete inventory: {"operation":"inventory","refresh":false}\n'
            '  discover: {"operation":"find_channels","query":"giveaway",'
            '"limit":20,"max_scrolls":8}\n'
            '  continue content scan: {"operation":"scan_batch","cursor":0,'
            '"batch_limit":10}\n'
            '  exact observed channel: {"operation":"read_channel",'
            '"channel":"observed channel_key"}'
        ),
        agent_rules=(
            "- You own the Discord workflow. Select current_account, discovery, "
            "inventory, navigation, or reading only when the task and latest evidence "
            "justify it. The application preserves your exact selected operation.\n"
            "- If the request says every or all joined servers/channels, complete "
            "inventory, exact coverage, or scan everything, the first operation must "
            "be inventory. find_channels and list_servers are partial discovery and "
            "can never satisfy that scope.\n"
            "- open_channel and read_channel accept only an exact channel result "
            "observed in this task or a query that resolves unambiguously. A "
            "stable channel_key tolerates decorative emoji changes, but fresh "
            "server/channel readback must still match.\n"
            "- A channel name, keyword, or gift icon is discovery evidence only. "
            "A live find_channels result is account-bound and may feed scan_batch "
            "for a fast task-targeted pass; label that scope partial. Use a complete "
            "inventory only when exhaustive joined-server coverage is required. "
            "Use scan_batch with its returned next_cursor to inspect content. Treat "
            "unparsed requirements and incomplete message coverage as blocking "
            "evidence, never as eligibility.\n"
            "- scan_batch may instead receive channels as an array of exact keys or "
            "unambiguous names returned by a current-task inventory, including a "
            "partial inventory. This explicit scope remains partial and every item "
            "is reopened and verified before content is trusted.\n"
            "- inventory validates any cached index against the freshly observed "
            "account and complete server set. Use refresh=true when an exhaustive "
            "channel-index rebuild is specifically required.\n"
        ),
        output_types={
            "status": "string",
            "operation": "string",
            "account": "object|null",
            "servers": "array[discord_server]",
            "channels": "array[discord_channel]",
            "inventory_gaps": "array[discord_inventory_gap]",
            "discovery_candidate_count": "integer|null",
            "candidate_index_size": "integer|null",
            "candidate_server_count": "integer|null",
            "scans": "array[discord_channel_scan]",
            "scan_gaps": "array[discord_scan_gap]",
            "cursor": "integer|null",
            "next_cursor": "integer|null",
            "done": "boolean|null",
            "total_channels": "integer|null",
            "index_scope": "string|null",
            "selected_channel": "discord_channel|null",
            "messages": "array[visible_message]",
            "giveaway_items": "array[discord_giveaway_item]",
            "active_giveaway_items": "array[discord_giveaway_item]",
            "ended_giveaway_items": "array[discord_giveaway_item]",
            "unknown_giveaway_items": "array[discord_giveaway_item]",
            "newest_observed_giveaway": "discord_giveaway_item|null",
            "giveaway_state_counts": "object",
            "unassociated_rule_evidence": "array[discord_rule_evidence]",
            "explicit_state": "string|null",
            "requirements": "array[requirement]",
            "discovery_signals": "object",
            "candidate_classification": "string|null",
            "criteria_status": "string|null",
            "disposition": "string|null",
            "window": "window|null",
            "substeps": "array[discord_inspection_step]",
            "coverage": "object",
            "error": "string|null",
        },
        observation_fields=(
            "operation",
            "account",
            "servers",
            "channels",
            "inventory_gaps",
            "discovery_candidate_count",
            "candidate_index_size",
            "candidate_server_count",
            "scans",
            "scan_gaps",
            "cursor",
            "next_cursor",
            "done",
            "total_channels",
            "index_scope",
            "selected_channel",
            "messages",
            "giveaway_items",
            "active_giveaway_items",
            "ended_giveaway_items",
            "unknown_giveaway_items",
            "newest_observed_giveaway",
            "giveaway_state_counts",
            "unassociated_rule_evidence",
            "explicit_state",
            "requirements",
            "discovery_signals",
            "candidate_classification",
            "criteria_status",
            "disposition",
            "window",
            "substeps",
            "coverage",
            "error",
        ),
        supported_observations=(
            "discord_account",
            "discord_servers",
            "discord_channels",
            "discord_visible_messages",
            "discord_explicit_item_state",
        ),
        requirements=(
            "installed Discord desktop app",
            "currently signed-in account",
            "window control, UI Automation, and target-bound input",
        ),
    ),
}

CAPABILITIES = tuple(CAPABILITY_REGISTRY)
CAPABILITY_DISPLAY_NAMES = {
    capability: descriptor.display_name
    for capability, descriptor in CAPABILITY_REGISTRY.items()
}
CAPABILITY_FIELDS = {
    capability: frozenset(descriptor.argument_types)
    for capability, descriptor in CAPABILITY_REGISTRY.items()
}


def get_capability_descriptor(capability: str) -> CapabilityDescriptor:
    try:
        return CAPABILITY_REGISTRY[str(capability)]
    except KeyError as error:
        raise KeyError(f"Unknown capability descriptor: {capability!r}") from error


__all__ = [
    "APPLICATION_LAUNCH_CAPABILITY",
    "BROWSER_CAPABILITY",
    "DISCORD_INSPECT_CAPABILITY",
    "CAPABILITIES",
    "CAPABILITY_DISPLAY_NAMES",
    "CAPABILITY_FIELDS",
    "CAPABILITY_REGISTRY",
    "CapabilityDescriptor",
    "FILES_CAPABILITY",
    "FILE_OPERATIONS",
    "INPUT_CONTROL_CAPABILITY",
    "SCREEN_CAPTURE_CAPABILITY",
    "TERMINAL_CAPABILITY",
    "UI_AUTOMATION_CAPABILITY",
    "WINDOW_CONTROL_CAPABILITY",
    "get_capability_descriptor",
]
