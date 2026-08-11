using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Python.Runtime;

namespace SaltyPotatoAI.Desktop;

internal static class Program
{
    private static readonly string SingleInstanceName =
        @"Local\SaltyPotatoAI.Desktop.2."
        + Convert.ToHexString(
            SHA256.HashData(Encoding.UTF8.GetBytes(AppContext.BaseDirectory))
        )[..16];

    [STAThread]
    private static int Main()
    {
        WorkspaceSelection.ApplyCommandLineOverride();
        StartupTimeline.Mark("native_entry");
        using var singleInstance = new Mutex(true, SingleInstanceName, out var ownsInstance);
        StartupTimeline.Mark(ownsInstance ? "single_instance_acquired" : "single_instance_detected");
        if (!ownsInstance)
        {
            NativeWindowIdentity.ActivateExistingWindow();
            StartupTimeline.Mark("existing_instance_activation_requested");
            return 0;
        }

        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        StartupTimeline.Mark("native_ui_thread_ready");



        using var loading = new StartupWindowV2();
        StartupTimeline.Mark("startup_window_created");
        Application.Run(loading);
        StartupTimeline.Mark("native_ui_loop_completed");




        NativeProcessExit.Terminate(0);
        return 0;
    }
}

internal static class NativeProcessExit
{
    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    public static void Terminate(uint exitCode)
    {
        if (!TerminateProcess(GetCurrentProcess(), exitCode))
        {
            Environment.FailFast(
                $"Windows could not complete the cleaned Salty Steak process exit: {Marshal.GetLastWin32Error()}"
            );
        }
    }
}

internal static class WorkspaceSelection
{
    public static string? CommandLineArgument()
    {
        var arguments = Environment.GetCommandLineArgs();
        for (var index = 1; index < arguments.Length; index += 1)
        {
            if (
                !string.Equals(
                    arguments[index],
                    "--workspace",
                    StringComparison.OrdinalIgnoreCase
                )
            )
            {
                continue;
            }

            if (index + 1 >= arguments.Length || string.IsNullOrWhiteSpace(arguments[index + 1]))
            {
                throw new ArgumentException("--workspace requires a directory path.");
            }

            return Path.GetFullPath(arguments[index + 1]);
        }

        return null;
    }

    public static void ApplyCommandLineOverride()
    {
        var workspace = CommandLineArgument();
        if (!string.IsNullOrWhiteSpace(workspace))
        {
            Environment.SetEnvironmentVariable("SALTY_POTATO_WORKSPACE", workspace);
        }
    }
}

internal static class StartupTimeline
{
    private static readonly Stopwatch Clock = Stopwatch.StartNew();
    private static readonly object Gate = new();
    private static readonly List<(string Phase, double Milliseconds)> Pending = [];
    private static string? timelinePath;

    public static void Mark(string phase)
    {
        var milliseconds = Math.Round(Clock.Elapsed.TotalMilliseconds, 3);
        lock (Gate)
        {
            if (timelinePath is null)
            {
                Pending.Add((phase, milliseconds));
                return;
            }
            Write(timelinePath, phase, milliseconds);
        }
    }

    public static void Configure(string workspace)
    {
        lock (Gate)
        {
            var directory = Path.Combine(workspace, "logs");
            Directory.CreateDirectory(directory);
            timelinePath = Path.Combine(directory, "startup-timeline.jsonl");
            foreach (var entry in Pending)
            {
                Write(timelinePath, entry.Phase, entry.Milliseconds);
            }
            Pending.Clear();
        }
    }

    private static void Write(string path, string phase, double milliseconds)
    {
        try
        {
            var record = JsonSerializer.Serialize(new
            {
                timestamp_utc = DateTime.UtcNow.ToString("O"),
                build_id = BuildIdentity.Value,
                event_name = "startup_timeline",
                phase,
                elapsed_milliseconds = milliseconds,
            });
            File.AppendAllText(path, record + Environment.NewLine, Encoding.UTF8);
        }
        catch
        {

        }
    }
}

internal static class ProjectLocation
{
    public static string Find()
    {
        var executable = Environment.ProcessPath ?? Application.ExecutablePath;
        var installedRoot = Path.GetDirectoryName(executable)!;
        if (IsProjectRoot(installedRoot))
        {
            return Path.GetFullPath(installedRoot);
        }

        throw new DirectoryNotFoundException(
            "The application package is incomplete. Keep Salty Steak.exe beside its app, config, and .venv folders."
        );
    }

    private static bool IsProjectRoot(string path)
    {
        return File.Exists(Path.Combine(path, "config", "defaults.toml"))
            && File.Exists(Path.Combine(path, "app", "backend", "server.py"))
            && Directory.Exists(Path.Combine(path, ".venv"));
    }
}

internal static class BuildIdentity
{
    public static string Value { get; private set; } = "development";

    public static void Load(string projectRoot)
    {
        var manifest = Path.Combine(projectRoot, "package.json");
        if (!File.Exists(manifest))
        {
            return;
        }
        using var document = System.Text.Json.JsonDocument.Parse(
            File.ReadAllText(manifest, Encoding.UTF8)
        );
        if (
            document.RootElement.TryGetProperty("build_id", out var build)
            && !string.IsNullOrWhiteSpace(build.GetString())
        )
        {
            Value = build.GetString()!;
        }
    }
}

internal sealed class EmbeddedBackend : IDisposable
{
    private readonly string projectRoot;
    private readonly Action<string>? stageChanged;
    private readonly ManualResetEventSlim started = new(false);
    private readonly ManualResetEventSlim stopRequested = new(false);
    private PyModule? scope;
    private IntPtr pythonLibraryHandle;
    private IntPtr releasedThreadState;
    private Thread? hostThread;
    private Exception? startupError;
    private bool initialized;
    private bool stopped;

    public EmbeddedBackend(string projectRoot, Action<string>? stageChanged = null)
    {
        this.projectRoot = projectRoot;
        this.stageChanged = stageChanged;
    }

    public string Url { get; private set; } = string.Empty;

    public void Start()
    {
        StartupTimeline.Mark("backend_thread_creating");
        hostThread = new Thread(Run)
        {
            IsBackground = true,
            Name = "Salty Steak Python host"
        };
        hostThread.Start();
        StartupTimeline.Mark("backend_thread_started");
        started.Wait();
        if (startupError is not null)
        {
            throw new InvalidOperationException(
                "The local application service did not start.",
                startupError
            );
        }
    }

    private void Run()
    {
        try
        {
        stageChanged?.Invoke("Starting local engine…");
        StartupTimeline.Mark("python_host_initialization_started");
        var environment = PythonEnvironment.Read(projectRoot);
        Environment.SetEnvironmentVariable("VIRTUAL_ENV", environment.VirtualEnvironment);
        Environment.SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1");
        Environment.SetEnvironmentVariable("PYTHONUTF8", "1");
        Environment.SetEnvironmentVariable(
            "PATH",
            string.Join(
                Path.PathSeparator,
                Path.Combine(environment.VirtualEnvironment, "Scripts"),
                environment.PythonHome,
                Environment.GetEnvironmentVariable("PATH") ?? string.Empty
            )
        );

        pythonLibraryHandle = NativeLibrary.Load(environment.PythonLibrary);
        if (
            !NativeLibrary.TryGetExport(
                pythonLibraryHandle,
                "Py_DontWriteBytecodeFlag",
                out var dontWriteBytecodeFlag
            )
        )
        {
            throw new InvalidOperationException(
                "The private Python runtime does not expose Py_DontWriteBytecodeFlag."
            );
        }
        Marshal.WriteInt32(dontWriteBytecodeFlag, 1);
        Runtime.PythonDLL = environment.PythonLibrary;
        PythonEngine.ProgramName = Path.Combine(
            environment.VirtualEnvironment,
            "Scripts",
            "python.exe"
        );
        PythonEngine.PythonHome = environment.PythonHome;
        PythonEngine.PythonPath = string.Join(
            Path.PathSeparator,
            this.projectRoot,
            Path.Combine(environment.VirtualEnvironment, "Lib", "site-packages"),
            Path.Combine(environment.PythonHome, "Lib"),
            Path.Combine(environment.PythonHome, "DLLs")
        );

        PythonEngine.Initialize();
        StartupTimeline.Mark("python_host_initialized");
        initialized = true;
        using (Py.GIL())
        {
            stageChanged?.Invoke("Opening workspace…");
            StartupTimeline.Mark("python_application_import_started");
            scope = Py.CreateScope("salty_potato_desktop");
            var workspace = Environment.GetEnvironmentVariable(
                "SALTY_POTATO_WORKSPACE"
            ) ?? throw new InvalidOperationException(
                "The writable application workspace was not configured."
            );
            scope.Set("project_root", projectRoot);
            scope.Set("virtual_environment", environment.VirtualEnvironment);
            scope.Set("workspace", workspace);
            scope.Set("build_id", BuildIdentity.Value);
            scope.Set("startup_mark", new Action<string>(StartupTimeline.Mark));
            scope.Exec(
                """
                import sys
                sys.dont_write_bytecode = True
                import os
                os.environ["VIRTUAL_ENV"] = virtual_environment
                os.environ["SALTY_POTATO_WORKSPACE"] = workspace
                os.environ["SALTY_POTATO_BUILD_ID"] = build_id
                from app.backend.server import start_server
                server_handle = start_server(
                    project_root=project_root,
                    port=0,
                    startup_mark=startup_mark,
                )
                server_url = server_handle.url
                """
            );
            StartupTimeline.Mark("python_application_server_ready");
            using var serverUrl = scope.Get("server_url");
            Url = serverUrl.ToString() ?? string.Empty;
        }

        if (string.IsNullOrWhiteSpace(Url))
        {
            throw new InvalidOperationException("The local application service did not start.");
        }

        releasedThreadState = PythonEngine.BeginAllowThreads();
            StartupTimeline.Mark("backend_ready");
            started.Set();
            stopRequested.Wait();
            StartupTimeline.Mark("backend_stop_signal_received");
            StartupTimeline.Mark("backend_stop_gil_wait_started");
            using (Py.GIL())
            {
                StartupTimeline.Mark("backend_stop_gil_acquired");
                StartupTimeline.Mark("python_application_server_stop_started");
                StopServer();
                StartupTimeline.Mark("python_application_server_stop_completed");
            }
            StartupTimeline.Mark("python_thread_state_restore_started");
            PythonEngine.EndAllowThreads(releasedThreadState);
            releasedThreadState = IntPtr.Zero;
            StartupTimeline.Mark("python_thread_state_restore_completed");
        }
        catch (Exception error)
        {
            startupError = error;
            DesktopDiagnostics.Write(projectRoot, "backend_host_failed", error);
            started.Set();
        }
        finally
        {
            StartupTimeline.Mark("python_scope_dispose_started");
            scope?.Dispose();
            scope = null;
            StartupTimeline.Mark("python_scope_dispose_completed");
            if (initialized)
            {
                if (stopped)
                {






                    StartupTimeline.Mark("python_shutdown_deferred_to_process_exit");
                    initialized = false;
                    pythonLibraryHandle = IntPtr.Zero;
                }
                else
                {
                    try
                    {
                        StartupTimeline.Mark("python_shutdown_started");
                        PythonEngine.Shutdown();
                        StartupTimeline.Mark("python_shutdown_completed");
                    }
                    catch (Exception error)
                    {
                        DesktopDiagnostics.Write(projectRoot, "python_shutdown_failed", error);
                        pythonLibraryHandle = IntPtr.Zero;
                    }
                    finally
                    {
                        initialized = false;
                    }
                }
            }
            if (pythonLibraryHandle != IntPtr.Zero)
            {
                NativeLibrary.Free(pythonLibraryHandle);
                pythonLibraryHandle = IntPtr.Zero;
            }
        }
    }

    public void Dispose()
    {
        Stop();
    }

    private void Stop()
    {
        if (stopped)
        {
            return;
        }
        stopped = true;

        if (!initialized)
        {
            return;
        }

        StartupTimeline.Mark("backend_stop_requested");
        stopRequested.Set();
        StartupTimeline.Mark("backend_stop_join_started");
        if (hostThread is not null && !hostThread.Join(TimeSpan.FromSeconds(5)))
        {
            StartupTimeline.Mark("backend_stop_join_timed_out");
            DesktopDiagnostics.Write(
                projectRoot,
                "backend_stop_timed_out",
                new TimeoutException(
                    "The embedded backend did not stop within 5 seconds; "
                    + "the background host will be reclaimed with the process."
                )
            );
            return;
        }
        StartupTimeline.Mark("backend_stop_join_completed");
    }

    private void StopServer()
    {
        scope?.Exec("server_handle.stop(timeout=120.0)");
    }
}

internal sealed class StartupWindow : Form
{
    private readonly string projectRoot;
    private readonly EmbeddedBackend backend;
    private readonly Label stage;
    private readonly Label elapsed;
    private readonly Stopwatch stopwatch = Stopwatch.StartNew();
    private readonly System.Windows.Forms.Timer timer;
    private MainWindow? mainWindow;

    public StartupWindow(string projectRoot, EmbeddedBackend backend)
    {
        this.projectRoot = projectRoot;
        this.backend = backend;
        Text = "Starting Salty Steak";
        Name = "SaltyPotatoStartupWindow";
        AccessibleName = "Starting Salty Steak";
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(560, 310);
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        BackColor = Color.FromArgb(21, 23, 21);

        var title = new Label
        {
            AutoSize = true,
            Font = new Font("Segoe UI", 24, FontStyle.Bold),
            ForeColor = Color.FromArgb(35, 32, 28),
            Location = new Point(48, 58),
            Text = "Salty Steak"
        };
        stage = new Label
        {
            AutoSize = true,
            Font = new Font("Segoe UI", 12),
            ForeColor = Color.FromArgb(93, 78, 62),
            Location = new Point(52, 132),
            Text = "Starting the private application service…"
        };
        elapsed = new Label
        {
            AutoSize = true,
            Font = new Font("Segoe UI", 9),
            ForeColor = Color.FromArgb(112, 105, 96),
            Location = new Point(52, 169),
            Text = "Elapsed 0 seconds"
        };
        Controls.Add(title);
        Controls.Add(stage);
        Controls.Add(elapsed);
        timer = new System.Windows.Forms.Timer { Interval = 1000 };
        timer.Tick += (_, _) =>
            elapsed.Text = $"Elapsed {(int)stopwatch.Elapsed.TotalSeconds} seconds";
        Shown += BeginStartup;
    }

    protected override void OnHandleCreated(EventArgs eventArgs)
    {
        base.OnHandleCreated(eventArgs);
        NativeWindowIdentity.UseDarkTitleBar(Handle);
    }

    private async void BeginStartup(object? sender, EventArgs eventArgs)
    {
        timer.Start();
        try
        {
            await Task.Run(backend.Start);
            stage.Text = "Opening your workspace…";
            DesktopDiagnostics.WriteTiming(
                projectRoot,
                "backend_ready",
                stopwatch.Elapsed.TotalSeconds
            );
            mainWindow = new MainWindow(
                projectRoot,
                backend.Url,
                () =>
                {
                    timer.Stop();
                    DesktopDiagnostics.WriteTiming(
                        projectRoot,
                        "interface_ready",
                        stopwatch.Elapsed.TotalSeconds
                    );
                    Hide();
                }
            );
            mainWindow.FormClosed += (_, _) => Close();
            mainWindow.Show(this);
        }
        catch (Exception error)
        {
            timer.Stop();
            DesktopDiagnostics.Write(projectRoot, "startup_failed", error);
            MessageBox.Show(
                this,
                $"Salty Steak could not open.\n\n{error.Message}",
                "Salty Steak",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            Close();
        }
    }
}

internal sealed record PythonEnvironment(
    string VirtualEnvironment,
    string PythonHome,
    string PythonLibrary
)
{
    public static PythonEnvironment Read(string projectRoot)
    {
        var virtualEnvironment = Path.Combine(projectRoot, ".venv");
        var configurationPath = Path.Combine(virtualEnvironment, "pyvenv.cfg");
        if (!File.Exists(configurationPath))
        {
            throw new FileNotFoundException(
                "The private application runtime is missing.",
                configurationPath
            );
        }

        var values = File.ReadLines(configurationPath)
            .Select(line => line.Split('=', 2, StringSplitOptions.TrimEntries))
            .Where(parts => parts.Length == 2)
            .ToDictionary(parts => parts[0], parts => parts[1], StringComparer.OrdinalIgnoreCase);
        if (!values.TryGetValue("home", out var configuredPythonHome)
            || !values.TryGetValue("version", out var version))
        {
            throw new InvalidDataException("The private application runtime is incomplete.");
        }






        var packagedPythonHome = Path.GetFullPath(Path.Combine(projectRoot, ".python"));
        var pythonHome = Directory.Exists(packagedPythonHome)
            ? packagedPythonHome
            : Path.IsPathRooted(configuredPythonHome)
                ? Path.GetFullPath(configuredPythonHome)
                : Path.GetFullPath(Path.Combine(projectRoot, configuredPythonHome));

        var match = Regex.Match(version, @"^(?<major>\d+)\.(?<minor>\d+)");
        if (!match.Success)
        {
            throw new InvalidDataException("The private application runtime version is invalid.");
        }

        var library = Path.Combine(
            pythonHome,
            $"python{match.Groups["major"].Value}{match.Groups["minor"].Value}.dll"
        );
        if (!File.Exists(library))
        {
            throw new FileNotFoundException(
                "The private application runtime library is missing.",
                library
            );
        }

        return new PythonEnvironment(virtualEnvironment, pythonHome, library);
    }
}

internal sealed class MainWindow : Form
{
    private readonly string projectRoot;
    private readonly Uri applicationUri;
    private readonly WebView2 browser;
    private bool browserReady;
    private CoreWebView2Environment? webViewEnvironment;
    private readonly Action interfaceReady;
    private bool saveInProgress;

    public MainWindow(string projectRoot, string applicationUrl, Action interfaceReady)
    {
        this.projectRoot = Path.GetFullPath(projectRoot);
        applicationUri = new Uri(applicationUrl, UriKind.Absolute);
        this.interfaceReady = interfaceReady;
        Text = "\u200B";
        Name = "SaltyPotatoMainWindow";
        AccessibleName = "Salty Steak";
        StartPosition = FormStartPosition.CenterScreen;






        AutoScaleDimensions = new SizeF(96f, 96f);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(1360, 860);


        MinimumSize = new Size(960, 640);
        BackColor = Color.FromArgb(29, 29, 28);

        var iconPath = Path.Combine(
            projectRoot,
            "app",
            "frontend",
            "public",
            "assets",
            "salty-potato.ico"
        );
        if (File.Exists(iconPath))
        {
            Icon = new Icon(iconPath);
        }

        browser = new WebView2
        {
            Dock = DockStyle.Fill,
            DefaultBackgroundColor = BackColor
        };
        StartupTimeline.Mark("webview_control_object_created");
        Controls.Add(browser);
        Shown += OpenApplication;
    }

    protected override void OnHandleCreated(EventArgs eventArgs)
    {
        base.OnHandleCreated(eventArgs);
        NativeWindowIdentity.MarkWindow(Handle);
        NativeWindowIdentity.UseDarkTitleBar(Handle);
    }

    protected override bool ProcessCmdKey(ref Message message, Keys keys)
    {
        if (keys == (Keys.Control | Keys.R) || keys == Keys.F5)
        {
            return true;
        }
        return base.ProcessCmdKey(ref message, keys);
    }

    private async void OpenApplication(object? sender, EventArgs eventArgs)
    {
        if (browserReady)
        {
            return;
        }

        try
        {
            var dataRoot = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Salty Steak",
                "WebView2",
                Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(this.projectRoot)))[..16]
            );
            Directory.CreateDirectory(dataRoot);
            StartupTimeline.Mark("webview_environment_creation_started");
            var environment = await CoreWebView2Environment.CreateAsync(
                userDataFolder: dataRoot
            );
            webViewEnvironment = environment;
            Environment.SetEnvironmentVariable(
                "SALTY_POTATO_WEBVIEW2_VERSION",
                environment.BrowserVersionString
            );
            StartupTimeline.Mark("webview_environment_created");
            StartupTimeline.Mark("webview_control_creation_started");
            await browser.EnsureCoreWebView2Async(environment);
            StartupTimeline.Mark("webview_control_created");
            ConfigureBrowser(browser.CoreWebView2);
            browserReady = true;
            browser.CoreWebView2.NavigationCompleted += (_, navigation) =>
            {
                if (navigation.IsSuccess)
                {
                    StartupTimeline.Mark("frontend_first_paint");
                    StartupTimeline.Mark("active_version_resolution_deferred");
                    StartupTimeline.Mark("cuda_initialization_deferred");
                    StartupTimeline.Mark("model_weight_loading_deferred");
                    StartupTimeline.Mark("runtime_warmup_deferred");
                    interfaceReady();
                }
            };
            StartupTimeline.Mark("frontend_navigation_started");
            browser.Source = applicationUri;
        }
        catch (Exception error)
        {
            DesktopDiagnostics.Write(projectRoot, "webview_initialization_failed", error);
            MessageBox.Show(
                this,
                $"The Salty Steak interface could not open.\n\n{error.Message}",
                "Salty Steak",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            Close();
        }
    }

    private void ConfigureBrowser(CoreWebView2 core)
    {
        core.Settings.AreDevToolsEnabled = false;


        core.Settings.AreDefaultContextMenusEnabled = true;
        core.Settings.AreBrowserAcceleratorKeysEnabled = false;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.IsZoomControlEnabled = false;
        core.Settings.IsGeneralAutofillEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false;
        core.ContextMenuRequested += (_, eventArgs) =>
        {



            var target = eventArgs.ContextMenuTarget;
            if (target.IsEditable)
            {
                return;
            }
            if (!target.HasSelection)
            {
                eventArgs.Handled = true;
                return;
            }
            var allowed = new HashSet<string>(
                ["copy", "selectAll"],
                StringComparer.OrdinalIgnoreCase
            );
            for (var index = eventArgs.MenuItems.Count - 1; index >= 0; index--)
            {
                if (!allowed.Contains(eventArgs.MenuItems[index].Name))
                {
                    eventArgs.MenuItems.RemoveAt(index);
                }
            }
            eventArgs.Handled = eventArgs.MenuItems.Count == 0;
        };
        core.NavigationStarting += (_, eventArgs) =>
        {
            if (!IsApplicationAddress(eventArgs.Uri))
            {
                eventArgs.Cancel = true;
            }
        };
        core.NewWindowRequested += (_, eventArgs) =>
        {
            eventArgs.Handled = true;
        };



        core.DownloadStarting += (_, eventArgs) => eventArgs.Cancel = true;
        core.WebMessageReceived += (_, eventArgs) =>
        {
            try
            {
                using var message = JsonDocument.Parse(eventArgs.WebMessageAsJson);
                var root = message.RootElement;
                var messageType = root.TryGetProperty("type", out var type)
                    ? type.GetString()
                    : null;
                if (
                    messageType == "open_browser"
                    && root.TryGetProperty("url", out var url)
                    && TryCreateWebAddress(url.GetString(), out var target)
                    && webViewEnvironment is not null
                )
                {
                    BeginInvoke(() => new BrowserWindow(projectRoot, webViewEnvironment, target).Show(this));
                    return;
                }
                if (messageType == "save_file")
                {
                    var source = root.TryGetProperty("source", out var sourceValue)
                        ? sourceValue.GetString()
                        : null;
                    var suggestedName = root.TryGetProperty("suggested_name", out var nameValue)
                        ? nameValue.GetString()
                        : null;
                    var urlValue = root.TryGetProperty("url", out var artifactUrl)
                        ? artifactUrl.GetString()
                        : null;
                    var content = root.TryGetProperty("content", out var contentValue)
                        ? contentValue.GetString()
                        : null;
                    _ = SaveFileFromChatAsync(source, suggestedName, urlValue, content);
                }
            }
            catch (Exception error) when (error is JsonException or InvalidOperationException)
            {
                DesktopDiagnostics.Write(projectRoot, "invalid_native_web_message", error);
            }
        };
        core.ProcessFailed += (_, _) =>
        {
            DesktopDiagnostics.Write(projectRoot, "webview_process_failed", null);
            BeginInvoke(() =>
            {
                MessageBox.Show(
                    this,
                    "The Salty Steak interface stopped unexpectedly. Reopen the application.",
                    "Salty Steak",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                Close();
            });
        };
    }

    private bool IsApplicationAddress(string address)
    {
        return Uri.TryCreate(address, UriKind.Absolute, out var target)
            && target.Scheme == applicationUri.Scheme
            && target.Host == applicationUri.Host
            && target.Port == applicationUri.Port;
    }

    private async Task SaveFileFromChatAsync(
        string? source,
        string? suggestedName,
        string? sourceUrl,
        string? content
    )
    {
        if (saveInProgress)
        {
            MessageBox.Show(
                this,
                "Finish the current save before starting another one.",
                "Save in progress",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information
            );
            return;
        }
        var safeName = SafeSuggestedFileName(suggestedName);
        var downloads = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            "Downloads"
        );
        Directory.CreateDirectory(downloads);
        using var dialog = new SaveFileDialog
        {
            Title = "Save from Salty Steak",
            InitialDirectory = downloads,
            FileName = safeName,
            AddExtension = true,
            OverwritePrompt = true,
            CheckPathExists = true,
            RestoreDirectory = true,
            Filter = SaveFilterFor(safeName),
        };
        if (dialog.ShowDialog(this) != DialogResult.OK)
        {
            return;
        }

        saveInProgress = true;
        using var progress = new SaveProgressWindow(dialog.FileName);
        progress.Show(this);
        progress.Refresh();
        try
        {
            if (string.Equals(source, "text", StringComparison.Ordinal))
            {
                var exact = content ?? "";
                if (Encoding.UTF8.GetByteCount(exact) > 8 * 1024 * 1024)
                {
                    throw new InvalidDataException("The generated file exceeds the 8 MiB save limit.");
                }
                await File.WriteAllTextAsync(
                    dialog.FileName,
                    exact,
                    new UTF8Encoding(encoderShouldEmitUTF8Identifier: false)
                );
            }
            else if (string.Equals(source, "url", StringComparison.Ordinal))
            {
                if (string.IsNullOrWhiteSpace(sourceUrl))
                {
                    throw new InvalidDataException("The image artifact URL is missing.");
                }
                var artifactUri = new Uri(applicationUri, sourceUrl);
                if (!IsApplicationAddress(artifactUri.AbsoluteUri))
                {
                    throw new InvalidDataException("Only local Salty Steak artifacts can be saved.");
                }
                using var client = new HttpClient();
                using var response = await client.GetAsync(
                    artifactUri,
                    HttpCompletionOption.ResponseHeadersRead
                );
                response.EnsureSuccessStatusCode();
                var total = response.Content.Headers.ContentLength;
                await using var input = await response.Content.ReadAsStreamAsync();
                await using var output = new FileStream(
                    dialog.FileName,
                    FileMode.Create,
                    FileAccess.Write,
                    FileShare.None,
                    128 * 1024,
                    useAsync: true
                );
                var buffer = new byte[128 * 1024];
                long copied = 0;
                while (true)
                {
                    var read = await input.ReadAsync(buffer);
                    if (read == 0) break;
                    await output.WriteAsync(buffer.AsMemory(0, read));
                    copied += read;
                    progress.Report(copied, total);
                }
                await output.FlushAsync();
            }
            else
            {
                throw new InvalidDataException("The requested save source is unsupported.");
            }
            browser.CoreWebView2?.PostWebMessageAsJson(
                JsonSerializer.Serialize(new
                {
                    type = "save_completed",
                    filename = Path.GetFileName(dialog.FileName),
                    directory = Path.GetDirectoryName(dialog.FileName),
                })
            );
        }
        catch (Exception error)
        {
            DesktopDiagnostics.Write(projectRoot, "chat_artifact_save_failed", error);
            try { File.Delete(dialog.FileName); } catch (IOException) { }
            MessageBox.Show(
                this,
                $"The file could not be saved.\n\n{error.Message}",
                "Save failed",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
        finally
        {
            progress.Close();
            saveInProgress = false;
        }
    }

    private static string SafeSuggestedFileName(string? value)
    {
        var leaf = Path.GetFileName(string.IsNullOrWhiteSpace(value) ? "salty-steak-file.txt" : value);
        foreach (var invalid in Path.GetInvalidFileNameChars())
        {
            leaf = leaf.Replace(invalid, '-');
        }
        leaf = leaf.Trim().TrimStart('.');
        return string.IsNullOrWhiteSpace(leaf) ? "salty-steak-file.txt" : leaf[..Math.Min(leaf.Length, 120)];
    }

    private static string SaveFilterFor(string fileName)
    {
        var extension = Path.GetExtension(fileName).ToLowerInvariant();
        return extension switch
        {
            ".png" => "PNG image (*.png)|*.png|All files (*.*)|*.*",
            ".py" => "Python file (*.py)|*.py|All files (*.*)|*.*",
            ".ps1" => "PowerShell script (*.ps1)|*.ps1|All files (*.*)|*.*",
            ".json" => "JSON file (*.json)|*.json|All files (*.*)|*.*",
            ".js" => "JavaScript file (*.js)|*.js|All files (*.*)|*.*",
            ".ts" => "TypeScript file (*.ts)|*.ts|All files (*.*)|*.*",
            _ => "All files (*.*)|*.*",
        };
    }

    private static bool TryCreateWebAddress(string? address, out Uri target)
    {
        target = null!;
        return Uri.TryCreate(address, UriKind.Absolute, out var parsed)
            && (parsed.Scheme == Uri.UriSchemeHttps || parsed.Scheme == Uri.UriSchemeHttp)
            && (target = parsed) is not null;
    }
}

internal sealed class SaveProgressWindow : Form
{
    private readonly ProgressBar bar;
    private readonly Label status;

    public SaveProgressWindow(string destination)
    {
        Text = "Saving from Salty Steak";
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        ControlBox = false;
        ShowInTaskbar = false;
        ClientSize = new Size(520, 126);
        BackColor = Color.FromArgb(29, 29, 28);
        ForeColor = Color.Gainsboro;
        var title = new Label
        {
            AutoSize = false,
            Text = "Saving file",
            Font = new Font(SystemFonts.MessageBoxFont!, FontStyle.Bold),
            Location = new Point(20, 16),
            Size = new Size(480, 24),
        };
        status = new Label
        {
            AutoEllipsis = true,
            Text = destination,
            Location = new Point(20, 44),
            Size = new Size(480, 24),
        };
        bar = new ProgressBar
        {
            Location = new Point(20, 82),
            Size = new Size(480, 16),
            Style = ProgressBarStyle.Marquee,
            MarqueeAnimationSpeed = 28,
        };
        Controls.Add(title);
        Controls.Add(status);
        Controls.Add(bar);
    }

    public void Report(long copied, long? total)
    {
        if (total is not > 0) return;
        bar.Style = ProgressBarStyle.Continuous;
        bar.MarqueeAnimationSpeed = 0;
        bar.Maximum = 1000;
        bar.Value = Math.Clamp((int)(copied * 1000L / total.Value), 0, 1000);
        status.Text = $"{copied:N0} of {total.Value:N0} bytes";
        Refresh();
    }
}

internal sealed class BrowserWindow : Form
{
    private readonly string projectRoot;
    private readonly CoreWebView2Environment environment;
    private readonly Uri initialAddress;
    private readonly WebView2 browser;
    private readonly TextBox address;
    private readonly Button back;
    private readonly Button forward;

    public BrowserWindow(string projectRoot, CoreWebView2Environment environment, Uri initialAddress)
    {
        this.projectRoot = projectRoot;
        this.environment = environment;
        this.initialAddress = initialAddress;
        Text = "Browser - Salty Steak";
        AccessibleName = "Salty Steak Browser";
        StartPosition = FormStartPosition.CenterParent;
        ClientSize = new Size(1180, 780);
        MinimumSize = new Size(820, 600);
        BackColor = Color.FromArgb(29, 29, 28);

        var toolbar = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            Height = 54,
            BackColor = Color.FromArgb(20, 20, 19),
            Padding = new Padding(12, 10, 12, 10),
            ColumnCount = 4,
            RowCount = 1
        };
        toolbar.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 38));
        toolbar.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 38));
        toolbar.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        toolbar.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 70));

        back = BrowserButton("\u2190", "Back");
        forward = BrowserButton("\u2192", "Forward");
        back.Font = new Font("Segoe UI Symbol", 11F);
        forward.Font = new Font("Segoe UI Symbol", 11F);
        address = new TextBox
        {
            Dock = DockStyle.Fill,
            AccessibleName = "Web address",
            BackColor = Color.FromArgb(36, 36, 35),
            ForeColor = Color.FromArgb(242, 240, 237),
            BorderStyle = BorderStyle.None,
            Font = new Font("Segoe UI", 10F),
            Text = initialAddress.AbsoluteUri,
            Margin = new Padding(8, 6, 8, 5)
        };
        var go = BrowserButton("Open", "Open address");
        go.BackColor = Color.FromArgb(176, 124, 99);
        go.ForeColor = Color.FromArgb(23, 21, 19);
        go.Font = new Font("Segoe UI", 9F, FontStyle.Bold);
        toolbar.Controls.Add(back, 0, 0);
        toolbar.Controls.Add(forward, 1, 0);
        toolbar.Controls.Add(address, 2, 0);
        toolbar.Controls.Add(go, 3, 0);

        browser = new WebView2
        {
            Dock = DockStyle.Fill,
            DefaultBackgroundColor = Color.FromArgb(29, 29, 28)
        };
        Controls.Add(browser);
        Controls.Add(toolbar);

        back.Click += (_, _) => { if (browser.CoreWebView2?.CanGoBack == true) browser.CoreWebView2.GoBack(); };
        forward.Click += (_, _) => { if (browser.CoreWebView2?.CanGoForward == true) browser.CoreWebView2.GoForward(); };
        go.Click += (_, _) => NavigateFromAddressBar();
        address.KeyDown += (_, eventArgs) =>
        {
            if (eventArgs.KeyCode == Keys.Enter)
            {
                eventArgs.SuppressKeyPress = true;
                NavigateFromAddressBar();
            }
        };
        Shown += OpenBrowser;
    }

    protected override void OnHandleCreated(EventArgs eventArgs)
    {
        base.OnHandleCreated(eventArgs);
        NativeWindowIdentity.UseDarkTitleBar(Handle);
    }

    protected override bool ProcessCmdKey(ref Message message, Keys keys)
    {
        if (keys == (Keys.Control | Keys.L))
        {
            address.Focus();
            address.SelectAll();
            return true;
        }
        if (keys == (Keys.Alt | Keys.Left) && browser.CoreWebView2?.CanGoBack == true)
        {
            browser.CoreWebView2.GoBack();
            return true;
        }
        if (keys == (Keys.Alt | Keys.Right) && browser.CoreWebView2?.CanGoForward == true)
        {
            browser.CoreWebView2.GoForward();
            return true;
        }
        if (keys == (Keys.Control | Keys.R) && browser.CoreWebView2 is not null)
        {
            browser.CoreWebView2.Reload();
            return true;
        }
        return base.ProcessCmdKey(ref message, keys);
    }

    private static Button BrowserButton(string text, string accessibleName)
    {
        var button = new Button
        {
            Dock = DockStyle.Fill,
            Text = text,
            AccessibleName = accessibleName,
            FlatStyle = FlatStyle.Flat,
            BackColor = Color.FromArgb(20, 20, 19),
            ForeColor = Color.FromArgb(203, 199, 194),
            Margin = new Padding(2, 0, 2, 0),
            TabStop = true
        };
        button.FlatAppearance.BorderSize = 0;
        button.FlatAppearance.MouseOverBackColor = Color.FromArgb(46, 46, 44);
        button.FlatAppearance.MouseDownBackColor = Color.FromArgb(41, 41, 39);
        return button;
    }

    private async void OpenBrowser(object? sender, EventArgs eventArgs)
    {
        try
        {
            await browser.EnsureCoreWebView2Async(environment);
            var core = browser.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreDefaultContextMenusEnabled = true;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsZoomControlEnabled = true;
            core.Settings.IsGeneralAutofillEnabled = false;
            core.Settings.IsPasswordAutosaveEnabled = false;
            core.NavigationStarting += (_, eventArgs) =>
            {
                if (!IsWebAddress(eventArgs.Uri)) eventArgs.Cancel = true;
            };
            core.NewWindowRequested += (_, eventArgs) =>
            {
                eventArgs.Handled = true;
                if (IsWebAddress(eventArgs.Uri)) core.Navigate(eventArgs.Uri);
            };
            core.DownloadStarting += (_, eventArgs) => eventArgs.Cancel = true;
            core.PermissionRequested += (_, eventArgs) => eventArgs.State = CoreWebView2PermissionState.Deny;
            core.HistoryChanged += (_, _) => UpdateNavigationState();
            core.SourceChanged += (_, _) => address.Text = core.Source;
            core.DocumentTitleChanged += (_, _) =>
                Text = string.IsNullOrWhiteSpace(core.DocumentTitle)
                    ? "Browser - Salty Steak"
                    : $"{core.DocumentTitle} - Salty Steak";
            core.ProcessFailed += (_, _) =>
            {
                DesktopDiagnostics.Write(projectRoot, "browser_process_failed", null);
                BeginInvoke(Close);
            };
            core.Navigate(initialAddress.AbsoluteUri);
        }
        catch (Exception error)
        {
            DesktopDiagnostics.Write(projectRoot, "browser_initialization_failed", error);
            MessageBox.Show(this, error.Message, "Browser could not open", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }

    private void NavigateFromAddressBar()
    {
        var candidate = address.Text.Trim();
        if (!candidate.Contains("://", StringComparison.Ordinal)) candidate = "https://" + candidate;
        if (IsWebAddress(candidate)) browser.CoreWebView2?.Navigate(candidate);
    }

    private void UpdateNavigationState()
    {
        back.Enabled = browser.CoreWebView2?.CanGoBack == true;
        forward.Enabled = browser.CoreWebView2?.CanGoForward == true;
    }

    private static bool IsWebAddress(string? value) =>
        Uri.TryCreate(value, UriKind.Absolute, out var target)
        && (target.Scheme == Uri.UriSchemeHttps || target.Scheme == Uri.UriSchemeHttp);
}

internal static class DesktopDiagnostics
{
    public static void Write(string? projectRoot, string eventName, Exception? error)
    {
        try
        {
            var root = projectRoot ?? AppContext.BaseDirectory;
            var workspace = Environment.GetEnvironmentVariable("SALTY_POTATO_WORKSPACE")
                ?? Path.Combine(root, "workspace");
            var directory = Path.Combine(workspace, "logs");
            Directory.CreateDirectory(directory);
            var record = System.Text.Json.JsonSerializer.Serialize(new
            {
                timestamp_utc = DateTime.UtcNow.ToString("O"),
                build_id = BuildIdentity.Value,
                event_name = eventName,
                error_type = error?.GetType().FullName,
                error_message = error?.Message,
                technical_details = error?.ToString(),
            });
            File.AppendAllText(
                Path.Combine(directory, "desktop-host.jsonl"),
                record + Environment.NewLine,
                Encoding.UTF8
            );
        }
        catch
        {

        }
    }

    public static void WriteTiming(
        string projectRoot,
        string phase,
        double elapsedSeconds
    )
    {
        try
        {
            var workspace = Environment.GetEnvironmentVariable("SALTY_POTATO_WORKSPACE")
                ?? Path.Combine(projectRoot, "workspace");
            var directory = Path.Combine(workspace, "logs");
            Directory.CreateDirectory(directory);
            var record = System.Text.Json.JsonSerializer.Serialize(new
            {
                timestamp_utc = DateTime.UtcNow.ToString("O"),
                build_id = BuildIdentity.Value,
                event_name = "startup_timing",
                phase,
                elapsed_seconds = Math.Round(elapsedSeconds, 3),
            });
            File.AppendAllText(
                Path.Combine(directory, "desktop-host.jsonl"),
                record + Environment.NewLine,
                Encoding.UTF8
            );
        }
        catch
        {
        }
    }
}

internal static class NativeWindowIdentity
{
    private const string WindowMarker = "SaltyPotatoAI.NativeWindow.2";
    private const int RestoreWindow = 9;
    private const int DarkTitleBarAttribute = 20;
    private const int WindowCornerPreferenceAttribute = 33;
    private const int BorderColorAttribute = 34;
    private const int CaptionColorAttribute = 35;
    private const int TextColorAttribute = 36;
    private const uint NoMove = 0x0002;
    private const uint NoSize = 0x0001;
    private const uint ShowWindowFlag = 0x0040;
    private const uint FlashAll = 0x0003;

    [StructLayout(LayoutKind.Sequential)]
    private struct FlashInfo
    {
        public uint Size;
        public IntPtr Window;
        public uint Flags;
        public uint Count;
        public uint Timeout;
    }

    private delegate bool EnumWindowsCallback(IntPtr window, IntPtr parameter);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr FindWindow(string? className, string windowName);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr parameter);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetProp(IntPtr window, string name, IntPtr data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetProp(IntPtr window, string name);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool BringWindowToTop(IntPtr window);

    [DllImport("user32.dll")]
    private static extern IntPtr SetFocus(IntPtr window);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(
        uint attachThread,
        uint attachToThread,
        bool attach
    );

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(
        IntPtr window,
        IntPtr insertAfter,
        int x,
        int y,
        int width,
        int height,
        uint flags
    );

    [DllImport("user32.dll")]
    private static extern bool FlashWindowEx(ref FlashInfo information);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(
        IntPtr window,
        int attribute,
        ref int value,
        int valueSize
    );

    public static void ActivateExistingWindow()
    {
        var window = IntPtr.Zero;
        _ = EnumWindows(
            (candidate, _) =>
            {
                if (GetProp(candidate, WindowMarker) == IntPtr.Zero)
                {
                    return true;
                }
                window = candidate;
                return false;
            },
            IntPtr.Zero
        );
        if (window == IntPtr.Zero)
        {
            window = FindWindow(null, "Salty Steak");
        }
        if (window == IntPtr.Zero)
        {
            window = FindWindow(null, "Starting Salty Steak");
        }
        if (window == IntPtr.Zero)
        {
            DesktopDiagnostics.Write(
                null,
                "existing_instance_window_not_found",
                new InvalidOperationException("The existing Salty Steak window was not found.")
            );
            return;
        }
        var foreground = GetForegroundWindow();
        var currentThread = GetCurrentThreadId();
        var foregroundThread = GetWindowThreadProcessId(foreground, out _);
        var targetThread = GetWindowThreadProcessId(window, out _);
        var attachedForeground =
            foregroundThread != 0
            && foregroundThread != currentThread
            && AttachThreadInput(currentThread, foregroundThread, true);
        var attachedTarget =
            targetThread != 0
            && targetThread != currentThread
            && targetThread != foregroundThread
            && AttachThreadInput(currentThread, targetThread, true);
        bool restored;
        bool raised;
        bool focused;
        try
        {
            restored = ShowWindow(window, RestoreWindow);
            raised =
                SetWindowPos(
                    window,
                    IntPtr.Zero,
                    0,
                    0,
                    0,
                    0,
                    NoMove | NoSize | ShowWindowFlag
                ) && BringWindowToTop(window);
            focused = SetForegroundWindow(window);
            if (focused)
            {
                _ = SetFocus(window);
            }
        }
        finally
        {
            if (attachedTarget)
            {
                _ = AttachThreadInput(currentThread, targetThread, false);
            }
            if (attachedForeground)
            {
                _ = AttachThreadInput(currentThread, foregroundThread, false);
            }
        }
        if (!focused)
        {
            var flash = new FlashInfo
            {
                Size = (uint)Marshal.SizeOf<FlashInfo>(),
                Window = window,
                Flags = FlashAll,
                Count = 3,
                Timeout = 0,
            };
            _ = FlashWindowEx(ref flash);
        }
        DesktopDiagnostics.Write(
            null,
            focused ? "existing_instance_focused" : "existing_instance_focus_denied",
            focused || (restored && raised)
                ? null
                : new InvalidOperationException("Windows did not restore the existing window.")
        );
    }

    public static void MarkWindow(IntPtr window)
    {
        _ = SetProp(window, WindowMarker, new IntPtr(1));
    }

    public static void UseDarkTitleBar(IntPtr window)
    {
        var enabled = 1;
        _ = DwmSetWindowAttribute(
            window,
            DarkTitleBarAttribute,
            ref enabled,
            Marshal.SizeOf<int>()
        );

        var captionColor = 0x00131414;
        var borderColor = 0x002C2E2E;
        var textColor = 0x00EDF0F2;
        _ = DwmSetWindowAttribute(
            window,
            CaptionColorAttribute,
            ref captionColor,
            Marshal.SizeOf<int>()
        );
        _ = DwmSetWindowAttribute(
            window,
            BorderColorAttribute,
            ref borderColor,
            Marshal.SizeOf<int>()
        );
        _ = DwmSetWindowAttribute(
            window,
            TextColorAttribute,
            ref textColor,
            Marshal.SizeOf<int>()
        );
    }

    public static void UseSmallRoundedCorners(IntPtr window)
    {
        const int roundSmall = 3;
        var preference = roundSmall;
        _ = DwmSetWindowAttribute(
            window,
            WindowCornerPreferenceAttribute,
            ref preference,
            Marshal.SizeOf<int>()
        );
    }
}
