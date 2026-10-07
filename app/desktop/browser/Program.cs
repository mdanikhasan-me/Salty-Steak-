











using System.Text.Json;
using System.Text.Json.Nodes;
using System.Runtime.InteropServices;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace SaltyPotatoAI.Browser;

internal static class Program
{
    private const int DefaultTimeoutMilliseconds = 20_000;

    private static BrowserSession? session;

    [STAThread]
    private static int Main(string[] arguments)
    {
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        var visible = arguments.Contains("--visible");
        var profile = ReadOption(arguments, "--profile")
            ?? Path.Combine(Path.GetTempPath(), "salty-steak-browser");

        using var pump = new ApplicationContextPump();
        session = new BrowserSession(profile, visible, arguments.Contains("--embedded"));

        var reader = new Thread(() => ReadLoop(pump)) { IsBackground = true };
        reader.Start();
        Application.Run(pump.Context);
        return 0;
    }

    private static string? ReadOption(string[] arguments, string name)
    {
        var index = Array.IndexOf(arguments, name);
        return index >= 0 && index + 1 < arguments.Length ? arguments[index + 1] : null;
    }

    private static void ReadLoop(ApplicationContextPump pump)
    {
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
                    Write(Success(id, new JsonObject { ["shutdown"] = true }));
                    pump.Stop();
                    return;
                }

                var timeout = TimeoutFor(payload);



                var work = pump.Invoke(() => session!.Dispatch(command, payload), timeout);
                response = Success(id, work);
            }
            catch (Exception error)
            {
                response = Failure(id, error);
            }

            Write(response);
        }

        pump.Stop();
    }

    private static int TimeoutFor(JsonObject payload) =>
        payload["timeout_ms"] is JsonValue value
        && value.TryGetValue<int>(out var milliseconds)
        && milliseconds is > 0 and <= 120_000
            ? milliseconds
            : DefaultTimeoutMilliseconds;

    private static void Write(JsonObject response)
    {
        Console.WriteLine(response.ToJsonString());
        Console.Out.Flush();
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
            BridgeException bridge => bridge.Kind,
            TimeoutException => "timeout",
            InvalidOperationException => "invalid_request",
            _ => "failed",
        };
        var detail = new JsonObject
        {
            ["kind"] = kind,
            ["type"] = error.GetType().Name,
            ["message"] = error.Message,
        };
        if (error is TimeoutException)
        {


            detail["retire_host"] = true;
        }

        return new JsonObject { ["id"] = id, ["ok"] = false, ["error"] = detail };
    }
}


internal sealed class BridgeException : Exception
{
    public BridgeException(string kind, string message)
        : base(message)
    {
        Kind = kind;
    }

    public string Kind { get; }
}


internal sealed class ApplicationContextPump : IDisposable
{
    private readonly Control marshaller;

    public ApplicationContextPump()
    {
        Context = new ApplicationContext();
        marshaller = new Control();

        _ = marshaller.Handle;
    }

    public ApplicationContext Context { get; }

    public JsonObject Invoke(Func<Task<JsonObject>> work, int timeoutMilliseconds)
    {
        JsonObject? outcome = null;
        Exception? failure = null;
        using var finished = new ManualResetEventSlim(false);

        marshaller.BeginInvoke(new Action(async () =>
        {
            try
            {
                outcome = await work().ConfigureAwait(true);
            }
            catch (Exception error)
            {
                failure = error;
            }
            finally
            {
                finished.Set();
            }
        }));

        if (!finished.Wait(timeoutMilliseconds))
        {
            throw new TimeoutException(
                $"The browser operation did not finish within {timeoutMilliseconds} ms.");
        }

        if (failure is not null)
        {
            throw failure;
        }

        return outcome ?? new JsonObject();
    }

    public void Stop() => marshaller.BeginInvoke(new Action(Context.ExitThread));

    public void Dispose()
    {
        marshaller.Dispose();
        Context.Dispose();
    }
}


internal sealed class BrowserTab
{
    public BrowserTab(string id, WebView2 view)
    {
        Id = id;
        View = view;
    }

    public string Id { get; }

    public WebView2 View { get; }

    public CoreWebView2 Core => View.CoreWebView2;


    public string Qualify(string element) => $"{Id}/{element}";






    public void QualifyHandles(JsonNode? node)
    {
        switch (node)
        {
            case JsonObject item:
                if (item["element"] is JsonValue value
                    && value.TryGetValue<string>(out var handle)
                    && !handle.Contains('/'))
                {
                    item["element"] = Qualify(handle);
                }

                foreach (var property in item.ToList())
                {
                    if (property.Key != "element")
                    {
                        QualifyHandles(property.Value);
                    }
                }

                break;
            case JsonArray list:
                foreach (var entry in list)
                {
                    QualifyHandles(entry);
                }

                break;
        }
    }

    public async Task Navigate(string url)
    {
        var completed = new TaskCompletionSource<bool>();
        ulong? navigationId = null;
        void Started(object? sender, CoreWebView2NavigationStartingEventArgs args)
            => navigationId = args.NavigationId;
        void DomReady(object? sender, CoreWebView2DOMContentLoadedEventArgs args)
        {
            if (navigationId == args.NavigationId) completed.TrySetResult(true);
        }
        void Handler(object? sender, CoreWebView2NavigationCompletedEventArgs args)
        {
            if (navigationId == args.NavigationId) completed.TrySetResult(args.IsSuccess);
        }
        Core.NavigationStarting += Started;
        Core.DOMContentLoaded += DomReady;
        Core.NavigationCompleted += Handler;
        try
        {
            Core.Navigate(url);
            // Readable DOM need not wait for every image, ad or tracking
            // subresource to finish. The agent still observes the actual page.
            var reached = await Task.WhenAny(completed.Task, Task.Delay(25_000))
                .ConfigureAwait(true);
            if (reached != completed.Task)
                throw new TimeoutException($"The page DOM did not become ready: {url}");
            if (!await completed.Task.ConfigureAwait(true))
                throw new BridgeException("navigation_failed", $"The page could not be opened: {url}");
        }
        finally
        {
            Core.NavigationStarting -= Started;
            Core.DOMContentLoaded -= DomReady;
            Core.NavigationCompleted -= Handler;
        }
        await Settle().ConfigureAwait(true);
    }

    public async Task Settle() => await Task.Delay(300).ConfigureAwait(true);

    public async Task<JsonObject> Page()
    {
        var page = await Bridge("window.__salty.page()").ConfigureAwait(true);
        page["tab"] = Id;
        return page;
    }

    public async Task<JsonObject> Bridge(string expression)
    {
        var raw = await Core
            .ExecuteScriptAsync(PageBridge.Call(expression))
            .ConfigureAwait(true);


        var inner = JsonNode.Parse(raw)?.GetValue<string>()
            ?? throw new BridgeException("failed", "The page bridge returned nothing.");
        var envelope = JsonNode.Parse(inner)?.AsObject()
            ?? throw new BridgeException("failed", "The page bridge returned unreadable data.");
        if (envelope["ok"]?.GetValue<bool>() != true)
        {
            var error = envelope["error"]?.AsObject();
            throw new BridgeException(
                error?["kind"]?.GetValue<string>() ?? "failed",
                error?["message"]?.GetValue<string>() ?? "The page operation failed.");
        }



        var value = envelope["value"]?.DeepClone();
        var payload = value is JsonObject existing
            ? existing
            : new JsonObject { ["value"] = value };
        QualifyHandles(payload);
        return payload;
    }
}

internal sealed class BrowserSession
{
    private readonly string profileDirectory;
    private bool surfaceVisible;
    private readonly List<BrowserTab> tabs = new();
    private Form? window;
    private CoreWebView2Environment? environment;
    private BrowserTab? active;
    private int nextTabNumber = 1;
    private IntPtr embeddedParent;
    private bool embeddedVisible;
    private readonly bool preferEmbedded;
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] private static extern IntPtr GetWindowStyle(IntPtr window, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)] private static extern IntPtr SetWindowStyle(IntPtr window, int index, IntPtr value);
    [DllImport("user32.dll")] private static extern IntPtr GetParent(IntPtr window);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int width, int height, uint flags);

    private JsonObject DockSurface(JsonObject payload)
    {
        var parent = new IntPtr(payload["parent_window"]!.GetValue<long>());
        GetWindowThreadProcessId(parent, out var pid);
        if (pid == 0 || pid != payload["parent_pid"]!.GetValue<uint>())
            throw new InvalidOperationException("The embedding window is no longer available.");
        if (payload["visible"]?.GetValue<bool>() != true)
        {
            ShowWindow(window!.Handle, 0); embeddedVisible = false;
            return new JsonObject { ["embedded"] = embeddedParent != IntPtr.Zero, ["visible"] = false };
        }
        if (embeddedParent != parent)
        {
            window!.Hide();
            window.FormBorderStyle = FormBorderStyle.None;
            window.ShowInTaskbar = false;
            var handle = window.Handle;
            var style = GetWindowStyle(handle, -16).ToInt64();
            SetWindowStyle(handle, -16, new IntPtr((style & ~0x80000000L) | 0x40000000L | 0x02000000L));
            SetParent(handle, parent);
            if (GetParent(handle) != parent) throw new InvalidOperationException("Could not embed the browser surface.");
            embeddedParent = parent;
        }
        window!.Show();
        var x = payload["x"]!.GetValue<int>(); var y = payload["y"]!.GetValue<int>();
        var width = payload["width"]!.GetValue<int>(); var height = payload["height"]!.GetValue<int>();
        if (!SetWindowPos(window!.Handle, IntPtr.Zero, x, y, width, height, 0x0040 | 0x0010 | 0x0020))
            throw new InvalidOperationException("Could not position the browser surface.");
        ShowWindow(window.Handle, 5); embeddedVisible = true; surfaceVisible = true;
        return new JsonObject { ["embedded"] = true, ["visible"] = true, ["parent_window"] = parent.ToInt64(), ["window"] = window.Handle.ToInt64(), ["width"] = width, ["height"] = height };
    }

    public BrowserSession(string profileDirectory, bool visible, bool preferEmbedded = false)
    {
        this.profileDirectory = profileDirectory;
        surfaceVisible = visible;
        this.preferEmbedded = preferEmbedded;
    }

    public async Task<JsonObject> Dispatch(string command, JsonObject payload)
    {
        if (command == "verify_html") return await HtmlVerifier.Verify(payload).ConfigureAwait(true);
        if (command == "ping")
        {
            return new JsonObject
            {
                ["ready"] = true,
                ["pid"] = Environment.ProcessId,
                ["started"] = active is not null,
                ["profile"] = profileDirectory,
            };
        }

        await EnsureStarted().ConfigureAwait(true);
        if (command == "dock_surface") return DockSurface(payload);




        var tab = ResolveTab(payload, command);
        var core = tab.Core;

        switch (command)
        {
            case "list_tabs":
                return new JsonObject
                {
                    ["tabs"] = new JsonArray(tabs.Select(Describe).ToArray()),
                    ["count"] = tabs.Count,
                    ["active"] = active!.Id,
                };

            case "get_active_tab":
                return Describe(active!);

            case "get_session_state":
                var state = Describe(active!);
                state["visible"] = IsSurfacePresented();
                return state;

            case "capture_preview":
                using (var captured = new MemoryStream())
                {
                    await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, captured)
                        .ConfigureAwait(true);
                    captured.Position = 0;
                    using var source = System.Drawing.Image.FromStream(captured);
                    var scale = Math.Min(1.0, Math.Min(1280.0 / source.Width, 900.0 / source.Height));
                    var width = Math.Max(1, (int)(source.Width * scale));
                    var height = Math.Max(1, (int)(source.Height * scale));
                    using var bitmap = new Bitmap(width, height);
                    using (var graphics = Graphics.FromImage(bitmap))
                    {
                        graphics.DrawImage(source, 0, 0, width, height);
                    }
                    using var png = new MemoryStream();
                    bitmap.Save(png, System.Drawing.Imaging.ImageFormat.Png);
                    var preview = Describe(tab);
                    preview["width"] = width;
                    preview["height"] = height;
                    preview["png_base64"] = Convert.ToBase64String(png.ToArray());
                    return preview;
                }

            case "new_tab":
                var created = await CreateTab().ConfigureAwait(true);
                Activate(created);
                var address = payload["url"]?.GetValue<string>();
                if (!string.IsNullOrWhiteSpace(address))
                {
                    await created.Navigate(RequireWebAddress(address)).ConfigureAwait(true);
                }

                return Describe(created);

            case "switch_tab":
                Activate(tab);
                return Describe(tab);

            case "close_tab":
                return CloseTab(tab);

            case "open_url":
            case "navigate":
                var target = payload["url"]?.GetValue<string>()
                    ?? throw new InvalidOperationException("A url is required.");
                await tab.Navigate(RequireWebAddress(target)).ConfigureAwait(true);
                return await tab.Page().ConfigureAwait(true);

            case "get_url":
            case "get_title":
            case "get_page":
                return await tab.Page().ConfigureAwait(true);

            case "read_page":
                return await tab.Bridge($"window.__salty.read({payload.ToJsonString()})")
                    .ConfigureAwait(true);

            case "wait_for":
                var waitOptions = (JsonObject)payload.DeepClone();
                waitOptions["visible"] = true;
                waitOptions["enabled"] = true;
                var waitLimit = Math.Clamp(payload["wait_timeout_ms"]?.GetValue<int>() ?? 5000, 0, 10000);
                var waitStarted = System.Diagnostics.Stopwatch.StartNew();
                JsonObject observed;
                do
                {
                    observed = await tab.Bridge($"window.__salty.query({waitOptions.ToJsonString()})").ConfigureAwait(true);
                    if ((observed["count"]?.GetValue<int>() ?? 0) > 0 || waitStarted.ElapsedMilliseconds >= waitLimit) break;
                    await Task.Delay(100).ConfigureAwait(true);
                } while (true);
                observed["timed_out"] = (observed["count"]?.GetValue<int>() ?? 0) == 0;
                observed["waited_ms"] = (int)waitStarted.ElapsedMilliseconds;
                observed["tab"] = tab.Id;
                return observed;

            case "get_media":
                return await tab.Bridge("window.__salty.media()")
                    .ConfigureAwait(true);

            case "query":
            case "find_element":
                return await tab.Bridge($"window.__salty.query({payload.ToJsonString()})")
                    .ConfigureAwait(true);

            case "get_element":
                return await tab.Bridge(
                    $"window.__salty.get({JsonSerializer.Serialize(LocalHandle(payload))})")
                    .ConfigureAwait(true);

            case "click":
            case "set_value":
            case "select":
            case "focus":
            case "scroll":
            case "submit":
                var handle = LocalHandle(payload);
                var result = await tab.Bridge(
                    $"window.__salty.act({JsonSerializer.Serialize(handle)}, "
                    + $"{JsonSerializer.Serialize(command)}, {payload.ToJsonString()})")
                    .ConfigureAwait(true);



                await Task.Delay(350).ConfigureAwait(true);
                result["tab"] = tab.Id;
                result["action_dispatched"] = true;
                // A dispatched action is not proof of the user's requested
                // outcome. Return fresh evidence without replaying the action
                // if navigation makes the immediate read temporarily fail.
                try
                {
                    var after = await tab.Bridge("window.__salty.read({text_limit:1200,limit:12})").ConfigureAwait(true);
                    result["after_state"] = after;
                }
                catch (Exception error)
                {
                    result["observation_error"] = "Action was dispatched; read the page again before deciding what to do next: " + error.Message;
                }
                if (active is not null && active != tab)
                {
                    result["active_tab"] = active.Id;
                }

                return result;

            case "play_media":
            case "pause_media":
                var mediaHandle = payload["element"] is null
                    ? ""
                    : LocalHandle(payload);
                await tab.Bridge(
                    $"window.__salty.mediaAct({JsonSerializer.Serialize(mediaHandle)}, "
                    + $"{JsonSerializer.Serialize(command == "play_media" ? "play" : "pause")})")
                    .ConfigureAwait(true);
                await Task.Delay(600).ConfigureAwait(true);
                return await tab.Bridge("window.__salty.media()")
                    .ConfigureAwait(true);

            case "back":
                if (core.CanGoBack) { core.GoBack(); await tab.Settle().ConfigureAwait(true); }
                return await tab.Page().ConfigureAwait(true);

            case "forward":
                if (core.CanGoForward) { core.GoForward(); await tab.Settle().ConfigureAwait(true); }
                return await tab.Page().ConfigureAwait(true);

            case "reload":
                core.Reload();
                await tab.Settle().ConfigureAwait(true);
                return await tab.Page().ConfigureAwait(true);

            case "show_window":
                Activate(tab);
                if (embeddedParent != IntPtr.Zero)
                    return new JsonObject { ["visible"] = IsSurfacePresented(), ["embedded"] = true };
                if (preferEmbedded)
                    return new JsonObject { ["visible"] = false, ["embedded_requested"] = true };


                window!.ShowInTaskbar = true;
                window.Location = new Point(
                    Math.Max(0, (Screen.PrimaryScreen!.WorkingArea.Width - window.Width) / 2),
                    Math.Max(0, (Screen.PrimaryScreen.WorkingArea.Height - window.Height) / 2));
                window.WindowState = FormWindowState.Normal;
                window.Show();
                window.Activate();
                window.BringToFront();
                surfaceVisible = true;
                return new JsonObject { ["visible"] = true };

            case "hide_window":
                if (embeddedParent != IntPtr.Zero)
                {
                    ShowWindow(window!.Handle, 0); embeddedVisible = false;
                    return new JsonObject { ["visible"] = false, ["embedded"] = true };
                }


                window!.ShowInTaskbar = false;
                window.Location = new Point(-32000, -32000);
                surfaceVisible = false;
                return new JsonObject { ["visible"] = false };

            default:
                throw new InvalidOperationException($"Unknown browser command: {command}");
        }
    }

    private static string RequireWebAddress(string target)
    {
        if (!Uri.TryCreate(target, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {


            throw new InvalidOperationException(
                "Only http and https addresses can be opened.");
        }

        return uri.AbsoluteUri;
    }


    private static string LocalHandle(JsonObject payload)
    {
        var handle = payload["element"]?.GetValue<string>()
            ?? throw new InvalidOperationException("This command needs an element handle.");
        var separator = handle.LastIndexOf('/');
        return separator < 0 ? handle : handle[(separator + 1)..];
    }








    private BrowserTab ResolveTab(JsonObject payload, string command)
    {
        var named = payload["tab"]?.GetValue<string>();
        var element = payload["element"]?.GetValue<string>();
        string? owner = null;
        if (!string.IsNullOrEmpty(element))
        {
            var separator = element.LastIndexOf('/');
            if (separator > 0)
            {
                owner = element[..separator];
            }
        }

        if (named is not null && owner is not null && named != owner)
        {
            throw new BridgeException(
                "wrong_tab",
                $"That element belongs to {owner}, not {named}.");
        }

        var wanted = named ?? owner;
        if (wanted is null)
        {
            return active!;
        }

        var found = tabs.FirstOrDefault(candidate => candidate.Id == wanted);
        if (found is null)
        {


            throw new BridgeException(
                owner is not null ? "stale_element" : "unknown_tab",
                $"{wanted} is not open.");
        }

        return found;
    }

    private static JsonObject Describe(BrowserTab tab)
    {
        var core = tab.Core;
        return new JsonObject
        {
            ["tab"] = tab.Id,
            ["url"] = core.Source,
            ["title"] = core.DocumentTitle,
        };
    }

    private bool IsSurfacePresented()
    {
        if (embeddedParent != IntPtr.Zero)
            return embeddedVisible && window is not null && IsWindowVisible(window.Handle) && IsWindowVisible(embeddedParent);
        if (window is null || !surfaceVisible || !window.Visible || !window.ShowInTaskbar
            || window.WindowState == FormWindowState.Minimized)
        {
            return false;
        }

        return Screen.AllScreens.Any(screen => screen.WorkingArea.IntersectsWith(window.Bounds));
    }

    private void Activate(BrowserTab tab)
    {
        active = tab;
        foreach (var candidate in tabs)
        {



            candidate.View.Visible = candidate == tab;
        }

        tab.View.BringToFront();
    }

    private JsonObject CloseTab(BrowserTab tab)
    {
        if (tabs.Count == 1)
        {
            throw new InvalidOperationException(
                "The last tab cannot be closed; the session would have no page.");
        }

        tabs.Remove(tab);
        window!.Controls.Remove(tab.View);
        tab.View.Dispose();
        if (active == tab)
        {
            Activate(tabs[^1]);
        }

        return new JsonObject
        {
            ["closed"] = tab.Id,
            ["active"] = active!.Id,
            ["count"] = tabs.Count,
        };
    }

    private async Task<BrowserTab> CreateTab(CoreWebView2? adopted = null)
    {
        var view = new WebView2 { Dock = DockStyle.Fill, Visible = false };
        window!.Controls.Add(view);
        var tab = new BrowserTab($"web-tab-{nextTabNumber++}", view);
        await view.EnsureCoreWebView2Async(environment).ConfigureAwait(true);
        await Prepare(tab).ConfigureAwait(true);
        tabs.Add(tab);
        return tab;
    }

    private async Task Prepare(BrowserTab tab)
    {
        var core = tab.Core;
        core.Settings.AreDefaultContextMenusEnabled = false;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreDevToolsEnabled = false;


        await core.AddScriptToExecuteOnDocumentCreatedAsync(PageBridge.Script)
            .ConfigureAwait(true);
        // The initial about:blank document already exists before registration.
        // A first read/query must be valid before any navigation as well.
        await core.ExecuteScriptAsync(PageBridge.Script).ConfigureAwait(true);




        core.NewWindowRequested += async (_, args) =>
        {
            var deferral = args.GetDeferral();
            try
            {
                var opened = await CreateTab().ConfigureAwait(true);
                args.NewWindow = opened.Core;
                args.Handled = true;
                Activate(opened);
            }
            catch (Exception)
            {
                args.Handled = false;
            }
            finally
            {
                deferral.Complete();
            }
        };


        core.WindowCloseRequested += (_, _) =>
        {
            if (tabs.Contains(tab) && tabs.Count > 1)
            {
                CloseTab(tab);
            }
        };
    }

    private async Task EnsureStarted()
    {
        if (active is not null)
        {
            return;
        }

        Directory.CreateDirectory(profileDirectory);


        environment = await CoreWebView2Environment
            .CreateAsync(userDataFolder: profileDirectory)
            .ConfigureAwait(true);

        window = new Form
        {
            Text = "Salty Steak Browser",
            ClientSize = new Size(1280, 900),
            StartPosition = FormStartPosition.CenterScreen,
            ShowInTaskbar = surfaceVisible,
        };
        if (!surfaceVisible)
        {


            window.StartPosition = FormStartPosition.Manual;
            window.Location = new Point(-32000, -32000);
        }

        window.Show();
        Activate(await CreateTab().ConfigureAwait(true));
    }
}
