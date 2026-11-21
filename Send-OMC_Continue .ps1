<#
.SYNOPSIS
  At a given time, find the PowerShell / Windows Terminal window running Claude Code,
  type "continue" and press Enter.

.EXAMPLES
  # 1) Preview which windows would match (nothing is typed)
  .\Send-Continue.ps1 -ListOnly

  # 2) Wait until 03:10, then send
  .\Send-Continue.ps1 -At 03:10

  # 3) Send right now to windows whose title contains "Claude"
  .\Send-Continue.ps1 -TitleLike "*Claude*"
#>
param(
    [string]$At,                              # "HH:mm". Omit to run immediately.
    [string[]]$TitleLike = @('*'),             # one or more title wildcards, e.g. "*Claude*","*foo*"
    [switch]$IncludeSelf,                      # also target the window running this script
    [switch]$All,                            # target every open PowerShell/terminal window (ignores -TitleLike)
    [string]$Text = ' /oh-my-claudecode:autopilot continue',
    [string[]]$HostProcesses = @('powershell','pwsh','WindowsTerminal','conhost','OpenConsole'),
    [switch]$ListOnly
)

$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'Send-Continue.log'
function Log($m) { $l = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m; Write-Host $l; Add-Content -Path $log -Value $l }

Add-Type -AssemblyName System.Windows.Forms
if (-not ('ScWinV3' -as [type])) {
Add-Type @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public class ScWinV3 {
  delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc p, IntPtr l);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);

  public static List<IntPtr> TopWindows() {
    var list = new List<IntPtr>();
    EnumWindows((h, l) => { if (IsWindowVisible(h) && GetWindowTextLength(h) > 0) list.Add(h); return true; }, IntPtr.Zero);
    return list;
  }
  public static string Title(IntPtr h) {
    var sb = new StringBuilder(GetWindowTextLength(h) + 1);
    GetWindowText(h, sb, sb.Capacity);
    return sb.ToString();
  }
}
"@
}

function Find-Targets {
    # Exclude the window this script itself is running in (unless -IncludeSelf)
    $selfWin = [ScWinV3]::GetConsoleWindow()
    $selfRoot = [IntPtr]::Zero
    if ($selfWin -ne [IntPtr]::Zero) { $selfRoot = [ScWinV3]::GetAncestor($selfWin, 3) }   # GA_ROOTOWNER
    foreach ($h in [ScWinV3]::TopWindows()) {
        if (-not $IncludeSelf -and $selfWin -ne [IntPtr]::Zero -and ($h -eq $selfWin -or $h -eq $selfRoot)) { continue }
        $procId = 0
        [void][ScWinV3]::GetWindowThreadProcessId($h, [ref]$procId)
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if (-not $p -or $HostProcesses -notcontains $p.ProcessName) { continue }
        $title = [ScWinV3]::Title($h)
        $hit = $All
        if (-not $hit) { foreach ($pat in $TitleLike) { if ($title -like $pat) { $hit = $true; break } } }
        if ($hit) {
            [pscustomobject]@{ Handle = $h; Process = $p.ProcessName; Pid = $procId; Title = $title }
        }
    }
}

function Focus-Window([IntPtr]$h) {
    if ([ScWinV3]::IsIconic($h)) { [void][ScWinV3]::ShowWindow($h, 9) }   # SW_RESTORE
    # Tapping ALT lets SetForegroundWindow succeed from a background process
    [ScWinV3]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
    [ScWinV3]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
    for ($i = 0; $i -lt 5; $i++) {
        [void][ScWinV3]::SetForegroundWindow($h)
        Start-Sleep -Milliseconds 300
        if ([ScWinV3]::GetForegroundWindow() -eq $h) { return $true }
    }
    return $false
}

function Send-Text([IntPtr]$h) {
    if (-not (Focus-Window $h)) { return $false }
    # Paste via clipboard so a Korean IME in Hangul mode can't turn "continue" into Hangul
    $old = $null
    try { $old = Get-Clipboard -Raw } catch {}
    Set-Clipboard -Value $Text
    Start-Sleep -Milliseconds 150
    [System.Windows.Forms.SendKeys]::SendWait('^v')
    Start-Sleep -Milliseconds 400
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    Start-Sleep -Milliseconds 300
    if ($null -ne $old) { Set-Clipboard -Value $old }
    return $true
}

if ($ListOnly) {
    $t = @(Find-Targets)
    if ($t.Count -eq 0) { Write-Host "No matching window (TitleLike = $TitleLike)" }
    else { $t | Format-Table Process, Pid, Title -AutoSize }
    return
}

if ($At) {
    $target = [datetime]::ParseExact($At, 'HH:mm', $null)
    if ($target -le (Get-Date)) { $target = $target.AddDays(1) }
    Log "Waiting until $target"
    while ((Get-Date) -lt $target) { Start-Sleep -Seconds 5 }
}

$targets = @(Find-Targets)
if ($targets.Count -eq 0) { Log "No matching window (TitleLike = $TitleLike). Nothing sent."; exit 1 }

foreach ($t in $targets) {
    if (Send-Text $t.Handle) { Log "Sent '$Text' + Enter -> [$($t.Process)] $($t.Title)" }
    else { Log "FAILED to focus -> [$($t.Process)] $($t.Title)" }
}