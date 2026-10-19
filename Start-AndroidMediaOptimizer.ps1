$ErrorActionPreference = 'Stop'
$mediaRoot = $PSScriptRoot
$mediaConfig = @{}
$mediaConfigPath = Join-Path $mediaRoot 'local-config.json'
if (Test-Path -LiteralPath $mediaConfigPath) { $mediaConfig = Get-Content -LiteralPath $mediaConfigPath -Raw | ConvertFrom-Json }
$mediaPython = $mediaConfig.service_python
if (-not $mediaPython) {
    $mediaPython = Join-Path $mediaRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $mediaPython)) { $mediaPython = (Get-Command python.exe -ErrorAction Stop).Source }
}
Push-Location $mediaRoot
try { & $mediaPython (Join-Path $mediaRoot 'work/phone-audit/launch_phone_compression.py') }
finally { Pop-Location }
