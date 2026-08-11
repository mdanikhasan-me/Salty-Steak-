












using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows.Automation;

namespace SaltyPotatoAI.Uia;

internal static class Program
{
    private const int DefaultTimeoutMilliseconds = 10_000;
    private const int MaxTreeNodes = 400;





    private const int MaxTreeDepth = 20;
    private const int MaxTextLength = 20_000;
    private const int MaxCollectedItems = 5_000;
    private const int SwShow = 5;
    private const int SwRestore = 9;

    private static readonly ElementRegistry Registry = new();

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool BringWindowToTop(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, IntPtr processId);

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint attach, uint attachTo, bool value);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();






    private static int abandonedOperations;

    private static int Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.InputEncoding = System.Text.Encoding.UTF8;

        string? line;
        while ((line = Console.ReadLine()) is not null)
        {
            if (line.Length == 0)
            {
                continue;
            }

            string? id = null;
            JsonObject response;
            try
            {
                var request = JsonNode.Parse(line)?.AsObject()
                    ?? throw new InvalidOperationException("Request is not a JSON object.");
                id = request["id"]?.GetValue<string>();
                var command = request["command"]?.GetValue<string>()
                    ?? throw new InvalidOperationException("Request has no command.");
                var payload = request["payload"]?.AsObject() ?? new JsonObject();

                if (command == "shutdown")
                {
                    Console.WriteLine(
                        Success(id, new JsonObject { ["shutdown"] = true }).ToJsonString());
                    Console.Out.Flush();
                    return 0;
                }

                var result = RunWithTimeout(
                    () => Dispatch(command, payload),
                    TimeoutFor(payload));
                response = Success(id, result);
            }
            catch (Exception error)
            {
                response = Failure(id, error);
            }

            Console.WriteLine(response.ToJsonString());
            Console.Out.Flush();
        }

        return 0;
    }

    private static int TimeoutFor(JsonObject payload)
    {
        if (payload["timeout_ms"] is JsonValue value
            && value.TryGetValue<int>(out var milliseconds)
            && milliseconds is > 0 and <= 120_000)
        {
            return milliseconds;
        }

        return DefaultTimeoutMilliseconds;
    }






    private static JsonObject RunWithTimeout(Func<JsonObject> work, int timeoutMilliseconds)
    {
        JsonObject? outcome = null;
        Exception? failure = null;

        var thread = new Thread(() =>
        {
            try
            {
                outcome = work();
            }
            catch (Exception error)
            {
                failure = error;
            }
        });
        thread.IsBackground = true;
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();

        if (!thread.Join(timeoutMilliseconds))
        {









            var abandoned = Interlocked.Increment(ref abandonedOperations);
            throw new HungOperationException(
                $"The UI Automation operation did not finish within {timeoutMilliseconds} ms. "
                + "The target application may be busy or not responding.",
                abandoned);
        }

        if (failure is not null)
        {
            throw failure;
        }

        return outcome ?? new JsonObject();
    }

    private static JsonObject Dispatch(string command, JsonObject payload) => command switch
    {
        "ping" => new JsonObject
        {
            ["ready"] = true,
            ["pid"] = Environment.ProcessId,
            ["abandoned_operations"] = Volatile.Read(ref abandonedOperations),
        },


        "stall" => Stall(payload),
        "get_windows" => GetWindows(),
        "get_active_window" => GetActiveWindow(),
        "get_tree" => GetTree(payload),
        "find_control" => FindControl(payload),
        "collect_list" => CollectList(payload),
        "get_properties" => GetProperties(payload),
        "get_text" => GetText(payload),
        "focus" => Act(payload, "focus"),
        "invoke" => Act(payload, "invoke"),
        "set_value" => Act(payload, "set_value"),
        "select" => Act(payload, "select"),
        "toggle" => Act(payload, "toggle"),
        "expand" => Act(payload, "expand"),
        "collapse" => Act(payload, "collapse"),
        "scroll" => Act(payload, "scroll"),
        _ => throw new InvalidOperationException($"Unknown UI Automation command: {command}"),
    };



    private static JsonObject Stall(JsonObject payload)
    {
        if (Environment.GetEnvironmentVariable("SALTY_UIA_ALLOW_STALL") != "1")
        {
            throw new InvalidOperationException("The stall command is not enabled.");
        }



        Thread.Sleep(Clamp(payload["seconds"], 30, 1, 300) * 1000);
        return new JsonObject { ["stalled"] = true };
    }

    private static JsonObject GetWindows()
    {
        var windows = new JsonArray();
        foreach (AutomationElement window in AutomationElement.RootElement.FindAll(
            TreeScope.Children, Condition.TrueCondition))
        {
            try
            {
                if (string.IsNullOrWhiteSpace(window.Current.Name))
                {
                    continue;
                }

                windows.Add(Describe(window, includeHandle: true));
            }
            catch (ElementNotAvailableException)
            {

            }
        }

        return new JsonObject { ["windows"] = windows, ["count"] = windows.Count };
    }

    private static JsonObject GetActiveWindow()
    {
        var focused = AutomationElement.FocusedElement;
        var window = AncestorWindow(focused) ?? focused;
        return new JsonObject { ["window"] = Describe(window, includeHandle: true) };
    }

    private static AutomationElement? AncestorWindow(AutomationElement element)
    {
        var walker = TreeWalker.ControlViewWalker;
        var current = element;
        for (var depth = 0; current is not null && depth < 32; depth++)
        {
            try
            {
                if (current.Current.ControlType == ControlType.Window)
                {
                    return current;
                }

                current = walker.GetParent(current);
            }
            catch (ElementNotAvailableException)
            {
                return null;
            }
        }

        return null;
    }

    private static AutomationElement ResolveScope(JsonObject payload)
    {
        if (payload["element"]?.GetValue<string>() is { Length: > 0 } handle)
        {
            return Registry.Resolve(handle);
        }

        if (payload["window_handle"] is JsonValue raw && raw.TryGetValue<long>(out var native))
        {
            var element = AutomationElement.FromHandle(new IntPtr(native));
            if (element is null)
            {
                throw new InvalidOperationException($"No window has handle {native}.");
            }

            return element;
        }




        if (payload["process_id"] is JsonValue processValue
            && processValue.TryGetValue<int>(out var processId))
        {
            var owned = new List<AutomationElement>();
            foreach (AutomationElement window in AutomationElement.RootElement.FindAll(
                TreeScope.Children, Condition.TrueCondition))
            {
                try
                {
                    if (window.Current.ProcessId == processId)
                    {
                        owned.Add(window);
                    }
                }
                catch (ElementNotAvailableException)
                {
                }
            }

            if (owned.Count == 0)
            {
                throw new InvalidOperationException(
                    $"Process {processId} has no top-level window.");
            }

            if (owned.Count > 1 && payload["window"]?.GetValue<string>() is { Length: > 0 } narrowing)
            {
                var narrowed = owned
                    .Where(item => Safe(() => item.Current.Name)
                        .Contains(narrowing, StringComparison.OrdinalIgnoreCase))
                    .ToList();
                if (narrowed.Count == 1)
                {
                    return narrowed[0];
                }
            }

            if (owned.Count > 1)
            {
                throw new AmbiguousMatchException(
                    $"Process {processId} has {owned.Count} windows: "
                    + string.Join("; ", owned.Take(5).Select(item => Safe(() => item.Current.Name))));
            }

            return owned[0];
        }

        if (payload["window"]?.GetValue<string>() is { Length: > 0 } title)
        {
            var matches = new List<AutomationElement>();
            foreach (AutomationElement window in AutomationElement.RootElement.FindAll(
                TreeScope.Children, Condition.TrueCondition))
            {
                try
                {
                    var name = window.Current.Name;
                    if (!string.IsNullOrWhiteSpace(name)
                        && name.Contains(title, StringComparison.OrdinalIgnoreCase))
                    {
                        matches.Add(window);
                    }
                }
                catch (ElementNotAvailableException)
                {
                }
            }

            var exact = matches.FirstOrDefault(item =>
                string.Equals(item.Current.Name, title, StringComparison.OrdinalIgnoreCase));
            if (exact is not null)
            {
                return exact;
            }

            if (matches.Count == 1)
            {
                return matches[0];
            }

            if (matches.Count > 1)
            {
                throw new AmbiguousMatchException(
                    $"{matches.Count} windows match '{title}': "
                    + string.Join("; ", matches.Take(5).Select(item => Safe(() => item.Current.Name))));
            }

            throw new InvalidOperationException($"No window title contains '{title}'.");
        }

        return AutomationElement.RootElement;
    }



    private static JsonObject GetTree(JsonObject payload)
    {
        var scope = ResolveScope(payload);
        var depth = Clamp(payload["depth"], 3, 1, MaxTreeDepth);
        var budget = Clamp(payload["max_nodes"], 120, 1, MaxTreeNodes);
        var visited = 0;
        var truncated = false;
        var root = BuildNode(scope, depth, ref visited, budget, ref truncated);
        return new JsonObject
        {
            ["tree"] = root,
            ["node_count"] = visited,


            ["truncated"] = truncated,
        };
    }

    private static JsonObject BuildNode(
        AutomationElement element,
        int remainingDepth,
        ref int visited,
        int budget,
        ref bool truncated)
    {
        var node = Describe(element, includeHandle: true);
        visited++;
        if (remainingDepth <= 0)
        {
            return node;
        }

        var children = new JsonArray();
        try
        {
            foreach (AutomationElement child in element.FindAll(
                TreeScope.Children, Condition.TrueCondition))
            {
                if (visited >= budget)
                {
                    truncated = true;
                    break;
                }

                try
                {
                    children.Add(BuildNode(child, remainingDepth - 1, ref visited, budget, ref truncated));
                }
                catch (ElementNotAvailableException)
                {
                }
            }
        }
        catch (ElementNotAvailableException)
        {
        }

        if (children.Count > 0)
        {
            node["children"] = children;
        }

        return node;
    }



    private static JsonObject FindControl(JsonObject payload)
    {
        var scope = ResolveScope(payload);
        var name = payload["name"]?.GetValue<string>();
        var automationId = payload["automation_id"]?.GetValue<string>();
        var controlType = payload["control_type"]?.GetValue<string>();
        var className = payload["class_name"]?.GetValue<string>();
        var exact = payload["exact"]?.GetValue<bool>() ?? false;
        var enabledOnly = payload["enabled_only"]?.GetValue<bool>() ?? true;
        var visibleOnly = payload["visible_only"]?.GetValue<bool>() ?? true;
        var limit = Clamp(payload["limit"], 10, 1, 50);



        var pattern = payload["pattern"]?.GetValue<string>();

        if (name is null && automationId is null && controlType is null
            && className is null && pattern is null)
        {
            throw new InvalidOperationException(
                "A search needs at least one of name, automation_id, control_type, "
                + "class_name, or pattern.");
        }

        var matches = new List<AutomationElement>();
        var nativeCondition = NativeSearchCondition(
            name,
            exact,
            automationId,
            controlType,
            className,
            enabledOnly,
            visibleOnly);
        foreach (AutomationElement candidate in scope.FindAll(
            TreeScope.Descendants, nativeCondition))
        {
            try
            {
                var info = candidate.Current;
                if (enabledOnly && !info.IsEnabled)
                {
                    continue;
                }

                if (visibleOnly && info.IsOffscreen)
                {
                    continue;
                }

                if (automationId is not null
                    && !string.Equals(info.AutomationId, automationId, StringComparison.Ordinal))
                {
                    continue;
                }

                if (className is not null
                    && !string.Equals(info.ClassName, className, StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                if (controlType is not null
                    && !string.Equals(
                        info.ControlType.ProgrammaticName.Replace("ControlType.", string.Empty),
                        controlType,
                        StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                if (name is not null && !NameMatches(info.Name, name, exact))
                {
                    continue;
                }

                if (pattern is not null && !SupportsPattern(candidate, pattern))
                {
                    continue;
                }

                matches.Add(candidate);
                if (matches.Count >= limit)
                {
                    break;
                }
            }
            catch (ElementNotAvailableException)
            {
            }
        }



        if (name is not null && !exact)
        {
            var precise = matches
                .Where(item => string.Equals(
                    Safe(() => item.Current.Name), name, StringComparison.OrdinalIgnoreCase))
                .ToList();
            if (precise.Count > 0)
            {
                matches = precise;
            }
        }

        var results = new JsonArray();
        foreach (var match in matches)
        {
            results.Add(Describe(match, includeHandle: true));
        }

        return new JsonObject
        {
            ["matches"] = results,
            ["count"] = results.Count,


            ["ambiguous"] = results.Count > 1,
        };
    }

    private static JsonObject CollectList(JsonObject payload)
    {
        var scopeName = payload["scope_name"]?.GetValue<string>()
            ?? throw new InvalidOperationException("collect_list needs scope_name.");
        var scopeControlType = payload["scope_control_type"]?.GetValue<string>()
            ?? "Window";
        var scopeExact = payload["scope_exact"]?.GetValue<bool>() ?? true;
        var itemControlType = payload["item_control_type"]?.GetValue<string>()
            ?? "ListItem";
        var scrollerName = payload["scroller_name"]?.GetValue<string>() ?? string.Empty;
        var limit = Clamp(payload["limit"], 500, 1, MaxCollectedItems);
        var maxScrolls = Clamp(payload["max_scrolls"], 30, 0, 100);
        var delay = Clamp(payload["post_scroll_delay_ms"], 60, 0, 500);
        var reverse = payload["reverse"]?.GetValue<bool>() ?? false;
        var found = new Dictionary<string, JsonObject>(StringComparer.OrdinalIgnoreCase);
        var orderedKeys = new List<string>();
        var boundaryReached = false;
        string? boundaryBasis = null;
        double? lastPercent = null;
        var pagesRead = 0;

        for (var scrollIndex = 0; scrollIndex <= maxScrolls; scrollIndex++)
        {
            pagesRead++;
            var scope = FindNamedDescendant(
                ResolveScope(payload), scopeName, scopeControlType, scopeExact);
            var pageKeys = new List<string>();
            foreach (AutomationElement item in scope.FindAll(
                TreeScope.Descendants,
                new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.ListItem)))
            {
                try
                {
                    var info = item.Current;
                    var typeName = info.ControlType.ProgrammaticName
                        .Replace("ControlType.", string.Empty);
                    if (!string.Equals(typeName, itemControlType, StringComparison.OrdinalIgnoreCase)
                        || string.IsNullOrWhiteSpace(info.Name))
                    {
                        continue;
                    }



                    if (found.TryAdd(info.Name, Describe(item, includeHandle: true)))
                    {
                        pageKeys.Add(info.Name);
                    }
                    if (found.Count >= limit)
                    {
                        break;
                    }
                }
                catch (ElementNotAvailableException)
                {
                }
            }







            if (reverse)
            {
                orderedKeys.InsertRange(0, pageKeys);
            }
            else
            {
                orderedKeys.AddRange(pageKeys);
            }

            if (found.Count >= limit || scrollIndex >= maxScrolls)
            {
                break;
            }

            var scroller = FindScroller(scope, scrollerName);
            if (scroller is null
                || !scroller.TryGetCurrentPattern(ScrollPattern.Pattern, out var rawScroll))
            {
                boundaryBasis = "scroll_pattern_unavailable";
                break;
            }
            var scroll = (ScrollPattern)rawScroll;
            var current = scroll.Current;
            if (!current.VerticallyScrollable)
            {
                boundaryReached = true;
                boundaryBasis = "scroll_pattern_not_scrollable";
                break;
            }
            lastPercent = current.VerticalScrollPercent;
            if ((!reverse && lastPercent >= 99.999) || (reverse && lastPercent <= 0.001))
            {
                boundaryReached = true;
                boundaryBasis = reverse ? "vertical_percent_0" : "vertical_percent_100";
                break;
            }
            var increment = Math.Max(5.0, current.VerticalViewSize * 0.9);
            var nextPercent = reverse
                ? Math.Max(0.0, lastPercent.Value - increment)
                : Math.Min(100.0, lastPercent.Value + increment);
            scroll.SetScrollPercent(ScrollPattern.NoScroll, nextPercent);
            lastPercent = nextPercent;
            if (delay > 0)
            {
                Thread.Sleep(delay);
            }
        }

        var items = new JsonArray();
        foreach (var key in orderedKeys)
        {
            items.Add(found[key]);
        }
        return new JsonObject
        {
            ["items"] = items,
            ["count"] = items.Count,
            ["pages_read"] = pagesRead,
            ["scroll_boundary_reached"] = boundaryReached,
            ["boundary_basis"] = boundaryBasis,
            ["last_vertical_percent"] = lastPercent,
            ["truncated_by_limit"] = found.Count >= limit,
        };
    }

    private static AutomationElement FindNamedDescendant(
        AutomationElement root,
        string name,
        string controlType,
        bool exact)
    {
        var matches = new List<AutomationElement>();
        var nativeCondition = NativeSearchCondition(
            name,
            exact,
            automationId: null,
            controlType,
            className: null,
            enabledOnly: false,
            visibleOnly: false);
        foreach (AutomationElement candidate in root.FindAll(
            TreeScope.Descendants, nativeCondition))
        {
            try
            {
                var info = candidate.Current;
                var typeName = info.ControlType.ProgrammaticName
                    .Replace("ControlType.", string.Empty);
                if ((exact
                        ? string.Equals(info.Name, name, StringComparison.OrdinalIgnoreCase)
                        : info.Name.Contains(name, StringComparison.OrdinalIgnoreCase))
                    && string.Equals(typeName, controlType, StringComparison.OrdinalIgnoreCase))
                {
                    matches.Add(candidate);
                }
            }
            catch (ElementNotAvailableException)
            {
            }
        }
        return matches.Count switch
        {
            1 => matches[0],
            0 => throw new InvalidOperationException(
                $"No {controlType} is named '{name}'."),
            _ => throw new AmbiguousMatchException(
                $"{matches.Count} {controlType} controls are named '{name}'."),
        };
    }









    private static Condition NativeSearchCondition(
        string? name,
        bool exact,
        string? automationId,
        string? controlType,
        string? className,
        bool enabledOnly,
        bool visibleOnly)
    {
        var conditions = new List<Condition>();
        if (name is not null && exact)
        {
            conditions.Add(new PropertyCondition(
                AutomationElement.NameProperty,
                name,
                PropertyConditionFlags.IgnoreCase));
        }
        if (automationId is not null)
        {
            conditions.Add(new PropertyCondition(
                AutomationElement.AutomationIdProperty,
                automationId));
        }
        if (className is not null)
        {
            conditions.Add(new PropertyCondition(
                AutomationElement.ClassNameProperty,
                className,
                PropertyConditionFlags.IgnoreCase));
        }
        var nativeControlType = ResolveControlType(controlType);
        if (nativeControlType is not null)
        {
            conditions.Add(new PropertyCondition(
                AutomationElement.ControlTypeProperty,
                nativeControlType));
        }
        if (enabledOnly)
        {
            conditions.Add(new PropertyCondition(
                AutomationElement.IsEnabledProperty,
                true));
        }
        if (visibleOnly)
        {
            conditions.Add(new PropertyCondition(
                AutomationElement.IsOffscreenProperty,
                false));
        }
        return conditions.Count switch
        {
            0 => Condition.TrueCondition,
            1 => conditions[0],
            _ => new AndCondition(conditions.ToArray()),
        };
    }

    private static ControlType? ResolveControlType(string? value)
    {
        return value?.Trim().ToLowerInvariant() switch
        {
            "button" => ControlType.Button,
            "checkbox" => ControlType.CheckBox,
            "combobox" => ControlType.ComboBox,
            "custom" => ControlType.Custom,
            "dataitem" => ControlType.DataItem,
            "document" => ControlType.Document,
            "edit" => ControlType.Edit,
            "group" => ControlType.Group,
            "header" => ControlType.Header,
            "headeritem" => ControlType.HeaderItem,
            "hyperlink" => ControlType.Hyperlink,
            "image" => ControlType.Image,
            "list" => ControlType.List,
            "listitem" => ControlType.ListItem,
            "menu" => ControlType.Menu,
            "menubar" => ControlType.MenuBar,
            "menuitem" => ControlType.MenuItem,
            "pane" => ControlType.Pane,
            "radiobutton" => ControlType.RadioButton,
            "scrollbar" => ControlType.ScrollBar,
            "separator" => ControlType.Separator,
            "statusbar" => ControlType.StatusBar,
            "tab" => ControlType.Tab,
            "tabitem" => ControlType.TabItem,
            "text" => ControlType.Text,
            "toolbar" => ControlType.ToolBar,
            "tree" => ControlType.Tree,
            "treeitem" => ControlType.TreeItem,
            "window" => ControlType.Window,
            _ => null,
        };
    }

    private static AutomationElement? FindScroller(
        AutomationElement scope,
        string wantedName)
    {
        var candidates = new List<AutomationElement>();
        if (scope.TryGetCurrentPattern(ScrollPattern.Pattern, out _))
        {
            candidates.Add(scope);
        }
        foreach (AutomationElement candidate in scope.FindAll(
            TreeScope.Descendants, Condition.TrueCondition))
        {
            try
            {
                if (candidate.TryGetCurrentPattern(ScrollPattern.Pattern, out _))
                {
                    candidates.Add(candidate);
                }
            }
            catch (ElementNotAvailableException)
            {
            }
        }
        if (!string.IsNullOrWhiteSpace(wantedName))
        {
            var named = candidates.FirstOrDefault(candidate => string.Equals(
                Safe(() => candidate.Current.Name),
                wantedName,
                StringComparison.OrdinalIgnoreCase));
            if (named is not null)
            {
                return named;
            }
        }
        return candidates.FirstOrDefault();
    }

    private static bool SupportsPattern(AutomationElement element, string wanted)
    {
        try
        {
            foreach (var supported in element.GetSupportedPatterns())
            {
                var name = supported.ProgrammaticName
                    .Replace("PatternIdentifiers.", string.Empty)
                    .Replace("Pattern", string.Empty);
                if (string.Equals(name, wanted, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
        }
        catch (ElementNotAvailableException)
        {
        }

        return false;
    }

    private static bool NameMatches(string? actual, string wanted, bool exact)
    {
        if (string.IsNullOrEmpty(actual))
        {
            return false;
        }

        return exact
            ? string.Equals(actual, wanted, StringComparison.OrdinalIgnoreCase)
            : actual.Contains(wanted, StringComparison.OrdinalIgnoreCase);
    }

    private static JsonObject GetProperties(JsonObject payload)
    {
        var element = Registry.Resolve(RequireHandle(payload));
        return new JsonObject { ["element"] = Describe(element, includeHandle: true, verbose: true) };
    }

    private static JsonObject GetText(JsonObject payload)
    {
        var element = Registry.Resolve(RequireHandle(payload));
        string? text = null;
        if (element.TryGetCurrentPattern(ValuePattern.Pattern, out var valueRaw))
        {
            text = ((ValuePattern)valueRaw).Current.Value;
        }
        else if (element.TryGetCurrentPattern(TextPattern.Pattern, out var textRaw))
        {
            text = ((TextPattern)textRaw).DocumentRange.GetText(MaxTextLength);
        }
        else
        {
            text = Safe(() => element.Current.Name);
        }

        return new JsonObject
        {
            ["text"] = text ?? string.Empty,
            ["length"] = text?.Length ?? 0,
        };
    }



    private static JsonObject Act(JsonObject payload, string action)
    {




        var element = action == "focus"
            && payload["element"]?.GetValue<string>() is not { Length: > 0 }
            ? ResolveScope(payload)
            : Registry.Resolve(RequireHandle(payload));
        var performed = new JsonObject { ["action"] = action };

        switch (action)
        {
            case "focus":
                FocusElement(element, performed);
                break;

            case "invoke":
                if (element.TryGetCurrentPattern(InvokePattern.Pattern, out var invoke))
                {
                    ((InvokePattern)invoke).Invoke();
                }
                else if (element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out var selectable))
                {


                    ((SelectionItemPattern)selectable).Select();
                    performed["fallback_pattern"] = "SelectionItem";
                }
                else if (element.TryGetCurrentPattern(TogglePattern.Pattern, out var toggle))
                {
                    ((TogglePattern)toggle).Toggle();
                    performed["fallback_pattern"] = "Toggle";
                }
                else
                {
                    throw new UnsupportedPatternException(element, "Invoke");
                }

                break;

            case "set_value":
                var value = payload["value"]?.GetValue<string>()
                    ?? throw new InvalidOperationException("set_value needs a value.");
                if (!element.TryGetCurrentPattern(ValuePattern.Pattern, out var setter))
                {
                    throw new UnsupportedPatternException(element, "Value");
                }

                var pattern = (ValuePattern)setter;
                if (pattern.Current.IsReadOnly)
                {
                    throw new InvalidOperationException("The control is read-only.");
                }

                element.SetFocus();
                pattern.SetValue(value);
                performed["value"] = value;
                break;

            case "select":
                if (!element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out var selection))
                {
                    throw new UnsupportedPatternException(element, "SelectionItem");
                }

                ((SelectionItemPattern)selection).Select();
                break;

            case "toggle":
                if (!element.TryGetCurrentPattern(TogglePattern.Pattern, out var toggling))
                {
                    throw new UnsupportedPatternException(element, "Toggle");
                }

                var toggler = (TogglePattern)toggling;
                toggler.Toggle();
                performed["state"] = toggler.Current.ToggleState.ToString();
                break;

            case "expand":
            case "collapse":
                if (!element.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out var expander))
                {
                    throw new UnsupportedPatternException(element, "ExpandCollapse");
                }

                var expandCollapse = (ExpandCollapsePattern)expander;
                if (action == "expand")
                {
                    expandCollapse.Expand();
                }
                else
                {
                    expandCollapse.Collapse();
                }

                performed["state"] = expandCollapse.Current.ExpandCollapseState.ToString();
                break;

            case "scroll":
                var amount = payload["amount"]?.GetValue<double>() ?? 1.0;
                var horizontal = payload["horizontal"]?.GetValue<bool>() ?? false;
                var percent = payload["vertical_percent"]?.GetValue<double?>();
                if (percent is < 0 or > 100)
                {
                    throw new InvalidOperationException(
                        "vertical_percent must be between 0 and 100.");
                }
                if (element.TryGetCurrentPattern(ScrollPattern.Pattern, out var scrolling))
                {
                    var scroller = (ScrollPattern)scrolling;
                    if (percent is not null)
                    {
                        scroller.SetScrollPercent(
                            horizontal ? percent.Value : ScrollPattern.NoScroll,
                            horizontal ? ScrollPattern.NoScroll : percent.Value);
                        performed["requested_vertical_percent"] = percent.Value;
                    }
                    else
                    {
                        var step = amount >= 0
                            ? ScrollAmount.LargeIncrement
                            : ScrollAmount.LargeDecrement;
                        scroller.Scroll(
                            horizontal ? step : ScrollAmount.NoAmount,
                            horizontal ? ScrollAmount.NoAmount : step);
                    }
                    performed["scroll"] = DescribeScroll(scroller);
                }
                else if (element.TryGetCurrentPattern(ScrollItemPattern.Pattern, out var intoView))
                {
                    ((ScrollItemPattern)intoView).ScrollIntoView();
                    performed["fallback_pattern"] = "ScrollItem";
                }
                else
                {
                    throw new UnsupportedPatternException(element, "Scroll");
                }

                break;

            default:
                throw new InvalidOperationException($"Unknown action: {action}");
        }



        performed["element"] = Describe(element, includeHandle: true);
        performed["succeeded"] = true;
        return performed;
    }

    private static void FocusElement(AutomationElement element, JsonObject performed)
    {
        var info = element.Current;
        var native = new IntPtr(info.NativeWindowHandle);
        if (info.ControlType != ControlType.Window || native == IntPtr.Zero)
        {
            element.SetFocus();
            return;
        }

        var wasVisible = IsWindowVisible(native);
        var wasMinimized = IsIconic(native);
        ShowWindow(native, wasMinimized ? SwRestore : SwShow);

        var currentThread = GetCurrentThreadId();
        var foreground = GetForegroundWindow();
        var foregroundThread = foreground == IntPtr.Zero
            ? 0
            : GetWindowThreadProcessId(foreground, IntPtr.Zero);
        var targetThread = GetWindowThreadProcessId(native, IntPtr.Zero);
        var attached = new List<uint>();
        foreach (var thread in new[] { foregroundThread, targetThread }.Distinct())
        {
            if (thread != 0 && thread != currentThread
                && AttachThreadInput(currentThread, thread, true))
            {
                attached.Add(thread);
            }
        }

        bool requested;
        try
        {
            BringWindowToTop(native);
            requested = SetForegroundWindow(native);
        }
        finally
        {
            foreach (var thread in attached.AsEnumerable().Reverse())
            {
                AttachThreadInput(currentThread, thread, false);
            }
        }

        Thread.Sleep(80);
        var visibleAfter = IsWindowVisible(native);
        var foregroundAfter = GetForegroundWindow() == native;
        if (!visibleAfter)
        {
            throw new InvalidOperationException("Windows did not restore the requested window.");
        }
        if (!foregroundAfter)
        {
            throw new InvalidOperationException(
                "Windows restored the requested window but did not focus it.");
        }
        performed["was_visible"] = wasVisible;
        performed["was_minimized"] = wasMinimized;
        performed["foreground_requested"] = requested;
        performed["foreground_after"] = foregroundAfter;
    }



    private static JsonObject Describe(
        AutomationElement element,
        bool includeHandle = false,
        bool verbose = false)
    {
        var node = new JsonObject();
        try
        {
            var info = element.Current;
            node["name"] = info.Name ?? string.Empty;
            node["control_type"] = info.ControlType.ProgrammaticName.Replace("ControlType.", string.Empty);
            node["automation_id"] = info.AutomationId ?? string.Empty;
            node["class_name"] = info.ClassName ?? string.Empty;
            node["enabled"] = info.IsEnabled;
            node["offscreen"] = info.IsOffscreen;
            node["keyboard_focusable"] = info.IsKeyboardFocusable;
            node["focused"] = info.HasKeyboardFocus;
            var rectangle = info.BoundingRectangle;
            if (!rectangle.IsEmpty)
            {
                node["bounds"] = new JsonObject
                {
                    ["x"] = (int)rectangle.X,
                    ["y"] = (int)rectangle.Y,
                    ["width"] = (int)rectangle.Width,
                    ["height"] = (int)rectangle.Height,
                };
            }

            if (info.NativeWindowHandle != 0)
            {
                node["window_handle"] = info.NativeWindowHandle;
            }



            node["process_id"] = info.ProcessId;

            var patterns = new JsonArray();
            foreach (var supported in element.GetSupportedPatterns())
            {
                patterns.Add(supported.ProgrammaticName
                    .Replace("PatternIdentifiers.", string.Empty)
                    .Replace("Pattern", string.Empty));
            }

            node["patterns"] = patterns;

            if (verbose)
            {
                node["process_id"] = info.ProcessId;
                node["framework"] = info.FrameworkId ?? string.Empty;
                node["is_content"] = info.IsContentElement;
                node["is_control"] = info.IsControlElement;
                if (element.TryGetCurrentPattern(ScrollPattern.Pattern, out var scrollRaw))
                {
                    node["scroll"] = DescribeScroll((ScrollPattern)scrollRaw);
                }
            }
        }
        catch (ElementNotAvailableException)
        {
            node["stale"] = true;
        }

        if (includeHandle)
        {
            node["element"] = Registry.Register(element);
        }

        return node;
    }

    private static JsonObject DescribeScroll(ScrollPattern scroll)
    {
        var current = scroll.Current;
        return new JsonObject
        {
            ["horizontally_scrollable"] = current.HorizontallyScrollable,
            ["vertical_scrollable"] = current.VerticallyScrollable,
            ["horizontal_percent"] = current.HorizontalScrollPercent,
            ["vertical_percent"] = current.VerticalScrollPercent,
            ["horizontal_view_size"] = current.HorizontalViewSize,
            ["vertical_view_size"] = current.VerticalViewSize,
        };
    }



    private static string RequireHandle(JsonObject payload) =>
        payload["element"]?.GetValue<string>()
        ?? throw new InvalidOperationException("This command needs an element handle.");

    private static int Clamp(JsonNode? node, int fallback, int minimum, int maximum)
    {
        if (node is JsonValue value && value.TryGetValue<int>(out var parsed))
        {
            return Math.Clamp(parsed, minimum, maximum);
        }

        return fallback;
    }

    private static string Safe(Func<string?> read)
    {
        try
        {
            return read() ?? string.Empty;
        }
        catch (ElementNotAvailableException)
        {
            return string.Empty;
        }
    }

    private static JsonObject Success(string? id, JsonObject result) => new()
    {
        ["id"] = id,
        ["ok"] = true,
        ["result"] = result,
    };

    private static JsonObject Failure(string? id, Exception error)
    {


        var kind = error switch
        {
            ElementNotAvailableException => "stale_element",
            UnsupportedPatternException => "unsupported_pattern",
            AmbiguousMatchException => "ambiguous",
            HungOperationException => "timeout",
            TimeoutException => "timeout",
            UnauthorizedAccessException => "access_denied",
            InvalidOperationException => "invalid_request",
            _ => "failed",
        };

        var detail = new JsonObject
        {
            ["kind"] = kind,
            ["type"] = error.GetType().Name,
            ["message"] = error.Message,
        };
        if (error is HungOperationException hung)
        {


            detail["abandoned_operations"] = hung.AbandonedOperations;
            detail["retire_host"] = true;
        }

        return new JsonObject
        {
            ["id"] = id,
            ["ok"] = false,
            ["error"] = detail,
        };
    }
}







internal sealed class HungOperationException : TimeoutException
{
    public HungOperationException(string message, int abandonedOperations)
        : base(message)
    {
        AbandonedOperations = abandonedOperations;
    }

    public int AbandonedOperations { get; }
}


internal sealed class UnsupportedPatternException : Exception
{
    public UnsupportedPatternException(AutomationElement element, string pattern)
        : base(BuildMessage(element, pattern))
    {
    }

    private static string BuildMessage(AutomationElement element, string pattern)
    {
        string name;
        string type;
        try
        {
            name = element.Current.Name ?? string.Empty;
            type = element.Current.ControlType.ProgrammaticName.Replace("ControlType.", string.Empty);
        }
        catch (ElementNotAvailableException)
        {
            name = "(unavailable)";
            type = "(unavailable)";
        }

        return string.Format(
            CultureInfo.InvariantCulture,
            "The {0} control '{1}' does not support the {2} pattern. "
            + "A different control, or a different capability, is required.",
            type,
            name,
            pattern);
    }
}


internal sealed class AmbiguousMatchException : Exception
{
    public AmbiguousMatchException(string message)
        : base(message)
    {
    }
}








internal sealed class ElementRegistry
{
    private readonly ConcurrentDictionary<string, AutomationElement> entries = new();
    private long sequence;

    public string Register(AutomationElement element)
    {
        var handle = "el-" + Interlocked.Increment(ref sequence).ToString(CultureInfo.InvariantCulture);
        entries[handle] = element;
        return handle;
    }

    public AutomationElement Resolve(string handle)
    {
        if (!entries.TryGetValue(handle, out var element))
        {
            throw new ElementNotAvailableException(
                $"Element {handle} is no longer known to this session. Find it again.");
        }

        try
        {

            _ = element.Current.ControlType;
        }
        catch (ElementNotAvailableException)
        {
            entries.TryRemove(handle, out _);
            throw;
        }

        return element;
    }
}
