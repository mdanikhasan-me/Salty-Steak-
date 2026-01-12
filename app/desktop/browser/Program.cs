











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

internal sealed class BrowserSession
{
    private readonly string profileDirectory;
    private readonly bool visible;
    private Form? window;
    private WebView2? view;
    private CoreWebView2Environment? environment;

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
                ["started"] = view is not null,
                ["profile"] = profileDirectory,
            };
        }

        await EnsureStarted().ConfigureAwait(true);
        var core = view!.CoreWebView2;

        switch (command)
        {
            case "open_url":
            case "navigate":
                var target = payload["url"]?.GetValue<string>()
                    ?? throw new InvalidOperationException("A url is required.");
                if (!Uri.TryCreate(target, UriKind.Absolute, out var uri)
                    || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
                {


                    throw new InvalidOperationException(
                        "Only http and https addresses can be opened.");
                }

                await Navigate(uri.AbsoluteUri).ConfigureAwait(true);
                return await Page().ConfigureAwait(true);

            case "get_url":
            case "get_title":
            case "get_page":
                return await Page().ConfigureAwait(true);

            case "read_page":
                return await Bridge($"window.__salty.read({payload.ToJsonString()})")
                    .ConfigureAwait(true);

            case "query":
            case "find_element":
                return await Bridge($"window.__salty.query({payload.ToJsonString()})")
                    .ConfigureAwait(true);

            case "get_element":
                return await Bridge(
                    $"window.__salty.get({JsonSerializer.Serialize(RequireHandle(payload))})")
                    .ConfigureAwait(true);

            case "click":
            case "set_value":
            case "select":
            case "focus":
            case "scroll":
            case "submit":
                var handle = RequireHandle(payload);
                var result = await Bridge(
                    $"window.__salty.act({JsonSerializer.Serialize(handle)}, "
                    + $"{JsonSerializer.Serialize(command)}, {payload.ToJsonString()})")
                    .ConfigureAwait(true);


                await Task.Delay(250).ConfigureAwait(true);
                return result;

            case "back":
                if (core.CanGoBack) { core.GoBack(); await Settle().ConfigureAwait(true); }
                return await Page().ConfigureAwait(true);

            case "forward":
                if (core.CanGoForward) { core.GoForward(); await Settle().ConfigureAwait(true); }
                return await Page().ConfigureAwait(true);

            case "reload":
                core.Reload();
                await Settle().ConfigureAwait(true);
                return await Page().ConfigureAwait(true);

            default:
                throw new InvalidOperationException($"Unknown browser command: {command}");
        }
    }

    private static string RequireHandle(JsonObject payload) =>
        payload["element"]?.GetValue<string>()
        ?? throw new InvalidOperationException("This command needs an element handle.");

    private async Task EnsureStarted()
    {
        if (view is not null)
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
        view = new WebView2 { Dock = DockStyle.Fill };
        window.Controls.Add(view);
        if (visible)
        {
            window.Show();
        }
        else
        {


            window.StartPosition = FormStartPosition.Manual;
            window.Location = new Point(-32000, -32000);
            window.Show();
        }

        await view.EnsureCoreWebView2Async(environment).ConfigureAwait(true);
        var core = view.CoreWebView2;
        core.Settings.AreDefaultContextMenusEnabled = false;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.AreDevToolsEnabled = false;


        await core.AddScriptToExecuteOnDocumentCreatedAsync(PageBridge.Script)
            .ConfigureAwait(true);
    }

    private async Task Navigate(string url)
    {
        var core = view!.CoreWebView2;
        var completed = new TaskCompletionSource<bool>();
        void Handler(object? sender, CoreWebView2NavigationCompletedEventArgs args)
        {
            core.NavigationCompleted -= Handler;
            completed.TrySetResult(args.IsSuccess);
        }

        core.NavigationCompleted += Handler;
        core.Navigate(url);
        var reached = await Task.WhenAny(completed.Task, Task.Delay(30_000))
            .ConfigureAwait(true);
        if (reached != completed.Task)
        {
            core.NavigationCompleted -= Handler;
            throw new TimeoutException($"The page did not finish loading: {url}");
        }

        if (!await completed.Task.ConfigureAwait(true))
        {
            throw new BridgeException("navigation_failed", $"The page could not be opened: {url}");
        }

        await Settle().ConfigureAwait(true);
    }

    private async Task Settle() => await Task.Delay(300).ConfigureAwait(true);

    private async Task<JsonObject> Page()
    {
        var page = await Bridge("window.__salty.page()").ConfigureAwait(true);
        return page;
    }

    private async Task<JsonObject> Bridge(string expression)
    {
        var raw = await view!.CoreWebView2
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
        return value is JsonObject payload
            ? payload
            : new JsonObject { ["value"] = value };
    }
}
