# Removes the Task Scheduler tasks and the files that install-task-windows.ps1
# generated. Keeps the bridges' store folders and the logs.
#
# Without -Name it removes the whatsapp-mcp-bridge task and every account's
# whatsapp-mcp-bridge-<name> task; with -Name only that account's.
param([string]$Name)
$ErrorActionPreference = 'Stop'

$TaskName = 'whatsapp-mcp-bridge'
$GeneratedFiles = 'whatsapp-bridge-launcher.exe', 'run-whatsapp-bridge.ps1', 'bridge-task.json'

function Fail([string]$Message) {
    Write-Host "Error: $Message"
    exit 1
}

# Stopping the task ends the launcher, and its job object ends the runner and
# the bridge with it.
function Stop-InstalledTask([string]$Name) {
    $Task = @{ TaskName = $Name; TaskPath = '\' }
    if (-not (Get-ScheduledTask @Task -ErrorAction SilentlyContinue)) { return }
    Stop-ScheduledTask @Task
    for ($i = 0; $i -lt 20 -and (Get-ScheduledTask @Task).State -eq 'Running'; $i++) {
        Start-Sleep -Milliseconds 500
    }
}

if ($env:OS -ne 'Windows_NT') { Fail 'Task Scheduler uninstall is only supported on Windows.' }
if (-not $env:LOCALAPPDATA) { Fail 'LOCALAPPDATA is not set.' }
# The name becomes a folder under %LOCALAPPDATA%\whatsapp-mcp.
if ($Name -and $Name -cnotmatch '^[a-z0-9-]+$') { Fail "Invalid account name '$Name'; use only lowercase letters, digits and hyphens." }

$AppDir = Join-Path $env:LOCALAPPDATA 'whatsapp-mcp'

# The single bridge's files are in the folder itself, each account's in its
# own subfolder.
if ($Name) {
    $tasks = @("$TaskName-$Name")
    $dirs = @(Join-Path $AppDir $Name)
} else {
    $tasks = @(Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue |
        Where-Object { $_.TaskName -eq $TaskName -or $_.TaskName -like "$TaskName-*" } | ForEach-Object { $_.TaskName })
    $dirs = @(Get-ChildItem -LiteralPath $AppDir -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }) + $AppDir
}

foreach ($task in $tasks) {
    Write-Host "Stopping the $task task if present..."
    Stop-InstalledTask $task
    if (Get-ScheduledTask -TaskName $task -TaskPath '\' -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -TaskPath '\' -Confirm:$false
        Write-Host "Removed the $task task."
    }
}

function Remove-EmptyFolder([string]$Path) {
    if ((Test-Path -LiteralPath $Path) -and -not (Get-ChildItem -LiteralPath $Path -Force)) {
        Remove-Item -LiteralPath $Path
    }
}

foreach ($dir in $dirs) {
    foreach ($file in $GeneratedFiles) {
        $path = Join-Path $dir $file
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
    Remove-EmptyFolder $dir
}
Remove-EmptyFolder $AppDir

Write-Host 'Removed the generated support files.'
Write-Host "Preserved each bridge's store folder."
Write-Host "Logs were left in place under $AppDir"
