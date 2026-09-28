[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$Desktop = [Environment]::GetFolderPath('Desktop')
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Controller = Join-Path $PSScriptRoot 'easel-desktop.ps1'
$Icon = Join-Path $Root 'assets\easel.ico'

foreach ($required in @($PowerShell, $Controller, $Icon)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "缺少桌面快捷方式依赖：$required" }
}

$shell = New-Object -ComObject WScript.Shell
$items = @(
    @{
        Path = Join-Path $Desktop 'Easel.lnk'
        Action = 'Start'
        Description = '启动 Easel 网关、媒体桥和网页，并打开专用窗口'
    },
    @{
        Path = Join-Path $Desktop '退出 Easel 后台.lnk'
        Action = 'Stop'
        Description = '关闭 Easel 专用网页窗口、网关、媒体桥和网页服务'
    }
)

foreach ($item in $items) {
    $shortcut = $shell.CreateShortcut($item.Path)
    $shortcut.TargetPath = $PowerShell
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Normal -File `"$Controller`" -Action $($item.Action)"
    $shortcut.WorkingDirectory = $Root
    $shortcut.IconLocation = "$Icon,0"
    $shortcut.Description = $item.Description
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    Write-Host "[Easel] 已更新：$($item.Path)"
}
