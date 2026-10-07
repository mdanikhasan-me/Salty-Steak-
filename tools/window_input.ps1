param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,
    [Parameter(Mandatory = $true)]
    [int]$X,
    [Parameter(Mandatory = $true)]
    [int]$Y,
    [ValidateSet("left", "right")]
    [string]$Button = "left",
    [switch]$MoveOnly,
    [string]$PasteText,
    [string]$Keys,
    [int]$Wheel = 0
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class SaltyWindowInput {
    public delegate bool EnumWindowCallback(IntPtr handle, IntPtr parameter);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowCallback callback, IntPtr parameter);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr handle);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr handle, out RECT rectangle);
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr handle);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr handle, int command);
    [DllImport("user32.dll")]
    public static extern bool SetWindowPos(
        IntPtr handle, IntPtr insertAfter, int x, int y, int width, int height,
        uint flags
    );
    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")]
    public static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
    public static IntPtr VisibleWindowForProcess(uint processId) {
        IntPtr result = IntPtr.Zero;
        EnumWindows((handle, parameter) => {
            uint candidate;
            GetWindowThreadProcessId(handle, out candidate);
            if (candidate == processId && IsWindowVisible(handle)) {
                result = handle;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
"@

$handle = [SaltyWindowInput]::VisibleWindowForProcess([uint32]$ProcessId)
if ($handle -eq [IntPtr]::Zero) {
    throw "No visible target window."
}
$rectangle = New-Object SaltyWindowInput+RECT
[SaltyWindowInput]::GetWindowRect($handle, [ref]$rectangle) | Out-Null
[SaltyWindowInput]::ShowWindow($handle, 9) | Out-Null
[SaltyWindowInput]::SetWindowPos(
    $handle, [IntPtr](-1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040
) | Out-Null
[SaltyWindowInput]::SetForegroundWindow($handle) | Out-Null
[SaltyWindowInput]::SetCursorPos($rectangle.Left + $X, $rectangle.Top + $Y) | Out-Null
Start-Sleep -Milliseconds 150
if ($MoveOnly) {
    # Positioning alone is useful for validating hover-revealed controls.
} elseif ($Button -eq "right") {
    [SaltyWindowInput]::mouse_event(0x0008, 0, 0, 0, [UIntPtr]::Zero)
    [SaltyWindowInput]::mouse_event(0x0010, 0, 0, 0, [UIntPtr]::Zero)
} else {
    [SaltyWindowInput]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [SaltyWindowInput]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
}
if ($Wheel -ne 0) {
    $wheelData = if ($Wheel -lt 0) {
        [uint32](4294967296L + $Wheel)
    } else {
        [uint32]$Wheel
    }
    [SaltyWindowInput]::mouse_event(
        0x0800,
        0,
        0,
        $wheelData,
        [UIntPtr]::Zero
    )
}
Start-Sleep -Milliseconds 200
if ($PSBoundParameters.ContainsKey("PasteText")) {
    [System.Windows.Forms.Clipboard]::SetText($PasteText)
    [System.Windows.Forms.SendKeys]::SendWait("^v")
}
if ($Keys) {
    [System.Windows.Forms.SendKeys]::SendWait($Keys)
}
[SaltyWindowInput]::SetWindowPos(
    $handle, [IntPtr](-2), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040
) | Out-Null
