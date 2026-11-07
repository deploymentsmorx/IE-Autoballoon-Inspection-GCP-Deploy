# Build InspectionTablet.apk for Android / Samsung factory tablets (not iOS).
param(
  [string]$ApiUrl = "http://100.51.165.76:19686"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Jdk = "C:\Program Files\Eclipse Adoptium\jdk-21.0.13.9-hotspot"
$Sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk"

if (-not (Test-Path "$Jdk\bin\java.exe")) {
  Write-Error "JDK 21 not found at $Jdk. Install Eclipse Temurin 21."
}
if (-not (Test-Path $Sdk)) {
  Write-Error "Android SDK not found at $Sdk"
}

$env:JAVA_HOME = $Jdk
$env:ANDROID_HOME = $Sdk
$env:ANDROID_SDK_ROOT = $Sdk
$env:Path = "$Jdk\bin;$Sdk\platform-tools;" + $env:Path
$env:VITE_API_URL = $ApiUrl.TrimEnd("/")
$env:VITE_TABLET_CLIENT = "true"

Set-Location $Root
Write-Host "VITE_API_URL = $env:VITE_API_URL"
Write-Host "VITE_TABLET_CLIENT = true"

npm run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

npx cap sync android
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Set-Location (Join-Path $Root "android")
& .\gradlew.bat assembleDebug --no-daemon
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$apk = Join-Path $Root "android\app\build\outputs\apk\debug\app-debug.apk"
$outDir = Join-Path $Root "dist-desktop"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$dest = Join-Path $outDir "InspectionReport.apk"
Copy-Item $apk $dest -Force
Copy-Item $apk "C:\Users\AIMLE\Downloads\InspectionReport.apk" -Force
Remove-Item (Join-Path $outDir "InspectionTablet.apk") -Force -ErrorAction SilentlyContinue
Remove-Item "C:\Users\AIMLE\Downloads\InspectionTablet.apk" -Force -ErrorAction SilentlyContinue
Write-Host "Built: $dest"
Write-Host "Copied: C:\Users\AIMLE\Downloads\InspectionReport.apk"
