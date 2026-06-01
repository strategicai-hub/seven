# build-local.ps1 - Build Docker local + push GHCR + force-update stack Portainer
# Carrega credenciais de ~/.claude/.env
# Requer: Docker Desktop com buildx, GitHub CLI (gh) autenticado

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 0. Ignorar cert autoassinado do Portainer (PS 5.1)
Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllCerts : ICertificatePolicy {
    public bool CheckValidationResult(ServicePoint sp, X509Certificate cert, WebRequest req, int error) { return true; }
}
"@ -ErrorAction SilentlyContinue
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCerts
[System.Net.ServicePointManager]::SecurityProtocol = "Tls,Tls11,Tls12"

# 1. Carregar credenciais de ~/.claude/.env
$dotenv = Join-Path $env:USERPROFILE ".claude\.env"
if (-not (Test-Path $dotenv)) { Write-Error "~/.claude/.env nao encontrado" }
Get-Content $dotenv | ForEach-Object {
    if ($_ -match "^\s*([^#=\s]+)\s*=\s*(.*)$") {
        [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
    }
}
$PORTAINER_URL   = $env:PORTAINER_URL
$PORTAINER_TOKEN = $env:PORTAINER_TOKEN
if (-not $PORTAINER_URL)   { Write-Error "PORTAINER_URL nao definido em ~/.claude/.env" }
if (-not $PORTAINER_TOKEN) { Write-Error "PORTAINER_TOKEN nao definido em ~/.claude/.env" }

# 2. Constantes
$IMAGE      = "ghcr.io/strategicai-hub/seven:latest"
$SERVICES   = @("seven_seven-api", "seven_seven-worker", "seven_seven-scheduler")
$VERIFY_URL = "https://webhook-whatsapp.strategicai.com.br/seven/painel"
$projectRoot = $PSScriptRoot

# 3. Auth GHCR via DOCKER_CONFIG isolado (ver doc no CLAUDE.md global)
Write-Host "=== [1/4] Auth GHCR ===" -ForegroundColor Cyan
$ghStatus = cmd /c "gh auth status --hostname github.com 2>&1" | Out-String
$ghUserMatch = [regex]::Match($ghStatus, "account\s+(\S+)")
if (-not $ghUserMatch.Success) { Write-Error "Nao detectei a conta do gh CLI. Rode: gh auth login" }
$GHCR_USER = $ghUserMatch.Groups[1].Value
if ($env:GHCR_PAT) {
    $GHCR_TOKEN = $env:GHCR_PAT.Trim()
    $tokenSource = "GHCR_PAT (Classic PAT de ~/.claude/.env)"
} else {
    $GHCR_TOKEN = (gh auth token --hostname github.com).Trim()
    $tokenSource = "gh auth token (OAuth - pode falhar no push)"
}
if (-not $GHCR_TOKEN) {
    Write-Error @"
Sem token GHCR disponivel. Crie um Classic PAT em
https://github.com/settings/tokens/new?scopes=write:packages,read:packages,delete:packages
e adicione em ~/.claude/.env:  GHCR_PAT=ghp_xxxxxxxx
"@
}
Write-Host "Token: $tokenSource"
$authB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${GHCR_USER}:${GHCR_TOKEN}"))
$dockerCfgDir = Join-Path $env:TEMP "seven-docker-config"
if (Test-Path $dockerCfgDir) { Remove-Item $dockerCfgDir -Recurse -Force }
New-Item -ItemType Directory -Path $dockerCfgDir | Out-Null
$cfgJson = @{ auths = @{ "ghcr.io" = @{ auth = $authB64 } } } | ConvertTo-Json -Depth 5 -Compress
[IO.File]::WriteAllBytes(
    (Join-Path $dockerCfgDir "config.json"),
    [Text.UTF8Encoding]::new($false).GetBytes($cfgJson)
)
$env:DOCKER_CONFIG = $dockerCfgDir
Write-Host "Auth pronto como $GHCR_USER (DOCKER_CONFIG=$dockerCfgDir)" -ForegroundColor Green

# 4. Build e push
Write-Host "=== [2/4] Build + Push ===" -ForegroundColor Cyan
$builderName = "seven-builder"
$buildxList  = docker buildx ls
if (-not ($buildxList | Select-String $builderName)) {
    docker buildx create --name $builderName --driver docker-container --use | Out-Null
} else {
    docker buildx use $builderName | Out-Null
}
$metaFile = Join-Path $env:TEMP "seven-meta.json"
cmd /c "docker buildx build --platform linux/amd64 --push --tag $IMAGE --metadata-file `"$metaFile`" `"$projectRoot`" 2>&1"
if ($LASTEXITCODE -ne 0) { Write-Error "Build falhou." }

$meta   = Get-Content $metaFile -Raw | ConvertFrom-Json
$DIGEST = $meta."containerimage.digest"
if (-not $DIGEST) { Write-Error "Nao foi possivel extrair o digest." }
$IMAGE_REF = "${IMAGE}@${DIGEST}"
Write-Host "Digest: $DIGEST" -ForegroundColor Green

# 5. Force-update cada servico no Portainer
Write-Host "=== [3/4] Deploy via Portainer ===" -ForegroundColor Cyan
$baseUrl = $PORTAINER_URL.TrimEnd("/")
$headers = @{ "X-API-Key" = $PORTAINER_TOKEN; "Content-Type" = "application/json" }

foreach ($svcName in $SERVICES) {
    Write-Host "  Atualizando $svcName..."
    $svcResp = Invoke-RestMethod -Uri "$baseUrl/api/endpoints/1/docker/services/$svcName" -Headers $headers -Method Get
    $version = $svcResp.Version.Index
    $spec    = $svcResp.Spec | ConvertTo-Json -Depth 20 | ConvertFrom-Json
    $spec.TaskTemplate.ContainerSpec.Image = $IMAGE_REF
    $fu = if ($spec.TaskTemplate.PSObject.Properties["ForceUpdate"]) { $spec.TaskTemplate.ForceUpdate } else { 0 }
    $spec.TaskTemplate.ForceUpdate = $fu + 1
    $body = $spec | ConvertTo-Json -Depth 20
    Invoke-RestMethod -Uri "$baseUrl/api/endpoints/1/docker/services/$svcName/update?version=$version" `
        -Headers $headers -Method Post -Body $body | Out-Null
    Write-Host "  OK: $svcName -> $IMAGE_REF" -ForegroundColor Green
}

# 6. Verificar HTTP 200
Write-Host "=== [4/4] Verificando $VERIFY_URL ===" -ForegroundColor Cyan
$ok = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 4
    try { $code = (Invoke-WebRequest -Uri $VERIFY_URL -Method Get -TimeoutSec 5 -UseBasicParsing).StatusCode }
    catch { $code = 0 }
    Write-Host "[$i] HTTP $code"
    if ($code -eq 200) { $ok = $true; break }
}
if (-not $ok) { Write-Error "Servico nao respondeu HTTP 200 em 2 minutos." }

Write-Host ""
Write-Host "Deploy concluido!" -ForegroundColor Green
Write-Host "  Imagem  : $IMAGE_REF"
Write-Host "  Servicos: $($SERVICES -join ', ')"
Write-Host "  URL     : $VERIFY_URL"
