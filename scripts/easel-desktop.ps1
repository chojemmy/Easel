[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop')]
    [string]$Action = 'Start',
    [switch]$Quiet,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$ServiceScript = Join-Path $PSScriptRoot 'easel-services.ps1'
$LocalState = Join-Path $env:LOCALAPPDATA 'Easel'
$LogDir = Join-Path $LocalState 'logs'
$BrowserProfile = Join-Path $LocalState 'BrowserProfile'
$WebUrl = 'http://127.0.0.1:7860/'
$Host.UI.RawUI.WindowTitle = if ($Action -eq 'Start') { 'Easel 正在启动' } else { 'Easel 正在关闭' }

function Write-Step([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Cyan) {
    Write-Host "`n[Easel] $Message" -ForegroundColor $Color
}

function Write-LifecycleLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    "$(Get-Date -Format o) $Message" | Add-Content -LiteralPath (Join-Path $LogDir 'desktop.log') -Encoding UTF8
}

function Show-Notice([string]$Title, [string]$Message, [bool]$IsError = $false) {
    if ($Quiet) { return }
    try {
        Add-Type -AssemblyName PresentationFramework
        $icon = if ($IsError) { [System.Windows.MessageBoxImage]::Error } else { [System.Windows.MessageBoxImage]::Information }
        [System.Windows.MessageBox]::Show($Message, $Title, [System.Windows.MessageBoxButton]::OK, $icon) | Out-Null
    } catch {
        Write-Host $Message -ForegroundColor $(if ($IsError) { 'Red' } else { 'Green' })
    }
}

function Test-Endpoint([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Assert-ServicesHealthy {
    $checks = [ordered]@{
        '网页' = 'http://127.0.0.1:7860/api/status'
        '媒体桥' = 'http://127.0.0.1:7861/healthz'
        '网关' = 'http://127.0.0.1:18789/healthz'
    }
    foreach ($name in $checks.Keys) {
        if (-not (Test-Endpoint $checks[$name])) { throw "$name 尚未连接。" }
        Write-Host ("  [OK] {0}" -f $name) -ForegroundColor Green
    }
}

function Get-EdgePath {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    $command = Get-Command msedge.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw '未找到 Microsoft Edge，无法打开 Easel 专用网页窗口。'
}

function Get-EaselBrowserProcesses {
    $needle = [regex]::Escape($BrowserProfile)
    return @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'msedge.exe' -and [string]$_.CommandLine -match $needle
    })
}

function Start-EaselBrowser {
    if ((Get-EaselBrowserProcesses).Count -gt 0) {
        Write-Host '  [OK] Easel 专用网页窗口已在运行。' -ForegroundColor Green
        return
    }
    New-Item -ItemType Directory -Force -Path $BrowserProfile | Out-Null
    $edge = Get-EdgePath
    $arguments = @(
        "--user-data-dir=`"$BrowserProfile`"",
        "--app=$WebUrl",
        '--no-first-run',
        '--disable-session-crashed-bubble'
    )
    Start-Process -FilePath $edge -ArgumentList $arguments -WindowStyle Normal | Out-Null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ((Get-EaselBrowserProcesses).Count -gt 0) { break }
        Start-Sleep -Milliseconds 250
    }
    if ((Get-EaselBrowserProcesses).Count -eq 0) { throw 'Easel 服务已启动，但专用网页窗口未能打开。' }
    Write-Host '  [OK] Easel 专用网页窗口已打开。' -ForegroundColor Green
}

function Stop-EaselBrowser {
    $snapshot = @(Get-CimInstance Win32_Process)
    $roots = @(Get-EaselBrowserProcesses)
    if (-not $roots.Count) {
        Write-Host '  [OK] Easel 专用网页窗口原本就已关闭。' -ForegroundColor Green
        return
    }
    $selected = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($item in $roots) { [void]$selected.Add([int]$item.ProcessId) }
    do {
        $added = $false
        foreach ($item in $snapshot) {
            if ($selected.Contains([int]$item.ParentProcessId) -and $selected.Add([int]$item.ProcessId)) { $added = $true }
        }
    } while ($added)
    foreach ($item in @($snapshot | Where-Object { $selected.Contains([int]$_.ProcessId) })) {
        Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
    }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ((Get-EaselBrowserProcesses).Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    if ((Get-EaselBrowserProcesses).Count -gt 0) { throw 'Easel 专用网页窗口未完全关闭。' }
    Write-Host '  [OK] Easel 专用网页窗口已关闭。' -ForegroundColor Green
}

New-Item -ItemType Directory -Force -Path $LocalState,$LogDir | Out-Null
$mutex = New-Object System.Threading.Mutex($false, 'Local\EaselDesktopLifecycle')
$held = $false
try {
    try { $held = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) {
        Show-Notice 'Easel 正在处理' '已有一个 Easel 启动或关闭操作正在进行，请查看另一个状态窗口。'
        return
    }

    if ($Action -eq 'Start') {
        Write-Step '正在检查并启动网关、媒体桥和网页服务……'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ServiceScript start
        if ($LASTEXITCODE -ne 0) { throw 'Easel 后台服务启动失败。' }
        Write-Step '正在确认三个服务都已连接……'
        Assert-ServicesHealthy
        if (-not $NoBrowser) {
            Write-Step '正在打开 Easel 专用网页窗口……'
            Start-EaselBrowser
        }
        Write-Step '启动完成：网关已连接，Easel 可以使用。' Green
        Write-LifecycleLog 'Start succeeded'
        Show-Notice 'Easel 已启动' '网关、媒体桥和网页服务均已连接，Easel 网页已经打开。'
    } else {
        Write-Step '正在关闭 Easel 专用网页窗口和后台服务……'
        Stop-EaselBrowser
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ServiceScript stop
        if ($LASTEXITCODE -ne 0) { throw 'Easel 后台服务关闭失败。' }
        foreach ($url in @('http://127.0.0.1:7860/api/status', 'http://127.0.0.1:7861/healthz', 'http://127.0.0.1:18789/healthz')) {
            if (Test-Endpoint $url) { throw "仍有 Easel 服务未关闭：$url" }
        }
        Write-Step '关闭完成：网关、网页和媒体桥均已退出。' Green
        Write-LifecycleLog 'Stop succeeded'
        Show-Notice 'Easel 已关闭' '专用网页窗口、网关、媒体桥和网页服务均已关闭。'
    }
} catch {
    $message = $_.Exception.Message
    Write-Step "操作未完成：$message" Red
    Write-LifecycleLog "$Action failed: $message"
    Show-Notice 'Easel 操作失败' "$message`n`n日志目录：$LogDir" $true
    throw
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
