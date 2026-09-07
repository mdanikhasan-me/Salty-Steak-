using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace SaltyPotatoAI.Desktop;

internal sealed record CompanionPreferences(bool enabled = false, string position = "right", string size = "small", bool reducedMotion = false)
{
    public CompanionPreferences Checked() => this with { position = position == "left" ? "left" : "right", size = size == "medium" ? "medium" : "small" };
}

internal sealed class CompanionWindow : System.Windows.Window
{
    private readonly WebView2CompositionControl webView;
    private readonly CoreWebView2Environment environment;
    private readonly Uri pageUri;
    private readonly IntPtr mainHandle;
    private readonly Action<Exception> reportError;
    private CompanionPreferences preferences;
    private double baseline = .88;
    private bool disposed;
    private bool working;
    private DateTime lastWorkSignal;
    private readonly System.Windows.Threading.DispatcherTimer watchdog = new() { Interval = TimeSpan.FromSeconds(2) };

    internal CompanionWindow(CoreWebView2Environment environment, Uri applicationUri, IntPtr mainHandle, CompanionPreferences preferences, Action<Exception> reportError)
    {
        this.environment = environment;
        this.mainHandle = mainHandle;
        this.preferences = preferences;
        this.reportError = reportError;
        pageUri = new Uri(applicationUri, "/assets/companion/desktop.html");
        Title = "Salty Steak companion";
        WindowStyle = System.Windows.WindowStyle.None;
        ResizeMode = System.Windows.ResizeMode.NoResize;
        AllowsTransparency = true;
        Background = System.Windows.Media.Brushes.Transparent;
        ShowInTaskbar = false;
        ShowActivated = false;
        Topmost = true;
        Width = Height = 175;
        webView = new WebView2CompositionControl { DefaultBackgroundColor = System.Drawing.Color.Transparent, AllowExternalDrop = false, Focusable = false };
        Content = webView;
        SourceInitialized += (_, _) => {
            var handle = new System.Windows.Interop.WindowInteropHelper(this).Handle;
            var style = GetWindowLongPtr(handle, -20).ToInt64();
            SetWindowLongPtr(handle, -20, new IntPtr(style | 0x08000000L | 0x00000080L));
            new System.Windows.Interop.WindowInteropHelper(this).EnsureHandle();
            System.Windows.Interop.HwndSource.FromHwnd(handle)?.AddHook(WindowMessage);
            PlaceAtTaskbar();
        };
        Loaded += Initialize;
        watchdog.Tick += (_, _) => { if (working && DateTime.UtcNow - lastWorkSignal > TimeSpan.FromSeconds(5)) ApplyWorkState(false); };
        Closed += (_, _) => { disposed = true; watchdog.Stop(); webView.Dispose(); };
    }

    private async void Initialize(object sender, System.Windows.RoutedEventArgs args)
    {
        try {
            await webView.EnsureCoreWebView2Async(environment);
            if (disposed) return;
            var core = webView.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.NavigationStarting += (_, e) => { if (e.Uri != pageUri.AbsoluteUri) e.Cancel = true; };
            core.NewWindowRequested += (_, e) => e.Handled = true;
            core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.DownloadStarting += (_, e) => e.Cancel = true;
            core.WebMessageReceived += (_, e) => {
                if (e.Source != pageUri.AbsoluteUri) return;
                try {
                    using var doc = JsonDocument.Parse(e.WebMessageAsJson);
                    if (doc.RootElement.TryGetProperty("baseline", out var value) && value.TryGetDouble(out var number) && double.IsFinite(number)) {
                        baseline = Math.Clamp(number, .5, 1);
                        PlaceAtTaskbar();
                        ApplyPreferences(preferences);
                        webView.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new {type="companion_work", active=working}));
                    }
                } catch (JsonException) { }
            };
            core.Navigate(pageUri.AbsoluteUri);
        } catch (Exception error) {
            reportError(error);
            if (!disposed) Close();
        }
    }

    internal void ApplyPreferences(CompanionPreferences next)
    {
        preferences = next.Checked();
        PlaceAtTaskbar();
        webView.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new { type = "companion_preferences", settings = preferences }));
    }

    internal void ApplyWorkState(bool active)
    {
        if(disposed)return;
        working=active;lastWorkSignal=DateTime.UtcNow;
        if(active)watchdog.Start();else watchdog.Stop();
        webView.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new {type="companion_work",active}));
    }

    private void PlaceAtTaskbar()
    {
        var handle = new System.Windows.Interop.WindowInteropHelper(this).Handle;
        if (handle == IntPtr.Zero) return;
        var screen = System.Windows.Forms.Screen.FromHandle(mainHandle);
        var area = screen.WorkingArea;
        var dpi = GetDpiForWindow(mainHandle);
        var size = (int)Math.Round((preferences.size == "medium" ? 215 : 175) * (dpi > 0 ? dpi : 96) / 96d);
        var x = preferences.position == "left" ? area.Left + 18 : area.Right - size - 18;
        var y = area.Bottom - (int)Math.Round(size * baseline);
        SetWindowPos(handle, new IntPtr(-1), x, y, size, size, 0x0010 | 0x0040);
    }

    private IntPtr WindowMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (message == 0x0021) { handled = true; return new IntPtr(3); } // MA_NOACTIVATE
        if (message is 0x007E or 0x001A or 0x02E0) Dispatcher.BeginInvoke(PlaceAtTaskbar);
        return IntPtr.Zero;
    }

    [DllImport("user32.dll")] private static extern IntPtr GetWindowLongPtr(IntPtr window, int index);
    [DllImport("user32.dll")] private static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int width, int height, uint flags);
}
