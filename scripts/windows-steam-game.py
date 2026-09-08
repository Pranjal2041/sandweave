#!/usr/bin/env python3
"""Read one Steam game's download state or inventory through an existing ut peer."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

SCRIPT = r'''
$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$r=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST__')) | ConvertFrom-Json)
$manifest=Join-Path $r.library ('steamapps/appmanifest_'+$r.appid+'.acf')
$state=@{}
if (-not (Test-Path -LiteralPath $manifest)) { throw 'Game manifest does not exist' }
$content=Get-Content -LiteralPath $manifest -Raw
foreach ($key in @('appid','name','StateFlags','installdir','buildid','TargetBuildID','BytesToDownload','BytesDownloaded','BytesToStage','BytesStaged','SizeOnDisk','UpdateResult')) {
  $match=[regex]::Match($content,'"'+$key+'"\s+"([^"]*)"')
  if ($match.Success) {$state[$key]=$match.Groups[1].Value}
}
$source=Join-Path (Join-Path $r.library 'steamapps/common') $state.installdir
if ($r.action -eq 'status') { $state.source=$source; ConvertTo-Json $state -Compress; exit }
if ([int]$state.StateFlags -ne 4) {throw 'Wait for a fully installed, idle game before taking its inventory'}
$source=(Get-Item -LiteralPath $source).FullName.TrimEnd('\')
$files=@(Get-ChildItem -LiteralPath $source -Recurse -File | ForEach-Object {
  if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Unexpected reparse point in game files'}
  @{path=$_.FullName.Substring($source.Length+1).Replace('\','/');size=$_.Length}
})
ConvertTo-Json -InputObject @{source=$source;files=$files;state=$state} -Depth 4 -Compress
'''

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('action', choices=('status', 'inventory'))
parser.add_argument('--machine', default='pranjala-win')
parser.add_argument('--appid', type=int, default=1786210)
parser.add_argument('--library', default='D:/SteamLibrary')
parser.add_argument('--output', type=Path)
args = parser.parse_args()
if args.appid <= 0:
    parser.error('appid must be positive')
if args.action == 'inventory' and args.output is None:
    parser.error('inventory requires --output for the importer manifest')
request = {key: getattr(args, key) for key in ('action', 'appid', 'library')}
script = SCRIPT.replace('__REQUEST__', base64.b64encode(json.dumps(request).encode()).decode())
command = 'powershell -NoProfile -NonInteractive -EncodedCommand '
command += base64.b64encode(script.encode('utf-16le')).decode()
result = subprocess.run(['ut', 'exec', '@'+args.machine, command], capture_output=True,
                        text=True, timeout=60)
if result.returncode:
    parser.exit(result.returncode, (result.stderr or result.stdout)[-5000:]+'\n')
data = json.loads(result.stdout)
if args.action == 'inventory':
    files = data.pop('files')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(files, indent=2)+'\n')
    data.update(manifest=str(args.output), total_files=len(files),
                total_bytes=sum(item['size'] for item in files))
print(json.dumps(data, indent=2))
