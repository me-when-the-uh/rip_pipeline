# Rebuild the vgm2wav-mute submodule and refresh the bundled vgm2wav-mute.exe
# next to this script.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$sub  = Join-Path $root "vgm2wav-mute"

if (-not (Test-Path $sub)) {
    Write-Error "vgm2wav-mute submodule not found. Run: git submodule update --init --recursive"
    exit 1
}

# Ensure the nested libvgm submodule is present.
if (-not (Test-Path (Join-Path $sub "libvgm/.git"))) {
    Push-Location $sub
    git submodule update --init
    Pop-Location
}

# Build via the submodule's own build script.
& (Join-Path $sub "build.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Copy the freshly built executable next to this script.
$exe = Get-ChildItem -Path (Join-Path $sub "build") -Recurse -Filter "vgm2wav-mute.exe" |
       Select-Object -First 1
if (-not $exe) {
    Write-Error "vgm2wav-mute.exe was not produced by the build."
    exit 1
}
Copy-Item $exe.FullName -Destination (Join-Path $root "vgm2wav-mute.exe") -Force
Write-Host "Updated vgm2wav-mute.exe from $($exe.FullName)"
