[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$templatePath = Join-Path $repoRoot '.env.example'
$envPath = Join-Path $repoRoot '.env'

if (Test-Path -LiteralPath $envPath) {
    throw "$envPath already exists; refusing to overwrite local secrets."
}

function New-HexSecret([int]$ByteCount) {
    $bytes = New-Object byte[] $ByteCount
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
}

$content = Get-Content -Raw -Encoding utf8 -LiteralPath $templatePath
$content = $content -replace '(?m)^SECRET_KEY_BASE=.*$', ('SECRET_KEY_BASE=' + (New-HexSecret 64))
$content = $content -replace '(?m)^POSTGRES_PASSWORD=.*$', ('POSTGRES_PASSWORD=' + (New-HexSecret 32))
$content = $content -replace '(?m)^REDIS_PASSWORD=.*$', ('REDIS_PASSWORD=' + (New-HexSecret 32))

Set-Content -NoNewline -Encoding utf8 -LiteralPath $envPath -Value $content
Write-Host "Created ignored local configuration: $envPath"
Write-Host 'Chatwoot API and mailbox credentials remain blank for attended setup.'
