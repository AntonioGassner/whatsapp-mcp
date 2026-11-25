# Installs per-user Task Scheduler tasks that start the WhatsApp bridge at
# login and start it again whenever it exits. Needs no administrator rights.
#
# Without an accounts file it installs one task, whatsapp-mcp-bridge, for the
# bridge in this checkout. With one (-AccountsFile, else WHATSAPP_ACCOUNTS_FILE)
# it installs whatsapp-mcp-bridge-<name> for every account in it.
param(
    [string]$AccountsFile = $env:WHATSAPP_ACCOUNTS_FILE,
    # Runs this bridge executable instead of building one from the checkout.
    [string]$Binary
)
$ErrorActionPreference = 'Stop'

$TaskName = 'whatsapp-mcp-bridge'
$RestartDelaySeconds = 10
# Passed to every bridge. In accounts mode WHATSAPP_BRIDGE_PORT comes from each
# api_url, and WHATSAPP_BRIDGE_TOKEN is never passed: the MCP server reads each
# account's token from .bridge-token in its store_dir, which a bridge given the
# variable would not use.
$PassThroughVars = 'WEBHOOK_URL', 'WEBHOOK_ENABLED', 'WHATSAPP_AUTO_DOWNLOAD_MEDIA', 'FORWARD_SELF',
    'WHATSAPP_MEDIA_ROOTS', 'WHATSAPP_DEVICE_NAME'

function Fail([string]$Message) {
    Write-Host "Error: $Message"
    exit 1
}

function Test-CgoToolchain {
    if (-not (Get-Command go -ErrorAction SilentlyContinue)) { return $false }
    if ("$(go env CGO_ENABLED)".Trim() -ne '1') { return $false }
    $cc = "$(go env CC)".Trim()
    return [bool]($cc -and (Get-Command $cc -ErrorAction SilentlyContinue))
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

# The csc.exe of .NET Framework 4, which ships with Windows. It is found by
# path because PowerShell 7 runs on a .NET that has no csc.exe.
function Get-CSharpCompiler {
    foreach ($framework in 'Framework64', 'Framework') {
        $csc = Join-Path $env:SystemRoot "Microsoft.NET\$framework\v4.0.30319\csc.exe"
        if (Test-Path -LiteralPath $csc -PathType Leaf) { return $csc }
    }
    Fail 'Could not find csc.exe from .NET Framework 4, which is needed to build the task launcher.'
}

# Like the macOS installer, reinstalling keeps the saved settings, and a
# variable set in this session overrides its saved value.
function Add-TaskEnvironment($Environment, [string]$ConfigFile, [string[]]$Names) {
    $saved = $null
    if (Test-Path -LiteralPath $ConfigFile -PathType Leaf) {
        try {
            $saved = ([System.IO.File]::ReadAllText($ConfigFile) | ConvertFrom-Json).Environment
        } catch {
            Fail "Could not read the saved settings in ${ConfigFile}: $($_.Exception.Message)"
        }
    }
    # The bridge treats an empty value as unset, and PowerShell 7 can leave a
    # cleared variable defined but empty, so only non-empty values are kept.
    foreach ($name in $Names) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (-not $value -and $saved) { $value = $saved.$name }
        if ($value) { $Environment[$name] = [string]$value }
    }
}

# The accounts the MCP server reads from the same file, as bridges to run:
# each in the parent of its store_dir, on the port of its api_url. Every entry
# is checked before anything is installed.
function Read-AccountsFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail "Accounts file not found: $Path" }
    $Path = (Resolve-Path -LiteralPath $Path).ProviderPath
    try {
        # Wrapped in an object because PowerShell 7 unwraps a one-entry list and
        # Windows PowerShell has no -NoEnumerate.
        $entries = (ConvertFrom-Json -InputObject ('{"accounts":' + [System.IO.File]::ReadAllText($Path) + "`n}")).accounts
    } catch {
        Fail "Accounts file $Path is not valid JSON: $($_.Exception.Message)"
    }
    if ($entries -isnot [array]) { Fail "Accounts file $Path must hold a JSON list of accounts" }
    if ($entries.Count -eq 0) { Fail "Accounts file $Path lists no accounts; add at least one" }

    # A relative store_dir is relative to the accounts file, as in accounts.py.
    $baseDir = Split-Path -Parent $Path
    $names = @{}
    $ports = @{}
    $stores = @{}
    for ($i = 1; $i -le $entries.Count; $i++) {
        $entry = $entries[$i - 1]
        $label = "Accounts file ${Path}: entry $i"
        if ($entry -isnot [System.Management.Automation.PSCustomObject]) { Fail "$label must be a JSON object with name, store_dir, api_url" }
        if ($entry.name -is [string] -and $entry.name) { $label += " ('$($entry.name)')" }
        # The MCP server refuses these too, so no bridge is started for a file
        # it cannot use.
        $unknown = @($entry.PSObject.Properties.Name | Where-Object { $_ -cnotin 'name', 'store_dir', 'api_url' } | Sort-Object)
        if ($unknown.Count -gt 0) { Fail "$label has unknown field(s) $($unknown -join ', '); allowed fields are name, store_dir, api_url" }
        foreach ($field in 'name', 'store_dir', 'api_url') {
            if ($entry.$field -isnot [string] -or -not $entry.$field.Trim()) { Fail "$label is missing $field (a non-empty string)" }
        }
        $name = $entry.name
        if ($name -cnotmatch '^[a-z0-9-]+$') { Fail "$label has an invalid name; use only lowercase letters, digits and hyphens" }
        if ($names.ContainsKey($name)) { Fail "$label repeats the name '$name' of entry $($names[$name])" }
        $names[$name] = $i

        $apiUrl = $entry.api_url.Trim()
        if ($apiUrl -notmatch '^https?://(\[[^\]/]+\]|[^/:\[\]]+):([0-9]{1,5})(/|$)' -or [int]$Matches[2] -lt 1 -or [int]$Matches[2] -gt 65535) {
            Fail "$label has api_url '$apiUrl' without an explicit port; its bridge is started on that port, e.g. http://localhost:8081/api"
        }
        $port = [int]$Matches[2]
        if ($ports.ContainsKey($port)) { Fail "$label uses port $port, like entry $($ports[$port]); give each account its own port" }
        $ports[$port] = $i

        $storeDir = $entry.store_dir.Trim()
        if ($storeDir -eq '~' -or $storeDir -match '^~[\\/]') { $storeDir = $HOME + $storeDir.Substring(1) }
        $storeDir = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($baseDir, $storeDir)).TrimEnd('\')
        # The bridge keeps its data in store\ under its working directory.
        if ((Split-Path -Leaf $storeDir) -ne 'store') {
            Fail "$label has store_dir '$($entry.store_dir)', whose last folder is not 'store'; the bridge keeps its data in a folder named store"
        }
        # Two bridges on one store would run the same WhatsApp session. The
        # hashtable compares keys case-insensitively, like Windows paths.
        if ($stores.ContainsKey($storeDir)) { Fail "$label repeats the store_dir '$storeDir' of entry $($stores[$storeDir])" }
        $stores[$storeDir] = $i
        [pscustomobject]@{ Name = $name; WorkDir = (Split-Path -Parent $storeDir); Port = [string]$port }
    }
}

if ($env:OS -ne 'Windows_NT') { Fail 'Task Scheduler installation is only supported on Windows.' }
if (-not $env:LOCALAPPDATA) { Fail 'LOCALAPPDATA is not set.' }

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BridgeDir = Join-Path $RepoRoot 'whatsapp-bridge'
$BridgeBinary = Join-Path $BridgeDir 'whatsapp-bridge.exe'
$NewBinary = Join-Path $BridgeDir 'whatsapp-bridge.new.exe'

if (-not (Test-Path -LiteralPath $BridgeDir -PathType Container)) { Fail "Could not find bridge directory: $BridgeDir" }

$AppDir = Join-Path $env:LOCALAPPDATA 'whatsapp-mcp'
$NewLauncher = Join-Path $AppDir 'whatsapp-bridge-launcher.new.exe'
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()

# Each bridge gets its own task and its own folder for the launcher, runner,
# config and logs: the launcher finds the runner and the logs next to itself.
$bridges = @(if ($AccountsFile) {
    foreach ($account in Read-AccountsFile $AccountsFile) {
        $dir = Join-Path $AppDir $account.Name
        $environment = [ordered]@{ WHATSAPP_BRIDGE_PORT = $account.Port }
        Add-TaskEnvironment $environment (Join-Path $dir 'bridge-task.json') $PassThroughVars
        [pscustomobject]@{ TaskName = "$TaskName-$($account.Name)"; Dir = $dir; WorkDir = $account.WorkDir; Environment = $environment }
    }
} else {
    $environment = [ordered]@{ WHATSAPP_BRIDGE_PORT = '8080' }
    Add-TaskEnvironment $environment (Join-Path $AppDir 'bridge-task.json') (@('WHATSAPP_BRIDGE_PORT') + $PassThroughVars + 'WHATSAPP_BRIDGE_TOKEN')
    $Port = $environment.WHATSAPP_BRIDGE_PORT
    if ($Port -notmatch '^[0-9]{1,5}$' -or [int]$Port -lt 1 -or [int]$Port -gt 65535) {
        Fail "WHATSAPP_BRIDGE_PORT must be a number between 1 and 65535, got: $Port"
    }
    [pscustomobject]@{ TaskName = $TaskName; Dir = $AppDir; WorkDir = $BridgeDir; Environment = $environment }
})

foreach ($bridge in $bridges) { New-Item -ItemType Directory -Force -Path (Join-Path $bridge.Dir 'logs') | Out-Null }

# Build next to the current binary: Windows cannot overwrite an executable
# while the existing task is still running it.
$built = $false
if ($Binary) {
    if (-not (Test-Path -LiteralPath $Binary -PathType Leaf)) { Fail "Bridge binary not found: $Binary" }
    $BridgeBinary = (Resolve-Path -LiteralPath $Binary).ProviderPath
} elseif (Test-CgoToolchain) {
    Write-Host 'Building WhatsApp bridge...'
    Push-Location -LiteralPath $BridgeDir
    try { go build -o $NewBinary . } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Fail 'go build failed; see the output above.' }
    $built = $true
} elseif (Test-Path -LiteralPath $BridgeBinary -PathType Leaf) {
    Write-Host "Go with cgo is not available; using the existing $BridgeBinary"
} else {
    Fail "Go with cgo is not available and no bridge binary exists at $BridgeBinary. See the README's Windows section for the C compiler the bridge needs."
}

# The task runs this launcher instead of powershell.exe. Task Scheduler gives
# powershell.exe a console window, and Windows Terminal, the default terminal
# on Windows 11, turns -WindowStyle Hidden into a minimized window that stays
# in the taskbar and ends the runner when closed. A Windows-subsystem program
# never gets a window. The launcher also puts itself in a job object, so
# stopping the task ends the runner and the bridge with it; Task Scheduler
# does not guarantee that for child processes.
#
# Trade-off: the installer holds C# source and compiles it with the csc.exe
# that ships with Windows, and an unsigned program in %LOCALAPPDATA% may be
# flagged by antivirus software. Rejected alternatives: a hidden PowerShell
# relaunch still flashes and Task Scheduler stops tracking the runner;
# conhost --headless is undocumented and commonly flagged by security tools;
# VBScript is being deprecated; and "Run whether user is logged on or not"
# needs a right that standard accounts lack.
$launcherSource = @'
// Generated by scripts/install-task-windows.ps1; re-run the installer instead
// of editing this file.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

static class Launcher {
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
    const int JobObjectExtendedLimitInformation = 9;

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, int length);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();

    // The installer puts the runner and the logs folder next to the launcher.
    static readonly string AppDir = AppDomain.CurrentDomain.BaseDirectory;

    // Logging must never stop the launcher.
    static void Log(string message) {
        try {
            string logDir = Path.Combine(AppDir, "logs");
            Directory.CreateDirectory(logDir);
            File.AppendAllText(Path.Combine(logDir, "runner.log"), DateTime.Now.ToString("s") + " " + message + Environment.NewLine);
        } catch (Exception) {
        }
    }

    // Without the job object, stopping the task could leave a bridge running,
    // so nothing is started.
    static int FailJobSetup(string what) {
        int error = Marshal.GetLastWin32Error();
        Log("Could not " + what + ", so the bridge was not started: " + new Win32Exception(error).Message);
        return 1;
    }

    static int Main() {
        // The handle stays open until the launcher exits. Its children inherit
        // the job, and closing the last handle ends every process in it.
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) return FailJobSetup("create a job object");
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, ref limits, Marshal.SizeOf(limits))) {
            return FailJobSetup("configure the job object");
        }
        if (!AssignProcessToJobObject(job, GetCurrentProcess())) return FailJobSetup("join the job object");

        string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe");
        string runnerScript = Path.Combine(AppDir, "run-whatsapp-bridge.ps1");
        ProcessStartInfo info = new ProcessStartInfo(powershell,
            "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + runnerScript + "\"");
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        Process runner;
        try {
            runner = Process.Start(info);
        } catch (Exception e) {
            Log("Could not start the runner: " + e.Message);
            return 1;
        }
        runner.WaitForExit();
        Log("Runner exited with code " + runner.ExitCode + ".");
        return runner.ExitCode;
    }
}
'@

# Built before the existing task is stopped, so a failed build leaves it
# running.
Write-Host 'Building the task launcher...'
$launcherSourceFile = Join-Path $AppDir 'whatsapp-bridge-launcher.cs'
[System.IO.File]::WriteAllText($launcherSourceFile, $launcherSource, [System.Text.UTF8Encoding]::new($false))
try {
    $cscOutput = & (Get-CSharpCompiler) /nologo /target:winexe /optimize "/out:$NewLauncher" $launcherSourceFile
    if ($LASTEXITCODE -ne 0) {
        $cscOutput | Write-Host
        Fail 'csc.exe could not build the task launcher; see the output above.'
    }
} finally {
    Remove-Item -LiteralPath $launcherSourceFile -Force -ErrorAction SilentlyContinue
}

Write-Host 'Stopping the existing whatsapp-mcp tasks if present...'
foreach ($bridge in $bridges) { Stop-InstalledTask $bridge.TaskName }

if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    foreach ($bridge in $bridges) {
        $Port = $bridge.Environment.WHATSAPP_BRIDGE_PORT
        # The stopped bridge can take a moment to release the port.
        for ($i = 0; $i -lt 10; $i++) {
            $listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$Port) -ErrorAction SilentlyContinue)
            if ($listeners.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        if ($listeners.Count -gt 0) {
            foreach ($listener in $listeners) {
                $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
                Write-Host ("  {0}:{1} pid {2} {3}" -f $listener.LocalAddress, $listener.LocalPort, $listener.OwningProcess, $owner.Path)
            }
            Fail "Port $Port is already in use after stopping the existing $($bridge.TaskName) task."
        }
    }
}

if ($built) {
    try {
        Move-Item -LiteralPath $NewBinary -Destination $BridgeBinary -Force
    } catch {
        Fail "Could not replace $BridgeBinary; is a bridge you started by hand still running it? $($_.Exception.Message)"
    }
}
foreach ($bridge in $bridges) {
    $launcher = Join-Path $bridge.Dir 'whatsapp-bridge-launcher.exe'
    try {
        Copy-Item -LiteralPath $NewLauncher -Destination $launcher -Force
    } catch {
        Fail "Could not replace $launcher; is the launcher still running? $($_.Exception.Message)"
    }
}
Remove-Item -LiteralPath $NewLauncher -Force

$runner = @'
# Generated by scripts/install-task-windows.ps1; re-run the installer instead
# of editing this file. The task's launcher starts it at login without a
# window.
$ErrorActionPreference = 'Stop'

$config = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'bridge-task.json')) | ConvertFrom-Json
foreach ($entry in $config.Environment.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($entry.Name, [string]$entry.Value, 'Process')
}

# Logging must never stop the runner, or the bridge stays down until the next
# login.
function Write-RunnerLog([string]$Message) {
    $line = '{0} {1}' -f (Get-Date -Format s), $Message
    Add-Content -LiteralPath (Join-Path $config.LogDir 'runner.log') -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

# Appends without buffering, and shares the file so it can be read while the
# bridge runs.
function Open-Log([string]$Name) {
    [System.IO.FileStream]::new((Join-Path $config.LogDir $Name), [System.IO.FileMode]::Append,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]'ReadWrite, Delete', 1)
}

while ($true) {
    $out = $null
    $err = $null
    try {
        # Recreated on every run in case the logs were cleaned up by hand.
        New-Item -ItemType Directory -Force -Path $config.LogDir | Out-Null
        $out = Open-Log 'bridge.out.log'
        $err = Open-Log 'bridge.err.log'
        # The bridge has no terminal to draw a QR code in, so a bridge that
        # needs pairing opens it in the browser instead. --full-history-pair
        # only changes what a pairing asks the phone for, so a restart of a
        # linked bridge never triggers a resync, and every new or repeated
        # link asks for all the history the phone will give.
        $info = [System.Diagnostics.ProcessStartInfo]::new($config.BridgeBinary, '--qr-browser --full-history-pair')
        $info.WorkingDirectory = $config.BridgeDir
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $bridge = [System.Diagnostics.Process]::Start($info)
        Write-RunnerLog "Started bridge (pid $($bridge.Id))."
        $copies = [System.Threading.Tasks.Task[]]@(
            $bridge.StandardOutput.BaseStream.CopyToAsync($out),
            $bridge.StandardError.BaseStream.CopyToAsync($err)
        )
        $bridge.WaitForExit()
        [void][System.Threading.Tasks.Task]::WaitAll($copies, 5000)
        Write-RunnerLog "Bridge exited with code $($bridge.ExitCode)."
    } catch {
        Write-RunnerLog "Could not run the bridge: $($_.Exception.Message)"
    } finally {
        if ($out) { $out.Dispose() }
        if ($err) { $err.Dispose() }
    }
    Write-RunnerLog "Restarting in $($config.RestartDelaySeconds) seconds."
    Start-Sleep -Seconds $config.RestartDelaySeconds
}
'@
$utf8 = [System.Text.UTF8Encoding]::new($false)
# The runner restarts the bridge 10 seconds after it exits, like launchd's
# KeepAlive and ThrottleInterval; Task Scheduler's own restart setting cannot
# retry sooner than once a minute. The runner is meant to run for the whole
# session, so the task must never hit the default 72-hour time limit or refuse
# to start on battery power.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity.Name
$principal = New-ScheduledTaskPrincipal -UserId $Identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

foreach ($bridge in $bridges) {
    $configFile = Join-Path $bridge.Dir 'bridge-task.json'
    $config = [ordered]@{
        BridgeDir           = $bridge.WorkDir
        BridgeBinary        = $BridgeBinary
        LogDir              = Join-Path $bridge.Dir 'logs'
        RestartDelaySeconds = $RestartDelaySeconds
        Environment         = $bridge.Environment
    }
    # The config can hold WHATSAPP_BRIDGE_TOKEN, so only the current user may
    # read it. Restrict the file before anything is written into it, replacing
    # its whole access list: icacls /grant:r would keep other explicit entries,
    # such as the SYSTEM and Administrators entries a file can get when created
    # elevated.
    [System.IO.File]::WriteAllText($configFile, '', $utf8)
    $acl = [System.Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($Identity.User, 'FullControl', 'Allow'))
    # Set-Acl would also write the audit list, which needs a privilege that
    # standard accounts lack. .NET Framework (Windows PowerShell) has an
    # instance method; .NET in PowerShell 7 has an extension class instead.
    $file = [System.IO.FileInfo]::new($configFile)
    try {
        if ('System.IO.FileSystemAclExtensions' -as [type]) {
            [System.IO.FileSystemAclExtensions]::SetAccessControl($file, $acl)
        } else {
            $file.SetAccessControl($acl)
        }
    } catch {
        Fail "Could not restrict access to ${configFile}: $($_.Exception.Message)"
    }
    [System.IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json), $utf8)
    [System.IO.File]::WriteAllText((Join-Path $bridge.Dir 'run-whatsapp-bridge.ps1'), $runner, $utf8)

    $action = New-ScheduledTaskAction -Execute (Join-Path $bridge.Dir 'whatsapp-bridge-launcher.exe') -WorkingDirectory $bridge.WorkDir
    Write-Host "Registering the $($bridge.TaskName) Task Scheduler task..."
    Register-ScheduledTask -TaskName $bridge.TaskName -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force `
        -Description 'Starts the WhatsApp MCP bridge at login and restarts it when it exits. Installed by scripts/install-task-windows.ps1.' | Out-Null
    Start-ScheduledTask -TaskName $bridge.TaskName -TaskPath '\'

    Write-Host "Installed Task Scheduler task: $($bridge.TaskName)"
    Write-Host "Logs: $($config.LogDir)"
}
