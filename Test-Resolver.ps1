param(
    [string]$ResolverUrl = '',
    [string]$VideoId = 'C2elY5Tctqg'
)
$ErrorActionPreference = 'Stop'
if (-not $ResolverUrl) { $ResolverUrl = Read-Host 'Existing Render resolver HTTPS URL' }
$ResolverUrl = $ResolverUrl.Trim().TrimEnd('/')
$Parsed = [Uri]$ResolverUrl
if ($Parsed.Scheme -ne 'https' -or $Parsed.UserInfo -or $Parsed.Query -or $Parsed.Fragment -or $Parsed.AbsolutePath -ne '/') {
    throw 'Use only the resolver HTTPS origin.'
}
if ($VideoId -notmatch '^[A-Za-z0-9_-]{11}$') { throw 'Invalid YouTube ID.' }
$Secure = Read-Host 'Paste RESOLVER_SECRET (the existing Worker/resolver secret, hidden)' -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try { $Secret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer) }
$Headers = @{Authorization = 'Bearer ' + $Secret}
$OutputDirectory = Join-Path $PSScriptRoot 'verification'
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$Report = [ordered]@{videoId=$VideoId; checkedAtUtc=[DateTime]::UtcNow.ToString('o'); ok=$false}
try {
    $Report.health = Invoke-RestMethod -Uri "$ResolverUrl/health" -Headers $Headers -TimeoutSec 90
    if ($Report.health.version -ne 'v39.1-mp3-stream') { throw 'The resolver is not running V39.1 yet.' }
    Invoke-RestMethod -Method Post -Uri "$ResolverUrl/prepare/$VideoId" -Headers $Headers -TimeoutSec 30 | Out-Null
    $Deadline = [DateTime]::UtcNow.AddSeconds(250)
    do {
        $Status = Invoke-RestMethod -Uri "$ResolverUrl/jobs/$VideoId" -Headers $Headers -TimeoutSec 30
        if ($Status.state -eq 'failed') { $Report.job = $Status; throw ($Status.code + ': ' + $Status.message) }
        if ($Status.state -eq 'complete') { break }
        Write-Host ('Source/MP3 job is running. Bytes produced: ' + $Status.bytes)
        Start-Sleep -Seconds 3
    } while ([DateTime]::UtcNow -lt $Deadline)
    if ($Status.state -ne 'complete') { throw 'The resolver job did not complete before the test deadline.' }
    $Report.job = $Status
    $AudioPath = Join-Path $OutputDirectory ($VideoId + '.mp3')
    $Timer = [Diagnostics.Stopwatch]::StartNew()
    $Response = Invoke-WebRequest -UseBasicParsing -Uri "$ResolverUrl/completed/$VideoId" -Headers $Headers `
        -OutFile $AudioPath -PassThru -TimeoutSec 90
    $Timer.Stop()
    if ($Response.Headers['X-Veeb-MP3-Complete'] -ne '1') { throw 'The resolver did not mark this MP3 complete.' }
    $Length = (Get-Item $AudioPath).Length
    if ($Length -ne [long]$Status.bytes -or $Length -ne [long]$Response.Headers['Content-Length']) {
        throw 'The downloaded MP3 length does not match the completed job.'
    }
    $Report.ok = $true
    $Report.download = @{bytes=$Length; sha256=(Get-FileHash $AudioPath -Algorithm SHA256).Hash; elapsedMs=$Timer.ElapsedMilliseconds}
    Write-Host ('Completed MP3 saved: ' + $AudioPath) -ForegroundColor Green
    Write-Host 'Play this file. Then test the same track in Veeb and confirm the admin result says stored / R2 HIT.'
    Write-Host 'This script does not write R2; it proves resolver acquisition and completed MP3 delivery.'
} catch {
    $Report.error = $_.Exception.Message
    Write-Host $Report.error -ForegroundColor Red
} finally {
    $ReportPath = Join-Path $OutputDirectory ($VideoId + '-report.json')
    $Report | ConvertTo-Json -Depth 15 | Set-Content -Encoding UTF8 $ReportPath
    $Headers.Clear()
    $Secret = $null
    Write-Host ('Report saved: ' + $ReportPath)
}
if (-not $Report.ok) { exit 1 }
