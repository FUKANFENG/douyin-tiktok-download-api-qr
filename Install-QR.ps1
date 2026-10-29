#requires -Version 7.0
param(
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [ValidatePattern('^[a-z0-9][a-z0-9_-]*$')][string]$Project = 'dtk-qr',
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'Linux / macOS 请按 README 中的 Docker 命令安装。' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw '请先安装并启动 Docker Desktop（Linux 容器模式）。' }
if ((& docker info --format '{{.OSType}}' 2>$null) -ne 'linux') { throw 'Docker 未就绪，或当前不是 Linux 容器模式。' }
Push-Location -LiteralPath $PSScriptRoot
try {
    & docker run --rm --mount "type=bind,source=$PSScriptRoot,target=/workspace" -w /workspace `
        --entrypoint python evil0ctal/douyin_tiktok_download_api:5.1.7 `
        scripts/init_qr.py --port $Port --project $Project
    if ($LASTEXITCODE -ne 0) { throw '初始化配置失败。' }
    $configPath = Join-Path $PSScriptRoot '.env.qr'
    $localOwner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $configPath /inheritance:r /grant:r "${localOwner}:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '无法限制配置文件权限。' }
    if ($NoStart) { return }
    $composeArgs = @('compose', '--env-file', '.env.qr', '-f', 'compose.qr.yaml')
    & docker @composeArgs build api qr-login
    if ($LASTEXITCODE -ne 0) { throw '镜像构建失败，请检查 Docker 网络连接。' }
    & docker @composeArgs up -d --no-build --wait --wait-timeout 240
    if ($LASTEXITCODE -ne 0) { throw '服务未全部就绪，请查看 docker compose 日志。' }
    $settings = @{}
    foreach ($line in Get-Content -LiteralPath $configPath -Encoding UTF8) {
        if ($line -match '^([A-Z_]+)=(.*)$') { $settings[$Matches[1]] = $Matches[2] }
    }
    $url = "http://127.0.0.1:$($settings['DTK_BIND_PORT'])"
    Write-Host "控制台已启动：$url"
    $setup = Invoke-RestMethod -Uri "$url/api/setup/status" -NoProxy -TimeoutSec 10
    if (-not $setup.data.initialized) {
        $setupLog = (& docker @composeArgs logs --no-log-prefix api 2>&1 | Out-String)
        $setupLinks = [regex]::Matches($setupLog, 'http://127\.0\.0\.1:8000/setup\?token=([A-Za-z0-9_-]+)')
        if ($setupLinks.Count -gt 0) {
            Write-Host '首次使用：打开以下本机初始化链接，创建你自己的管理员账号。'
            Write-Host "$url/setup?token=$($setupLinks[$setupLinks.Count - 1].Groups[1].Value)"
        }
    }
}
finally { Pop-Location }
