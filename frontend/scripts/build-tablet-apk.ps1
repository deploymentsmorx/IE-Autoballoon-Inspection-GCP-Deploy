# Build InspectionTablet.apk for Android / Samsung factory tablets (not iOS).
param(
  [string]$ApiUrl = "http://100.51.165.76:19686",
  [string]$WebUrl = "http://100.51.165.76:8464"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Bundled = "C:\Users\ADMIN\Downloads\Quality_check_IE\Quality_inspection_report_IE\_buildtools"
$JdkCandidates = @(
  (Join-Path $Bundled "jdk\jdk-21.0.13.1+2"),
  "C:\Program Files\Eclipse Adoptium\jdk-21.0.13.9-hotspot",
  (Join-Path $Bundled "jdk\jdk-17.0.21.1+2")
)
$SdkCandidates = @(
  (Join-Path $env:LOCALAPPDATA "Android\Sdk"),
  (Join-Path $Bundled "sdk")
)
$Jdk = $JdkCandidates | Where-Object { Test-Path "$_\bin\java.exe" } | Select-Object -First 1
$Sdk = $SdkCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $Jdk) {
  Write-Error "JDK not found. Install Eclipse Temurin 17 or 21."
}
if (-not $Sdk) {
  Write-Error "Android SDK not found."
}

$env:JAVA_HOME = $Jdk
$env:ANDROID_HOME = $Sdk
$env:ANDROID_SDK_ROOT = $Sdk
$env:Path = "$Jdk\bin;$Sdk\platform-tools;" + $env:Path
$env:CAPACITOR_SERVER_URL = $WebUrl.TrimEnd("/")

$Shell = Join-Path (Split-Path -Parent $Root) "android-tablet"
if (-not (Test-Path (Join-Path $Shell "gradlew.bat"))) {
  Write-Error "android-tablet project is missing."
}
Write-Host "Building the same WebView APK as Quality Inspection"
Write-Host "Server = $($WebUrl.TrimEnd('/'))"

Set-Location $Shell
& .\gradlew.bat assembleDebug --no-daemon
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$apk = Join-Path $Shell "app\build\outputs\apk\debug\app-debug.apk"
$outDir = Join-Path $Root "dist-desktop"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$dest = Join-Path $outDir "InspectionReport.apk"
Copy-Item $apk $dest -Force
$downloads = Join-Path $env:USERPROFILE "Downloads\InspectionReport.apk"
Copy-Item $apk $downloads -Force
Remove-Item (Join-Path $outDir "InspectionTablet.apk") -Force -ErrorAction SilentlyContinue
Write-Host "Built: $dest"
Write-Host "Copied: $downloads"
