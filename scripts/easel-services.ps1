[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('import-1password', 'start', 'stop', 'restart', 'status', 'self-test', 'exec')]
    [string]$Action = 'status',

    [string]$OnePasswordItem = 'minimax api',
    [string]$OnePasswordField = '',
    [string]$ApiboboOnePasswordItem = 'apibobo',
    [string]$ApiboboOnePasswordField = '',
    [switch]$RotateMediaToken,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easel-workbuddy-model.ps1')
$Root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LocalState = if ($env:EASEL_LOCAL_STATE) { $env:EASEL_LOCAL_STATE } else { Join-Path $env:LOCALAPPDATA 'Easel' }
$SecretFile = Join-Path $LocalState 'secrets.dpapi.json'
$LogDir = Join-Path $LocalState 'logs'
$GatewayScript = Join-Path $PSScriptRoot 'gateway.ps1'

# Windows 中文区域的 Python 默认标准输出为 GBK；Easel 的脚本日志包含 emoji。
# 服务及其后代进程统一使用 UTF-8，避免一次日志输出失败破坏真实业务状态。
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Write-Status([string]$Message) {
    Write-Host "[easel] $Message"
}

function Ensure-LocalState {
    New-Item -ItemType Directory -Force -Path $LocalState | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

    # The payload is already protected by CurrentUser DPAPI. Restrict the local
    # directory as a second layer without making startup depend on ACL support.
    try {
        $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        $security = New-Object System.Security.AccessControl.DirectorySecurity
        $security.SetOwner($sid)
        $security.SetAccessRuleProtection($true, $false)
        $rights = [System.Security.AccessControl.FileSystemRights]::FullControl
        $inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
        $propagation = [System.Security.AccessControl.PropagationFlags]::None
        $allow = [System.Security.AccessControl.AccessControlType]::Allow
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $rights, $inheritance, $propagation, $allow)
        $security.AddAccessRule($rule)
        [System.IO.Directory]::SetAccessControl($LocalState, $security)
    } catch {
        Write-Warning 'Could not tighten the Easel local-state ACL. DPAPI protection remains active.'
    }
}

function Protect-Text([string]$PlainText) {
    $secure = ConvertTo-SecureString -String $PlainText -AsPlainText -Force
    try {
        return ConvertFrom-SecureString -SecureString $secure
    } finally {
        $secure.Dispose()
    }
}

function Unprotect-Text([string]$CipherText) {
    $secure = ConvertTo-SecureString -String $CipherText
    try {
        return [System.Net.NetworkCredential]::new('', $secure).Password
    } finally {
        $secure.Dispose()
    }
}

function New-MediaToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    } finally {
        $rng.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Get-OnePasswordCredential(
    [string]$OpPath,
    [string]$ItemName,
    [string]$FieldName,
    [string]$Label
) {
    Write-Status "Requesting the '$ItemName' item from 1Password for $Label..."
    $raw = & $OpPath item get $ItemName --format json --reveal
    if ($LASTEXITCODE -ne 0) {
        throw "1Password did not return the requested $Label item."
    }
    try {
        $item = $raw | ConvertFrom-Json
        $fields = @($item.fields | Where-Object {
            $_.type -eq 'CONCEALED' -and -not [string]::IsNullOrWhiteSpace([string]$_.value)
        })
        if ($FieldName) {
            $fields = @($fields | Where-Object { $_.label -eq $FieldName -or $_.id -eq $FieldName })
        }
        if ($fields.Count -ne 1) {
            throw "Expected exactly one concealed credential field for $Label; found $($fields.Count). Select a field explicitly."
        }
        return [pscustomobject]@{
            PlainText = [string]$fields[0].value
            Source = [ordered]@{
                provider = '1Password'
                itemId = [string]$item.id
                itemTitle = [string]$item.title
                fieldId = [string]$fields[0].id
                fieldLabel = [string]$fields[0].label
            }
        }
    } finally {
        $raw = $null
        $item = $null
        $fields = $null
    }
}

function Import-FromOnePassword {
    Ensure-LocalState
    $op = Get-Command op -ErrorAction SilentlyContinue
    if (-not $op) {
        throw '1Password CLI (op) was not found.'
    }

    $minimaxCredential = Get-OnePasswordCredential $op.Source $OnePasswordItem $OnePasswordField 'MiniMax'
    $apiboboCredential = Get-OnePasswordCredential $op.Source $ApiboboOnePasswordItem $ApiboboOnePasswordField 'APIBOBO'
    $plainKey = [string]$minimaxCredential.PlainText
    $apiboboKey = [string]$apiboboCredential.PlainText
    if ([string]::IsNullOrWhiteSpace($plainKey) -or [string]::IsNullOrWhiteSpace($apiboboKey)) {
        throw 'A selected 1Password credential is empty.'
    }

    $mediaToken = $null
    if ((Test-Path -LiteralPath $SecretFile) -and -not $RotateMediaToken) {
        try {
            $existing = Get-Content -LiteralPath $SecretFile -Raw | ConvertFrom-Json
            if ($existing.easelMediaToken) {
                $mediaToken = Unprotect-Text ([string]$existing.easelMediaToken)
            }
        } catch {
            Write-Warning 'The previous media token could not be reused; a new one will be generated.'
        }
    }
    if ([string]::IsNullOrWhiteSpace($mediaToken)) {
        $mediaToken = New-MediaToken
    }

    try {
        $payload = [ordered]@{
            version = 2
            protection = 'Windows DPAPI CurrentUser'
            importedAt = [DateTimeOffset]::Now.ToString('o')
            sources = [ordered]@{
                minimax = $minimaxCredential.Source
                apibobo = $apiboboCredential.Source
            }
            minimaxApiKey = Protect-Text $plainKey
            apiboboApiKey = Protect-Text $apiboboKey
            easelMediaToken = Protect-Text $mediaToken
        }
        $json = $payload | ConvertTo-Json -Depth 8
        $tempFile = Join-Path $LocalState ('.secrets-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        try {
            [System.IO.File]::WriteAllText($tempFile, $json, (New-Object System.Text.UTF8Encoding($false)))
            Move-Item -LiteralPath $tempFile -Destination $SecretFile -Force
        } finally {
            Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue
        }
    } finally {
        $plainKey = $null
        $apiboboKey = $null
        $mediaToken = $null
        $minimaxCredential = $null
        $apiboboCredential = $null
        $payload = $null
        $json = $null
        [GC]::Collect()
    }
    Write-Status "Credentials imported into the local DPAPI store: $SecretFile"
}

function Import-DotEnv {
    $path = Join-Path $Root '.env'
    if (-not (Test-Path -LiteralPath $path)) { return }
    foreach ($line in Get-Content -LiteralPath $path) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { continue }
        $name = $matches[1]
        $value = $matches[2].Trim().Trim('"').Trim("'")
        if ($value -match '^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$') {
            $resolved = [Environment]::GetEnvironmentVariable($matches[1], 'Process')
            if ([string]::IsNullOrWhiteSpace($resolved)) { continue }
            $value = $resolved
        }
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

function Import-ProtectedEnvironment {
    if (-not (Test-Path -LiteralPath $SecretFile)) {
        throw "Protected credentials are missing. Run: powershell -File `"$PSCommandPath`" import-1password"
    }
    $store = Get-Content -LiteralPath $SecretFile -Raw | ConvertFrom-Json
    if ($store.protection -ne 'Windows DPAPI CurrentUser') {
        throw 'Unsupported Easel secret-store format.'
    }
    $minimax = Unprotect-Text ([string]$store.minimaxApiKey)
    $apibobo = if ($store.apiboboApiKey) { Unprotect-Text ([string]$store.apiboboApiKey) } else { '' }
    $media = Unprotect-Text ([string]$store.easelMediaToken)
    if ([string]::IsNullOrWhiteSpace($minimax) -or [string]::IsNullOrWhiteSpace($apibobo) -or [string]::IsNullOrWhiteSpace($media)) {
        throw 'The Easel secret store is incomplete. Re-run import-1password to import MiniMax and APIBOBO.'
    }
    try {
        $env:MINIMAX_API_KEY = $minimax
        $env:APIBOBO_KEY = $apibobo
        $env:EASEL_MEDIA_TOKEN = $media
        $env:EASEL_ROOT = $Root
        Import-EaselWorkBuddyModelEnvironment $LocalState
        Import-DotEnv
    } finally {
        $minimax = $null
        $apibobo = $null
        $media = $null
        $store = $null
    }
}

function Clear-ProtectedEnvironment {
    foreach ($name in @(
        'MINIMAX_API_KEY', 'APIBOBO_KEY', 'EASEL_MEDIA_TOKEN',
        'EASEL_LLM_API_KEY', 'EASEL_WORKBUDDY_API_KEY', 'IMG_API_KEY', 'VIDEO_API_KEY', 'EASEL_EMBEDDING_API_KEY'
    )) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
}

function Get-PortProcess([int]$Port) {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $connection) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
}

function Test-ManagedProcess($Process, [string]$Kind) {
    if (-not $Process) { return $false }
    $commandLine = [string]$Process.CommandLine
    switch ($Kind) {
        'web' {
            return $commandLine -match [regex]::Escape($Root) -and $commandLine -match 'web\.app:app'
        }
        'media' {
            return $commandLine -match [regex]::Escape($Root) -and $commandLine -match 'scripts\.local_media_bridge:app'
        }
        'gateway' {
            return $commandLine -match 'openclaw' -and $commandLine -match '--profile\s+easel' -and $commandLine -match 'gateway'
        }
    }
    return $false
}

function Stop-ManagedPort([int]$Port, [string]$Kind) {
    $process = Get-PortProcess $Port
    if (-not $process) { return }
    if (-not (Test-ManagedProcess $process $Kind)) {
        throw "Port $Port is owned by an unexpected process (PID $($process.ProcessId)); refusing to stop it."
    }
    Stop-Process -Id $process.ProcessId -Force
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (-not (Get-PortProcess $Port)) { return }
        Start-Sleep -Milliseconds 200
    }
    throw "Easel $Kind process did not release port $Port."
}

function Test-Http([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Wait-Http([string]$Url, [string]$Name) {
    1..40 | ForEach-Object {
        if (-not $script:ready) {
            if (Test-Http $Url) { $script:ready = $true }
            else { Start-Sleep -Milliseconds 500 }
        }
    }
    if (-not $script:ready) { throw "$Name did not become healthy: $Url" }
    $script:ready = $false
}

function Start-EaselServices {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "Easel Python environment was not found: $Python"
    }
    Ensure-LocalState
    Import-ProtectedEnvironment
    try {
        if (Get-PortProcess 7861) {
            if (-not (Test-ManagedProcess (Get-PortProcess 7861) 'media')) { throw 'Port 7861 is already in use.' }
        } else {
            Write-Status 'Starting MiniMax media bridge on 127.0.0.1:7861...'
            Start-Process -FilePath $Python -ArgumentList @('-m', 'uvicorn', 'scripts.local_media_bridge:app', '--host', '127.0.0.1', '--port', '7861', '--log-level', 'warning') -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir 'media.log') -RedirectStandardError (Join-Path $LogDir 'media.error.log') | Out-Null
        }
        Wait-Http 'http://127.0.0.1:7861/healthz' 'MiniMax media bridge'

        if (-not (Test-Http 'http://127.0.0.1:18789/healthz')) {
            Write-Status 'Starting OpenClaw gateway on 127.0.0.1:18789...'
            & powershell -NoProfile -ExecutionPolicy Bypass -File $GatewayScript start
            if ($LASTEXITCODE -ne 0) { throw 'OpenClaw gateway failed to start.' }
        }

        if (Get-PortProcess 7860) {
            if (-not (Test-ManagedProcess (Get-PortProcess 7860) 'web')) { throw 'Port 7860 is already in use.' }
        } else {
            Write-Status 'Starting Easel Web on 127.0.0.1:7860...'
            Start-Process -FilePath $Python -ArgumentList @('-m', 'uvicorn', 'web.app:app', '--host', '127.0.0.1', '--port', '7860', '--log-level', 'warning') -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir 'web.log') -RedirectStandardError (Join-Path $LogDir 'web.error.log') | Out-Null
        }
        Wait-Http 'http://127.0.0.1:7860/api/status' 'Easel Web'
    } finally {
        Clear-ProtectedEnvironment
    }
    Write-Status 'All Easel services are healthy.'
}

function Stop-EaselServices {
    Write-Status 'Stopping Easel services...'
    Stop-ManagedPort 7860 'web'
    Stop-ManagedPort 7861 'media'
    Stop-ManagedPort 18789 'gateway'
    Write-Status 'Easel services stopped.'
}

function Show-EaselStatus {
    $checks = @(
        @('Web', 'http://127.0.0.1:7860/api/status'),
        @('Media', 'http://127.0.0.1:7861/healthz'),
        @('Gateway', 'http://127.0.0.1:18789/healthz')
    )
    foreach ($check in $checks) {
        $state = if (Test-Http $check[1]) { 'healthy' } else { 'stopped/unhealthy' }
        Write-Host ("{0,-8} {1}" -f $check[0], $state)
    }
    Write-Host ("Secrets  {0}" -f $(if (Test-Path -LiteralPath $SecretFile) { 'DPAPI store present' } else { 'not imported' }))
}

function Test-SecureIntegration {
    Import-ProtectedEnvironment
    try {
        $headers = @{ Authorization = 'Bearer ' + $env:EASEL_MEDIA_TOKEN }
        $media = Invoke-WebRequest -Uri 'http://127.0.0.1:7861/v1/models' -Headers $headers -UseBasicParsing -TimeoutSec 10
        if ($media.StatusCode -ne 200) { throw 'The media bridge rejected its protected token.' }
        Write-Status 'Media bridge token: OK'

        $quota = Invoke-WebRequest -Uri 'http://127.0.0.1:7860/api/minimax/quota' -UseBasicParsing -TimeoutSec 30
        if ($quota.StatusCode -ne 200) { throw 'The MiniMax quota endpoint did not accept the protected API key.' }
        $quotaBody = $quota.Content | ConvertFrom-Json
        if ($null -eq $quotaBody.rows) { throw 'The MiniMax quota response was not recognized.' }
        Write-Status "MiniMax Token Plan credential: OK ($(@($quotaBody.rows).Count) quota rows)"

        $bobo = Get-Command bobo -ErrorAction SilentlyContinue
        if (-not $bobo) { throw 'APIBOBO CLI (bobo) was not found.' }
        $balanceRaw = & $bobo.Source balance -j 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'APIBOBO rejected the protected credential.' }
        $balanceBody = $balanceRaw | ConvertFrom-Json
        if ($null -eq $balanceBody) { throw 'The APIBOBO balance response was not recognized.' }
        Write-Status 'APIBOBO credential: OK'
    } finally {
        $headers = $null
        $balanceRaw = $null
        $balanceBody = $null
        Clear-ProtectedEnvironment
    }
}

function Invoke-WithProtectedEnvironment {
    if (-not $CommandArgs -or $CommandArgs.Count -eq 0) {
        throw 'exec requires a command.'
    }
    Import-ProtectedEnvironment
    try {
        $program = $CommandArgs[0]
        $arguments = @()
        if ($CommandArgs.Count -gt 1) { $arguments = $CommandArgs[1..($CommandArgs.Count - 1)] }
        & $program @arguments
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 0 }
    } finally {
        Clear-ProtectedEnvironment
    }
    exit $exitCode
}

switch ($Action) {
    'import-1password' { Import-FromOnePassword }
    'start' { Start-EaselServices }
    'stop' { Stop-EaselServices }
    'restart' { Stop-EaselServices; Start-EaselServices }
    'status' { Show-EaselStatus }
    'self-test' { Test-SecureIntegration }
    'exec' { Invoke-WithProtectedEnvironment }
}
