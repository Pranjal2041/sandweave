param(
  [ValidateSet('build','status')][string]$Action='status',
  [string]$Source='D:\SteamLibrary\steamapps\common\Automobilista 2 Demo',
  [string]$Folder='D:\general-vm-transfer'
)
# Run through the existing ut Windows peer. Keep the source installation idle.
$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$archive=Join-Path $Folder 'ams2-demo.tar.zst'
$partial=$archive+'.partial'
if ($Action -eq 'status') {
  $status=@{}
  foreach ($path in @($archive,$partial)) {
    if (Test-Path -LiteralPath $path) {$status[$path]=(Get-Item -LiteralPath $path).Length}
  }
  ConvertTo-Json $status -Compress
  exit
}
$drive=Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='"+[IO.Path]::GetPathRoot($Folder).TrimEnd('\')+"'")
$start=Get-Date
if ($Action -eq 'build') {
  if ($drive.FreeSpace -lt 16GB) {throw 'Insufficient scratch space for archive'}
  if ((Test-Path -LiteralPath $archive) -or (Test-Path -LiteralPath $partial)) {throw 'Preserve existing archive'}
  New-Item -ItemType Directory -Path $Folder -Force | Out-Null
  & tar.exe --zstd --options zstd:compression-level=3 -cf $partial -C $Source .
  if ($LASTEXITCODE -ne 0) {throw 'tar failed'}
  Move-Item -LiteralPath $partial -Destination $archive
  @{path=$archive;bytes=(Get-Item -LiteralPath $archive).Length;seconds=((Get-Date)-$start).TotalSeconds} | ConvertTo-Json -Compress
  exit
}
