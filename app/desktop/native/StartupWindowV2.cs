using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Reflection;
using System.Runtime.InteropServices;

namespace SaltyPotatoAI.Desktop;





internal sealed class StartupWindowV2 : Form
{
    private const uint SpiGetClientAreaAnimation = 0x1042;
    private const string MarkResourceName =
        "SaltyPotatoAI.Assets.salty-potato-mark.png";
    private static readonly Color Surface = Color.FromArgb(20, 20, 19);
    private static readonly Color PrimaryText = Color.FromArgb(242, 240, 237);
    private static readonly Color SecondaryText = Color.FromArgb(161, 155, 149);
    private static readonly Color Accent = Color.FromArgb(176, 124, 99);
    private static readonly Color InactiveAccent = Color.FromArgb(72, 55, 47);

    private readonly Label heading;
    private readonly Label stage;
    private readonly Label errorDetail;
    private readonly LoadingDotsControl loadingDots;
    private readonly FlowLayoutPanel failureActions;
    private readonly Button retryButton;
    private readonly Button logsButton;
    private readonly Button exitButton;
    private readonly System.Windows.Forms.Timer timer;
    private EmbeddedBackend? backend;
    private string? projectRoot;
    private string? workspace;
    private bool firstPaintRecorded;
    private bool starting;
    private bool mainWindowExitRequested;

    public StartupWindowV2()
    {
        SuspendLayout();
        AutoScaleDimensions = new SizeF(96f, 96f);
        AutoScaleMode = AutoScaleMode.Dpi;
        Text = "Starting Salty Steak";
        Name = "SaltyPotatoStartupWindow";
        AccessibleName = "Starting Salty Steak";
        AccessibleDescription = "Salty Steak is starting its local services.";
        AccessibleRole = AccessibleRole.Window;
        StartPosition = FormStartPosition.Manual;
        ClientSize = new Size(420, 332);
        FormBorderStyle = FormBorderStyle.None;
        ControlBox = false;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowIcon = false;
        ShowInTaskbar = true;
        BackColor = Surface;
        ForeColor = PrimaryText;
        TopMost = true;
        KeyPreview = true;

	        var layout = new TableLayoutPanel
	        {
	            BackColor = Surface,
            ColumnCount = 1,
            Dock = DockStyle.Fill,
            Margin = Padding.Empty,



	            Padding = new Padding(0, 50, 0, 0),
            RowCount = 11,
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100f));
        foreach (var height in new[] { 20f, 100f, 14f, 36f, 4f, 28f, 8f, 22f, 42f, 42f })
        {
            layout.RowStyles.Add(new RowStyle(SizeType.Absolute, height));
        }
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100f));

        var mark = new PotatoMarkControl(LoadApprovedMark())
        {
            AccessibleName = "Salty Steak mark",
            Anchor = AnchorStyles.None,
            BackColor = Surface,
            Margin = Padding.Empty,
            Size = new Size(100, 100),
            TabStop = false,
        };
        mark.Paint += (_, _) => RecordFirstPaint();
        heading = LabelFor(
            "Starting Salty Steak\u2026",
            15f,
            PrimaryText,
            FontStyle.Regular
        );
        heading.AccessibleName = "Starting Salty Steak";
        stage = LabelFor("Starting local engine\u2026", 10.5f, SecondaryText);
        stage.AccessibleName = "Startup status";
        loadingDots = new LoadingDotsControl(Accent, InactiveAccent)
        {
            AccessibleName = "Loading",
            Anchor = AnchorStyles.None,
            BackColor = Surface,
            Margin = Padding.Empty,
            Size = new Size(82, 22),
            TabStop = false,
        };
        errorDetail = LabelFor(string.Empty, 8.5f, SecondaryText);
        errorDetail.AccessibleName = "Startup error";
        errorDetail.AutoEllipsis = true;
        errorDetail.Margin = new Padding(32, 0, 32, 0);
        errorDetail.Visible = false;

        retryButton = ButtonFor("Retry", new Size(76, 30));
        logsButton = ButtonFor("Open logs", new Size(92, 30));
        exitButton = ButtonFor("Exit", new Size(68, 30));
        retryButton.Click += (_, _) => BeginStartup();
        logsButton.Click += (_, _) => OpenLogs();
        exitButton.Click += (_, _) => Close();
        failureActions = new FlowLayoutPanel
        {
            Anchor = AnchorStyles.None,
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            BackColor = Surface,
            FlowDirection = FlowDirection.LeftToRight,
            Margin = Padding.Empty,
            Padding = Padding.Empty,
            TabStop = false,
            Visible = false,
            WrapContents = false,
        };
        failureActions.Controls.Add(retryButton);
        failureActions.Controls.Add(logsButton);
        failureActions.Controls.Add(exitButton);

        layout.Controls.Add(mark, 0, 1);
        layout.Controls.Add(heading, 0, 3);
        layout.Controls.Add(stage, 0, 5);
        layout.Controls.Add(loadingDots, 0, 7);
        layout.Controls.Add(errorDetail, 0, 8);
        layout.Controls.Add(failureActions, 0, 9);
        Controls.Add(layout);

        timer = new System.Windows.Forms.Timer { Interval = 480 };
        timer.Tick += (_, _) => loadingDots.Advance();
        Shown += (_, _) =>
        {
            Refresh();
            RecordFirstPaint();
            StartupTimeline.Mark("startup_window_visibility_requested");
            BeginStartup();
        };
        FormClosed += (_, _) =>
        {
            timer.Stop();
            backend?.Dispose();
            StartupTimeline.Mark("startup_window_backend_disposed");
        };
        ResumeLayout(true);
    }

    protected override void OnLoad(EventArgs eventArgs)
    {
        CenterOnLaunchMonitor();
        base.OnLoad(eventArgs);
    }

    protected override void OnHandleCreated(EventArgs eventArgs)
    {
        base.OnHandleCreated(eventArgs);
        NativeWindowIdentity.MarkWindow(Handle);
        NativeWindowIdentity.UseSmallRoundedCorners(Handle);
    }

    protected override void OnPaint(PaintEventArgs eventArgs)
    {
        eventArgs.Graphics.Clear(Surface);
        base.OnPaint(eventArgs);
        RecordFirstPaint();
    }

    private void RecordFirstPaint()
    {
        if (firstPaintRecorded)
        {
            return;
        }
        firstPaintRecorded = true;
        StartupTimeline.Mark("startup_window_first_paint");
    }

    private static Label LabelFor(
        string text,
        float fontSize,
        Color color,
        FontStyle style = FontStyle.Regular
    ) => new()
    {
        AutoSize = false,
        BackColor = Surface,
        Dock = DockStyle.Fill,
        Font = new Font("Segoe UI", fontSize, style, GraphicsUnit.Point),
        ForeColor = color,
        Margin = Padding.Empty,
        Text = text,
        TextAlign = ContentAlignment.MiddleCenter,
        UseMnemonic = false,
    };

    private static Button ButtonFor(string text, Size size)
    {
        var button = new Button
        {
            AccessibleName = text,
            AutoSize = false,
            BackColor = Color.FromArgb(36, 36, 35),
            FlatStyle = FlatStyle.Flat,
            Font = new Font("Segoe UI", 9f, FontStyle.Regular, GraphicsUnit.Point),
            ForeColor = PrimaryText,
            Margin = new Padding(4, 0, 4, 0),
            Size = size,
            Text = text,
            UseVisualStyleBackColor = false,
        };
        button.FlatAppearance.BorderColor = Color.FromArgb(53, 52, 50);
        button.FlatAppearance.BorderSize = 1;
        button.FlatAppearance.MouseDownBackColor = Color.FromArgb(41, 41, 39);
        button.FlatAppearance.MouseOverBackColor = Color.FromArgb(46, 46, 44);
        return button;
    }

    private static Bitmap? LoadApprovedMark()
    {
        using var stream = Assembly
            .GetExecutingAssembly()
            .GetManifestResourceStream(MarkResourceName);
        if (stream is null)
        {
            return null;
        }
        using var source = new Bitmap(stream);
        return new Bitmap(source);
    }

    private void CenterOnLaunchMonitor()
    {
        var workingArea = Screen.FromPoint(Cursor.Position).WorkingArea;
        Location = new Point(
            Math.Max(workingArea.Left, workingArea.Left + (workingArea.Width - Width) / 2),
            Math.Max(workingArea.Top, workingArea.Top + (workingArea.Height - Height) / 2)
        );
    }

    private void StartAnimation()
    {
        loadingDots.Visible = true;
        if (ClientAreaAnimationsEnabled())
        {
            loadingDots.AnimationEnabled = true;
            timer.Start();
        }
        else
        {
            loadingDots.AnimationEnabled = false;
            timer.Stop();
        }
    }

    private static bool ClientAreaAnimationsEnabled()
    {
        var enabled = 1;
        return !SystemParametersInfo(
                SpiGetClientAreaAnimation,
                0,
                ref enabled,
                0
            )
            || enabled != 0;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SystemParametersInfo(
        uint action,
        uint parameter,
        ref int value,
        uint update
    );

    private async void BeginStartup()
    {
        if (starting)
        {
            return;
        }
        starting = true;
        heading.Text = "Starting Salty Steak\u2026";
        stage.Text = "Starting local engine\u2026";
        errorDetail.Text = string.Empty;
        errorDetail.Visible = false;
        failureActions.Visible = false;
        StartAnimation();
        try
        {
            await Task.Run(InitializeBackend);
            stage.Text = "Preparing Chat\u2026";
            var main = new MainWindow(projectRoot!, backend!.Url, OnInterfaceReady);
            main.FormClosing += (_, _) =>
            {
                if (ReferenceEquals(main.Owner, this))
                {
                    main.Owner = null;
                }
            };
            main.FormClosed += (_, _) => RequestMainWindowExit();
            main.HandleDestroyed += (_, _) => RequestMainWindowExit();
            main.Show(this);



            main.Owner = null;
        }
        catch (Exception error)
        {
            timer.Stop();
            StartupTimeline.Mark("startup_failed");
            DesktopDiagnostics.Write(projectRoot, "startup_failed", error);
            loadingDots.Visible = false;
            heading.Text = "Salty Steak couldn\u2019t start";
            stage.Text = "Startup stopped";
            errorDetail.Text = error.Message;
            errorDetail.AccessibleDescription = error.ToString();
            errorDetail.Visible = true;
            failureActions.Visible = true;
            retryButton.Focus();
        }
        finally
        {
            starting = false;
        }
    }

    private void InitializeBackend()
    {
        if (
            int.TryParse(
                Environment.GetEnvironmentVariable("SALTY_POTATO_STARTUP_FIXTURE_DELAY_SECONDS"),
                out var fixtureDelay
            )
            && fixtureDelay > 0
        )
        {
            fixtureDelay = Math.Min(fixtureDelay, 60);
            UpdateStageFromWorker("Opening workspace\u2026");
            StartupTimeline.Mark("startup_fixture_delay_started");
            Thread.Sleep(TimeSpan.FromSeconds(fixtureDelay));
            StartupTimeline.Mark("startup_fixture_delay_finished");
        }
        var failureFixture =
            Environment.GetEnvironmentVariable("SALTY_POTATO_STARTUP_FIXTURE_FAILURE");
        if (
            string.Equals(failureFixture, "1", StringComparison.Ordinal)
            || string.Equals(failureFixture, "once", StringComparison.OrdinalIgnoreCase)
        )
        {
            if (string.Equals(failureFixture, "once", StringComparison.OrdinalIgnoreCase))
            {
                Environment.SetEnvironmentVariable(
                    "SALTY_POTATO_STARTUP_FIXTURE_FAILURE",
                    null
                );
            }
            throw new InvalidOperationException("The startup failure fixture was requested.");
        }
        StartupTimeline.Mark("package_root_resolution_started");
        projectRoot = ProjectLocation.Find();
        StartupTimeline.Mark("package_root_resolved");
        BuildIdentity.Load(projectRoot);
        Environment.SetEnvironmentVariable("SALTY_POTATO_BUILD_ID", BuildIdentity.Value);
        var executable = Environment.ProcessPath ?? Application.ExecutablePath;
        Environment.SetEnvironmentVariable("SALTY_POTATO_EXECUTABLE_PATH", executable);
        Environment.SetEnvironmentVariable(
            "SALTY_POTATO_NATIVE_VERSION",
            FileVersionInfo.GetVersionInfo(executable).ProductVersion
        );
        workspace = WorkspaceSelection.CommandLineArgument();
        if (string.IsNullOrWhiteSpace(workspace))
        {
            workspace = Environment.GetEnvironmentVariable("SALTY_POTATO_WORKSPACE");
        }
        if (string.IsNullOrWhiteSpace(workspace))
        {
            workspace = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Salty Steak",
                "Workspace"
            );
        }
        Environment.SetEnvironmentVariable("SALTY_POTATO_WORKSPACE", workspace);
        StartupTimeline.Configure(workspace);
        StartupTimeline.Mark("workspace_path_resolved");
        backend = new EmbeddedBackend(projectRoot, UpdateStageFromWorker);
        backend.Start();
    }

    private void UpdateStageFromWorker(string value)
    {
        if (IsDisposed || !IsHandleCreated)
        {
            return;
        }
        BeginInvoke(() => stage.Text = NormalizeStage(value));
    }

    private static string NormalizeStage(string value) => value
        .Replace("\u00E2\u20AC\u00A6", "\u2026", StringComparison.Ordinal)
        .Replace("...", "\u2026", StringComparison.Ordinal);

    private void OnInterfaceReady()
    {
        timer.Stop();
        StartupTimeline.Mark("main_window_first_paint");
        StartupTimeline.Mark("chat_ready");
        StartupTimeline.Mark("startup_loader_dismissed");
        TopMost = false;
        Hide();
    }

    private void RequestMainWindowExit()
    {
        if (mainWindowExitRequested)
        {
            return;
        }
        mainWindowExitRequested = true;




        BeginInvoke(() =>
        {
            if (!IsDisposed)
            {
                Close();
            }
        });
    }

    private void OpenLogs()
    {
        var directoryRoot = workspace;
        if (string.IsNullOrWhiteSpace(directoryRoot))
        {
            directoryRoot = Environment.GetEnvironmentVariable("SALTY_POTATO_WORKSPACE");
        }
        if (string.IsNullOrWhiteSpace(directoryRoot))
        {
            directoryRoot = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "Salty Steak",
                "Workspace"
            );
        }
        var directory = Path.Combine(directoryRoot, "logs");
        Directory.CreateDirectory(directory);
        Process.Start(new ProcessStartInfo { FileName = directory, UseShellExecute = true });
    }
}

internal sealed class PotatoMarkControl : Control
{
    private readonly Bitmap? mark;

    public PotatoMarkControl(Bitmap? mark)
    {
        this.mark = mark;
        SetStyle(
            ControlStyles.AllPaintingInWmPaint
                | ControlStyles.OptimizedDoubleBuffer
                | ControlStyles.ResizeRedraw
                | ControlStyles.SupportsTransparentBackColor
                | ControlStyles.UserPaint,
            true
        );
    }

    protected override void OnPaint(PaintEventArgs eventArgs)
    {
        base.OnPaint(eventArgs);
        var graphics = eventArgs.Graphics;
        graphics.CompositingQuality = CompositingQuality.HighQuality;
        graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
        graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
        graphics.SmoothingMode = SmoothingMode.HighQuality;
        if (mark is not null)
        {
            graphics.DrawImage(mark, ClientRectangle);
            return;
        }
        DrawVectorFallback(graphics, ClientRectangle);
    }

    private static void DrawVectorFallback(Graphics graphics, Rectangle bounds)
    {
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        var scale = Math.Min(bounds.Width, bounds.Height) / 64f;
        graphics.TranslateTransform(
            bounds.Left + (bounds.Width - 64f * scale) / 2f,
            bounds.Top + (bounds.Height - 64f * scale) / 2f
        );
        graphics.ScaleTransform(scale, scale);
        using var potato = new SolidBrush(Color.FromArgb(215, 121, 82));
        using var crater = new SolidBrush(Color.FromArgb(174, 89, 62));
        using var salt = new SolidBrush(Color.FromArgb(243, 216, 184));
        graphics.FillEllipse(potato, 10f, 10f, 43f, 45f);
        graphics.FillEllipse(crater, 21f, 21f, 7f, 7f);
        graphics.FillEllipse(crater, 37f, 39f, 6f, 6f);
        graphics.TranslateTransform(44f, 12f);
        graphics.RotateTransform(45f);
        graphics.FillRectangle(salt, -5f, -5f, 10f, 10f);
        graphics.ResetTransform();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            mark?.Dispose();
        }
        base.Dispose(disposing);
    }
}

internal sealed class LoadingDotsControl : Control
{
    private readonly SolidBrush activeBrush;
    private readonly SolidBrush inactiveBrush;
    private int frame;

    public LoadingDotsControl(Color active, Color inactive)
    {
        activeBrush = new SolidBrush(active);
        inactiveBrush = new SolidBrush(inactive);
        SetStyle(
            ControlStyles.AllPaintingInWmPaint
                | ControlStyles.OptimizedDoubleBuffer
                | ControlStyles.ResizeRedraw
                | ControlStyles.SupportsTransparentBackColor
                | ControlStyles.UserPaint,
            true
        );
    }

    public bool AnimationEnabled { get; set; } = true;

    public void Advance()
    {
        if (!AnimationEnabled)
        {
            return;
        }
        frame = (frame + 1) % 3;
        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs eventArgs)
    {
        base.OnPaint(eventArgs);
        eventArgs.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        var diameter = Math.Max(5f, DeviceDpi / 16f);
        var gap = diameter * 2.25f;
        var groupWidth = diameter + gap * 2f;
        var startX = (ClientSize.Width - groupWidth) / 2f;
        var y = (ClientSize.Height - diameter) / 2f;
        for (var index = 0; index < 3; index += 1)
        {
            var brush =
                !AnimationEnabled || index == frame ? activeBrush : inactiveBrush;
            eventArgs.Graphics.FillEllipse(
                brush,
                startX + gap * index,
                y,
                diameter,
                diameter
            );
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            activeBrush.Dispose();
            inactiveBrush.Dispose();
        }
        base.Dispose(disposing);
    }
}
