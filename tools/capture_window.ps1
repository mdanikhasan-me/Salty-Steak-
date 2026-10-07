param([string]$Out = "window.png", [string]$Process = "Salty Steak")

# Capture one window by its handle, without touching the foreground.
# PW_RENDERFULLCONTENT (0x2) is what makes this work for a WebView2 host:
# without it a hardware-composited surface comes back blank.
Add-Type -AssemblyName System.Drawing

$signature = @'
using System;
using System.Drawing;
using System.Runtime.InteropServices;

public static class Shot {
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    public static Bitmap Capture(IntPtr handle) {
        RECT rect;
        if (!GetWindowRect(handle, out rect)) return null;
        int width = rect.Right - rect.Left;
        int height = rect.Bottom - rect.Top;
        if (width <= 0 || height <= 0) return null;
        Bitmap bitmap = new Bitmap(width, height);
        using (Graphics graphics = Graphics.FromImage(bitmap)) {
            IntPtr dc = graphics.GetHdc();
            bool ok = PrintWindow(handle, dc, 2);
            graphics.ReleaseHdc(dc);
            if (!ok) { bitmap.Dispose(); return null; }
        }
        return bitmap;
    }
}
'@
if (-not ("Shot" -as [type])) { Add-Type -TypeDefinition $signature -ReferencedAssemblies System.Drawing }

$target = Get-Process -Name $Process -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $target) { Write-Output "no window"; exit 1 }

$bitmap = [Shot]::Capture($target.MainWindowHandle)
if (-not $bitmap) { Write-Output "capture failed"; exit 1 }
$bitmap.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "saved $Out ($($bitmap.Width)x$($bitmap.Height))"
$bitmap.Dispose()
