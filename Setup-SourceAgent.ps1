param(
    [string]$ProbeVideoId = 'C2elY5Tctqg',
    [switch]$Connect
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Install and start Docker Desktop first: https://docs.docker.com/desktop/setup/install/windows-install/'
}
& docker version --format '{{.Server.Version}}'
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop, then run this script again.' }

$ConfigFile = Join-Path $PSScriptRoot '.agent.env'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
if (-not (Test-Path $ConfigFile)) {
    $ResolverUrl = (Read-Host 'Paste the existing Render resolver HTTPS URL').Trim().TrimEnd('/')
    $Parsed = [Uri]$ResolverUrl
    if ($Parsed.Scheme -ne 'https' -or $Parsed.UserInfo -or $Parsed.Query -or $Parsed.Fragment -or $Parsed.AbsolutePath -ne '/') {
        throw 'Use only the HTTPS origin, for example https://your-resolver.onrender.com'
    }
    $RandomBytes = New-Object byte[] 32
    $Random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Random.GetBytes($RandomBytes) } finally { $Random.Dispose() }
    $AgentSecret = [Convert]::ToBase64String($RandomBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $ConfigText = "VEEB_RESOLVER_URL=$ResolverUrl`nVEEB_SOURCE_AGENT_SECRET=$AgentSecret`nVEEB_AGENT_CACHE_MB=1024`n"
    [IO.File]::WriteAllText($ConfigFile, $ConfigText, $Utf8)
}
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'agent-data') -Force | Out-Null

if (-not $Connect) {
    if ($ProbeVideoId -notmatch '^[A-Za-z0-9_-]{11}$') { throw 'ProbeVideoId must be an 11-character YouTube ID.' }
    & docker compose -f compose.agent.yaml build
    if ($LASTEXITCODE -ne 0) { throw 'The container build failed. The build includes the regression suite.' }
    & docker compose -f compose.agent.yaml run --rm source-agent --probe $ProbeVideoId
    if ($LASTEXITCODE -ne 0) {
        throw 'The full download/MP3 test failed on this computer. Keep Render in direct mode and save the printed failure.'
    }
    Write-Host 'Full MP3 test passed on this computer.' -ForegroundColor Green
    Write-Host 'In Render > resolver service > Environment, add:'
    Write-Host '  VEEB_SOURCE_MODE = agent'
    Write-Host '  VEEB_SOURCE_AGENT_SECRET = the value of that name in the local .agent.env file'
    Write-Host 'Open .agent.env locally in Notepad to copy the secret. Do not post it in chat or commit it.'
    Write-Host 'Save and redeploy Render, then run: .\Setup-SourceAgent.ps1 -Connect'
    exit 0
}

$Config = @{}
Get-Content $ConfigFile | ForEach-Object {
    if ($_ -match '^([A-Z_]+)=(.*)$') { $Config[$Matches[1]] = $Matches[2] }
}
try {
    Invoke-RestMethod -Method Post -Uri ($Config['VEEB_RESOLVER_URL'] + '/agent/heartbeat') `
        -Headers @{Authorization = 'Bearer ' + $Config['VEEB_SOURCE_AGENT_SECRET']} -TimeoutSec 90 | Out-Null
} catch {
    throw 'Render did not accept the source-agent settings. Deploy V39.0 and set VEEB_SOURCE_MODE and the matching agent secret, then retry -Connect.'
}
& docker compose -f compose.agent.yaml up -d
if ($LASTEXITCODE -ne 0) { throw 'Could not start the source agent.' }
Write-Host 'Source agent started. Keep this computer awake and Docker Desktop running.' -ForegroundColor Green
Write-Host 'Logs: docker compose -f compose.agent.yaml logs --tail 60 -f'
