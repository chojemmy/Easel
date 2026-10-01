# Optional text-model credential, stored locally under Windows CurrentUser DPAPI.
# Dot-source after easel-secrets.ps1; no credentials are embedded in this script.
function Import-EaselWorkBuddyModelEnvironment([string]$LocalState) {
    $credentialPath = Join-Path $LocalState 'workbuddy-model.dpapi'
    if (-not (Test-Path -LiteralPath $credentialPath)) { return }
    $modelKey = $null
    try {
        $modelKey = Unprotect-Text (([System.IO.File]::ReadAllText($credentialPath)).Trim())
        if ([string]::IsNullOrWhiteSpace($modelKey)) { throw 'Empty credential.' }
        $env:EASEL_WORKBUDDY_API_KEY = $modelKey
    } catch {
        throw 'The protected WorkBuddy model credential cannot be read by this Windows user.'
    } finally {
        $modelKey = $null
    }
}

function Save-EaselWorkBuddyModelCredential([string]$LocalState, [string]$ModelsPath, [string]$ModelId) {
    $model = $null
    $modelKey = $null
    $models = $null
    try {
        $models = [System.IO.File]::ReadAllText($ModelsPath) | ConvertFrom-Json
        $matchesForModel = @($models | Where-Object { $_.id -ceq $ModelId })
        if ($matchesForModel.Count -ne 1) { throw 'Expected exactly one matching WorkBuddy model.' }
        $model = $matchesForModel[0]
        if ($model.apiKey -isnot [string] -or [string]::IsNullOrWhiteSpace($model.apiKey)) {
            throw 'The selected WorkBuddy model has no usable string API credential.'
        }
        $modelKey = [string]$model.apiKey
        New-Item -ItemType Directory -Force -Path $LocalState | Out-Null
        $path = Join-Path $LocalState 'workbuddy-model.dpapi'
        $temporary = Join-Path $LocalState ('.workbuddy-model-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        try {
            [System.IO.File]::WriteAllText($temporary, (Protect-Text $modelKey), (New-Object System.Text.UTF8Encoding($false)))
            if ([System.IO.File]::Exists($path)) { [System.IO.File]::Replace($temporary, $path, [NullString]::Value) }
            else { [System.IO.File]::Move($temporary, $path) }
        } finally {
            if ([System.IO.File]::Exists($temporary)) { Remove-Item -LiteralPath $temporary -Force }
        }
        Write-Host '[easel] WorkBuddy text-model credential saved with CurrentUser DPAPI.'
    } finally {
        $modelKey = $null; $model = $null; $models = $null; $matchesForModel = $null
    }
}
