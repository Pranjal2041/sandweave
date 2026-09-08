#!/usr/bin/env python3
"""Inspect and operate only Steam-owned windows through an existing ut peer."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

SCRIPT = r'''
$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
$request=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST__')) | ConvertFrom-Json)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class SteamWin {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public delegate bool EnumProc(IntPtr hwnd, IntPtr arg);
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int L,T,R,B; }
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc fn, IntPtr arg);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hwnd,StringBuilder text,int count);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd,out Rect rect);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd,IntPtr dc,uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags,uint x,uint y,uint data,UIntPtr extra);
}
'@
[void][SteamWin]::SetProcessDPIAware()
$steamIds=@(Get-Process steam,steamwebhelper -ErrorAction SilentlyContinue | ForEach-Object {$_.Id})
$script:windows=[Collections.Generic.List[object]]::new()
$callback=[SteamWin+EnumProc]{param($hwnd,$unused)
  [uint32]$owner=0
  [void][SteamWin]::GetWindowThreadProcessId($hwnd,[ref]$owner)
  if (($steamIds -contains [int]$owner) -and [SteamWin]::IsWindowVisible($hwnd)) {
    $text=[Text.StringBuilder]::new(1024)
    [void][SteamWin]::GetWindowText($hwnd,$text,1024)
    $rect=[SteamWin+Rect]::new()
    [void][SteamWin]::GetWindowRect($hwnd,[ref]$rect)
    if ($rect.R -gt $rect.L -and $rect.B -gt $rect.T) {
      $script:windows.Add([PSCustomObject]@{hwnd=$hwnd.ToInt64();title=$text.ToString();x=$rect.L;y=$rect.T;width=$rect.R-$rect.L;height=$rect.B-$rect.T})
    }
  }
  return $true
}
[void][SteamWin]::EnumWindows($callback,[IntPtr]::Zero)
if ($request.action -eq 'list') { ConvertTo-Json -InputObject @($script:windows.ToArray()) -Compress; exit }
$window=@($script:windows | Where-Object {$_.hwnd -eq $request.hwnd})
if ($window.Count -ne 1) { throw 'Choose one current visible Steam-owned window handle' }
$window=$window[0]
$hwnd=[IntPtr][long]$window.hwnd
if ($request.action -eq 'capture') {
  $bitmap=[Drawing.Bitmap]::new($window.width,$window.height)
  $graphics=[Drawing.Graphics]::FromImage($bitmap)
  $dc=$graphics.GetHdc()
  try { if (-not [SteamWin]::PrintWindow($hwnd,$dc,2)) {throw 'PrintWindow failed'} }
  finally {$graphics.ReleaseHdc($dc)}
  $stream=[IO.MemoryStream]::new()
  $bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png)
  [PSCustomObject]@{window=$window;png=[Convert]::ToBase64String($stream.ToArray())} | ConvertTo-Json -Compress
  $stream.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
} else {
  if (-not [SteamWin]::SetForegroundWindow($hwnd)) {throw 'Could not focus the selected Steam window'}
  Start-Sleep -Milliseconds 200
  if ([SteamWin]::GetForegroundWindow() -ne $hwnd) {throw 'Selected Steam window is not focused'}
  if ($request.action -eq 'click') {
    if ($request.x -lt 0 -or $request.y -lt 0 -or $request.x -ge $window.width -or $request.y -ge $window.height) {throw 'Click outside captured window'}
    [void][SteamWin]::SetCursorPos($window.x+$request.x,$window.y+$request.y)
    [SteamWin]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
    [SteamWin]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
  } elseif ($request.action -eq 'keys') {
    [Windows.Forms.SendKeys]::SendWait($request.keys)
  }
  $window | ConvertTo-Json -Compress
}
'''

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('action', choices=('list', 'capture', 'click', 'keys'))
parser.add_argument('--machine', default='pranjala-win')
parser.add_argument('--hwnd', type=int)
parser.add_argument('--x', type=int)
parser.add_argument('--y', type=int)
parser.add_argument('--keys')
parser.add_argument('--output', type=Path)
args = parser.parse_args()
if args.action != 'list' and args.hwnd is None:
    parser.error('select a Steam window from list using --hwnd')
if args.action == 'capture' and args.output is None:
    parser.error('capture requires --output')
if args.action == 'click' and (args.x is None or args.y is None):
    parser.error('click requires --x and --y relative to the captured window')
if args.action == 'keys' and args.keys is None:
    parser.error('keys requires --keys in Windows SendKeys notation')
request = {key: getattr(args, key) for key in ('action','hwnd','x','y','keys')}
encoded = base64.b64encode(json.dumps(request).encode()).decode()
script = SCRIPT.replace('__REQUEST__', encoded)
local = Path(__file__).resolve().parents[1] / 'runs/racing/windows-steam-ui.ps1'
local.parent.mkdir(parents=True, exist_ok=True)
local.write_text(script)
remote = 'C:/Users/pranjala/AppData/Local/Temp/general-vm-steam-ui.ps1'
subprocess.run(['ut', 'cp', str(local), args.machine+':'+remote],
               stdout=subprocess.DEVNULL, check=True, timeout=40)
result = subprocess.run(['ut','exec','@'+args.machine,
                         'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File '+remote],
                        capture_output=True, text=True, timeout=40)
if result.returncode:
    parser.exit(result.returncode, (result.stderr or result.stdout)[-5000:]+'\n')
data = json.loads(result.stdout)
if args.action == 'capture':
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(base64.b64decode(data.pop('png')))
    data['output'] = str(args.output)
print(json.dumps(data, indent=2))
