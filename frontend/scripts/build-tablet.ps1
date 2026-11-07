# Build InspectionTablet.exe (Windows factory-tablet client)
# Requires: Rust, Node, VS Build Tools (C++), WebView2 Runtime
param(
  [string]$ApiUrl = "http://100.51.165.76:19686"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$cargoBin = "$env:USERPROFILE\.cargo\bin"
$vs = "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"
$msvcRoot = "$vs\VC\Tools\MSVC\14.44.35207"
$sdkRoot = "C:\Program Files (x86)\Windows Kits\10"
$sdk = "10.0.26100.0"

if (-not (Test-Path "$msvcRoot\bin\Hostx64\x64\link.exe")) {
  Write-Error "MSVC link.exe not found. Install VS Build Tools with Desktop C++ workload."
}

$env:Path = "$cargoBin;$msvcRoot\bin\Hostx64\x64;$sdkRoot\bin\$sdk\x64;" + $env:Path
$env:LIB = "$msvcRoot\lib\x64;$sdkRoot\Lib\$sdk\um\x64;$sdkRoot\Lib\$sdk\ucrt\x64"
$env:INCLUDE = "$msvcRoot\include;$sdkRoot\Include\$sdk\ucrt;$sdkRoot\Include\$sdk\um;$sdkRoot\Include\$sdk\shared;$sdkRoot\Include\$sdk\winrt"
$env:CARGO_TARGET_DIR = Join-Path $Root "src-tauri\target"

Set-Location $Root

Write-Host "VITE_API_URL = $ApiUrl"
Write-Host "VITE_TABLET_CLIENT = true"
$env:VITE_API_URL = $ApiUrl.TrimEnd("/")
$env:VITE_TABLET_CLIENT = "true"
$tauriJs = Join-Path $Root "node_modules\@tauri-apps\cli\tauri.js"
if (-not (Test-Path $tauriJs)) {
  Write-Error "Tauri CLI missing. From frontend run: npm install"
}
node $tauriJs build --no-bundle --config src-tauri/tauri.conf.tablet.json

$exe = Join-Path $env:CARGO_TARGET_DIR "release\autoballoon.exe"
$outDir = Join-Path $Root "dist-desktop"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$dest = Join-Path $outDir "InspectionTablet.exe"
try {
  Copy-Item $exe $dest -Force
  Write-Host "Built: $dest"
} catch {
  $alt = Join-Path $outDir "InspectionTablet.new.exe"
  Copy-Item $exe $alt -Force
  Write-Host ""
  Write-Host "Built: $exe" -ForegroundColor Green
  Write-Host "Could not overwrite $dest (app is running)." -ForegroundColor Yellow
  Write-Host "Saved as: $alt" -ForegroundColor Yellow
  Write-Host "Close InspectionTablet, then run:" -ForegroundColor Yellow
  Write-Host "  Copy-Item '$alt' '$dest' -Force" -ForegroundColor Cyan
}
