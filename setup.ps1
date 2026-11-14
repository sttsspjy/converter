# Sets up Converter for daily use:
#   - a private Python venv with its dependencies
#   - Start menu and right-click "Send to" shortcuts (add -Desktop for a desktop one)
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1 [-Desktop] [-NoShortcuts] [-Uninstall]
param([switch]$Desktop, [switch]$NoShortcuts, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$links = @(
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Converter.lnk'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\SendTo\Converter.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Converter.lnk')
)

if ($Uninstall) {
    foreach ($l in $links) { if (Test-Path $l) { Remove-Item $l; Write-Host "Removed $l" } }
    return
}

if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Write-Host 'Creating virtual environment...'
    $py = (Get-Command py -ErrorAction SilentlyContinue)
    if ($py) { & py -3 -m venv $venv } else { & python -m venv $venv }
}
Write-Host 'Installing dependencies...'
& (Join-Path $venv 'Scripts\python.exe') -m pip install --disable-pip-version-check --no-cache-dir -q -r (Join-Path $root 'requirements.txt')

foreach ($tool in 'ffmpeg', 'ffprobe', 'opusenc', 'gifsicle') {
    $found = (Test-Path (Join-Path $root "bin\$tool.exe")) -or (Get-Command $tool -ErrorAction SilentlyContinue)
    if ($found) { Write-Host "  found   $tool" }
    elseif ($tool -eq 'gifsicle') { Write-Host "  missing $tool (optional: lossy GIF compression. choco install gifsicle)" -ForegroundColor DarkYellow }
    else { Write-Host "  missing $tool (put $tool.exe in $root\bin or on PATH)" -ForegroundColor Yellow }
}

if (-not $NoShortcuts) {
    $ws = New-Object -ComObject WScript.Shell
    $targets = $links[0..1]
    if ($Desktop) { $targets += $links[2] }
    foreach ($path in $targets) {
        $l = $ws.CreateShortcut($path)
        $l.TargetPath = Join-Path $venv 'Scripts\pythonw.exe'
        $l.Arguments = "`"$(Join-Path $root 'app.py')`""
        $l.WorkingDirectory = $root
        $l.IconLocation = Join-Path $root 'assets\icon.ico'
        $l.Description = 'Compress videos, GIFs and music'
        $l.Save()
        Write-Host "Shortcut: $path"
    }
}
Write-Host 'Done. Launch Converter from the Start menu, or right-click files > Send to > Converter.'
