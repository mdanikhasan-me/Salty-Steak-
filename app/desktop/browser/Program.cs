











using System.Text.Json;
using System.Text.Json.Nodes;
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
        Application.EnableVisualStyles();
        var visible = arguments.Contains("--visible");
        var profile = ReadOption(arguments, "--profile")
            ?? Path.Combine(Path.GetTempPath(), "salty-steak-browser");

        using var pump = new ApplicationContextPump();
        session = new BrowserSession(profile, visible);

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
        void Handler(object? sender, CoreWebView2NavigationCompletedEventArgs args)
        {
            Core.NavigationCompleted -= Handler;
            completed.TrySetResult(args.IsSuccess);
        }

        Core.NavigationCompleted += Handler;
        Core.Navigate(url);
        var reached = await Task.WhenAny(completed.Task, Task.Delay(30_000))
            .ConfigureAwait(true);
        if (reached != completed.Task)
        {
            Core.NavigationCompleted -= Handler;
            throw new TimeoutException($"The page did not finish loading: {url}");
        }

        if (!await completed.Task.ConfigureAwait(true))
        {
            throw new BridgeException("navigation_failed", $"The page could not be opened: {url}");
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
    private readonly bool visible;
    private readonly List<BrowserTab> tabs = new();
    private Form? window;
    private CoreWebView2Environment? environment;
    private BrowserTab? active;
    private int nextTabNumber = 1;

    public BrowserSession(string profileDirectory, bool visible)
    {
        this.profileDirectory = profileDirectory;
        this.visible = visible;
    }

    public async Task<JsonObject> Dispatch(string command, JsonObject payload)
    {
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
                if (active is not null && active != tab)
                {
                    result["active_tab"] = active.Id;
                }

                return result;

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


                window!.ShowInTaskbar = true;
                window.Location = new Point(
                    Math.Max(0, (Screen.PrimaryScreen!.WorkingArea.Width - window.Width) / 2),
                    Math.Max(0, (Screen.PrimaryScreen.WorkingArea.Height - window.Height) / 2));
                window.WindowState = FormWindowState.Normal;
                window.Show();
                window.Activate();
                window.BringToFront();
                return new JsonObject { ["visible"] = true };

            case "hide_window":


                window!.ShowInTaskbar = false;
                window.Location = new Point(-32000, -32000);
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
            ShowInTaskbar = visible,
        };
        if (!visible)
        {


            window.StartPosition = FormStartPosition.Manual;
            window.Location = new Point(-32000, -32000);
        }

        window.Show();
        Activate(await CreateTab().ConfigureAwait(true));
    }
}
