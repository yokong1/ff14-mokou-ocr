param(
    [switch]$Administrator,
    [switch]$Elevated,
    [switch]$CheckOnly,
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location -LiteralPath $root
$logName = if ($Administrator) { 'launch-admin.log' } else { 'launch.log' }
$logPath = Join-Path $root $logName
$utf8 = [Text.UTF8Encoding]::new($false)

function Write-LaunchLog([string]$Message) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $Message
    [IO.File]::AppendAllText($logPath, $line + [Environment]::NewLine, $utf8)
    Write-Host $line
}

function Get-PythonCandidates {
    $paths = [Collections.Generic.List[string]]::new()
    if ($PythonPath) { $paths.Add($PythonPath) }
    foreach ($relative in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe', 'python\python.exe')) {
        $paths.Add((Join-Path $root $relative))
    }
    foreach ($registryPath in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore',
                                 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem -LiteralPath $registryPath -ErrorAction SilentlyContinue)) {
            $install = Get-Item -LiteralPath (Join-Path $version.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($install) {
                $exe = $install.GetValue('ExecutablePath')
                if ($exe) { $paths.Add($exe) }
                $directory = $install.GetValue('')
                if ($directory) { $paths.Add((Join-Path $directory 'python.exe')) }
            }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
        foreach ($command in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            $paths.Add($command.Source)
        }
    }
    # 本机可用时作为备用；其他电脑不依赖这个路径。
    $paths.Add((Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'))
    $unique = @($paths | Select-Object -Unique | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
    return @($unique | Where-Object { $_ -notmatch '\\WindowsApps\\' }) +
           @($unique | Where-Object { $_ -match '\\WindowsApps\\' })
}

function Resolve-Python {
    foreach ($candidate in @(Get-PythonCandidates)) {
        $process = $null
        try {
            $info = [Diagnostics.ProcessStartInfo]::new()
            $info.FileName = $candidate
            $prefix = if ([IO.Path]::GetFileName($candidate) -ieq 'py.exe') { '-3 ' } else { '' }
            $info.Arguments = $prefix + '-c "import sys;import tkinter as tk;r=tk.Tk();r.withdraw();r.destroy();print(sys.executable)"'
            $info.UseShellExecute = $false
            $info.CreateNoWindow = $true
            $info.RedirectStandardOutput = $true
            $info.RedirectStandardError = $true
            $info.StandardOutputEncoding = $utf8
            $info.StandardErrorEncoding = $utf8
            $info.EnvironmentVariables['PYTHONUTF8'] = '1'
            $process = [Diagnostics.Process]::new()
            $process.StartInfo = $info
            [void]$process.Start()
            $stdout = $process.StandardOutput.ReadToEndAsync()
            $stderr = $process.StandardError.ReadToEndAsync()
            if (-not $process.WaitForExit(5000)) {
                $process.Kill()
                $process.WaitForExit()
                Write-LaunchLog "[SKIP] Python timed out: $candidate"
                continue
            }
            $text = $stdout.GetAwaiter().GetResult().Trim()
            $errorText = $stderr.GetAwaiter().GetResult().Trim()
            if ($process.ExitCode -eq 0 -and $text -and (Test-Path -LiteralPath $text -PathType Leaf)) {
                return $text
            }
            Write-LaunchLog "[SKIP] Python failed: $candidate $errorText"
        }
        catch { Write-LaunchLog "[SKIP] $candidate : $($_.Exception.Message)" }
        finally { if ($process) { $process.Dispose() } }
    }
    throw 'No usable Python 3 with Tkinter was found. Install Python from python.org, including Tcl/Tk.'
}

try {
    Write-LaunchLog '----- Start -----'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-LaunchLog "[INFO] Administrator=$isAdmin; requested=$Administrator"
    if ($Elevated -and -not $isAdmin) { throw 'The administrator token was not granted.' }
    $python = Resolve-Python
    Write-LaunchLog "[INFO] Python: $python"

    if ($Administrator -and -not $isAdmin) {
        Write-LaunchLog '[INFO] Waiting for UAC approval. Choose Yes to continue.'
        $shell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath +
                     '" -Administrator -Elevated -PythonPath "' + $python + '"'
        if ($CheckOnly) { $arguments += ' -CheckOnly' }
        $child = Start-Process -FilePath $shell -ArgumentList $arguments -WorkingDirectory $root -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop
        Write-LaunchLog "[INFO] Administrator process exited: $($child.ExitCode)"
        exit $child.ExitCode
    }

    if ($CheckOnly) {
        Write-LaunchLog '[OK] Python and Tkinter startup check passed.'
        exit 0
    }
    $app = Join-Path $root 'app.py'
    if (-not (Test-Path -LiteralPath $app -PathType Leaf)) { throw "Missing app.py: $app" }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $python
    $info.Arguments = '-u "' + $app + '"'
    $info.WorkingDirectory = $root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = $utf8
    $info.StandardErrorEncoding = $utf8
    $info.EnvironmentVariables['PYTHONUTF8'] = '1'
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $info
    [void]$process.Start()
    Write-LaunchLog "[INFO] App PID=$($process.Id)"
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $outputText = $stdout.GetAwaiter().GetResult()
    $errorText = $stderr.GetAwaiter().GetResult()
    if ($outputText) { [IO.File]::AppendAllText($logPath, $outputText, $utf8) }
    if ($errorText) { [IO.File]::AppendAllText($logPath, $errorText, $utf8) }
    $exitCode = $process.ExitCode
    $process.Dispose()
    Write-LaunchLog "[INFO] App exited: $exitCode"
    exit $exitCode
}
catch {
    Write-LaunchLog "[ERROR] $($_.Exception.Message)"
    Write-LaunchLog "[INFO] Details: $logPath"
    exit 1
}
