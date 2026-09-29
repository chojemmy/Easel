$ErrorActionPreference = 'Stop'
$Profile = 'easel'
$Root = Split-Path -Parent $PSScriptRoot
$LogFile = Join-Path $env:TEMP 'easel-gateway.log'
$ErrorLogFile = Join-Path $env:TEMP 'easel-gateway.error.log'
$ConfigDir = Join-Path $HOME ".openclaw-$Profile"
$Port = 18789

function Test-Gateway {
    try { Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true }
    catch { return $false }
}

function Get-GatewayProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" |
        Where-Object { $_.CommandLine -match "openclaw.*--profile\s+$Profile.*gateway" } |
        Select-Object -First 1
}

function Stop-Gateway {
    $process = Get-GatewayProcess
    if ($process) { Stop-Process -Id $process.ProcessId -Force; Write-Host '[easel] Gateway stopped' }
    else { Write-Host '[easel] Gateway was not running' }
}

function Test-ProtectedEnvironment {
    $key = [Environment]::GetEnvironmentVariable('MINIMAX_API_KEY', 'Process')
    return -not [string]::IsNullOrWhiteSpace($key) -and -not $key.StartsWith('${')
}

# 直接运行本脚本不会解开 DPAPI 凭证，启动出的网关虽然 healthz 为绿，真正对话却会报
# SecretSurfaceUnavailableError。缺少进程内密钥时转交受保护的总入口；总入口注入密钥后
# 再次调用 gateway.ps1，此时不会递归。
if ($args[0] -in @('start', 'restart') -and -not (Test-ProtectedEnvironment)) {
    $serviceScript = Join-Path $PSScriptRoot 'easel-services.ps1'
    if (-not (Test-Path -LiteralPath $serviceScript)) {
        Write-Error "Protected Easel launcher not found: $serviceScript"
        exit 1
    }
    Write-Host '[easel] Protected credentials are not loaded; delegating to easel-services.ps1.'
    & powershell -NoProfile -ExecutionPolicy Bypass -File $serviceScript $args[0]
    exit $LASTEXITCODE
}

switch ($args[0]) {
    'start' {
        if (Test-Gateway) { Write-Host '[easel] Gateway already running'; break }
        New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
        Write-Host "[easel] Starting Easel gateway (profile: $Profile)..."
        $command = "openclaw --profile $Profile gateway run --force --allow-unconfigured --bind loopback"
        Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command', $command `
            -WorkingDirectory $Root -RedirectStandardOutput $LogFile -RedirectStandardError $ErrorLogFile -WindowStyle Hidden | Out-Null
        $ready = $false
        # First boot can spend 20-40 seconds loading providers/plugins before
        # the health endpoint is ready. Keep the visible launcher honest by
        # waiting for readiness instead of reporting a false failure.
        1..120 | ForEach-Object {
            if (-not $ready) {
                if (Test-Gateway) { $ready = $true }
                else { Start-Sleep -Milliseconds 500 }
            }
        }
        if ($ready) { Write-Host '[easel] Gateway started' }
        else { Write-Error "Gateway 启动失败；请检查 $LogFile 和 $ErrorLogFile"; exit 1 }
    }
    'stop' { Stop-Gateway }
    'restart' { Stop-Gateway; Start-Sleep -Seconds 2; & $PSCommandPath start }
    'status' {
        if (Test-Gateway) { Write-Host "[easel] Gateway running (profile: $Profile)" }
        else { Write-Host '[easel] Gateway not running' }
    }
    'logs' { Get-Content $LogFile -Wait }
    default { Write-Host 'Usage: gateway.ps1 {start|stop|restart|status|logs}'; exit 1 }
}
