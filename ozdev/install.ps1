# ozdev installer for Windows.
#
#   irm https://raw.githubusercontent.com/oizom-iot/public-data/main/ozdev/install.ps1 | iex
#
# Needs nothing but PowerShell 5+ — no Node, no git, no GitHub account.
# Everything it fetches is public.
#
# Override with environment variables:
#   OZDEV_REPO     release repo        (default oizom-iot/public-data)
#   OZDEV_BIN_DIR  where to install    (default %LOCALAPPDATA%\Programs\ozdev)
#   OZDEV_VERSION  a specific version  (default: newest published)

$ErrorActionPreference = 'Stop'

$Repo      = if ($env:OZDEV_REPO)    { $env:OZDEV_REPO }    else { 'oizom-iot/public-data' }
$BinDir    = if ($env:OZDEV_BIN_DIR) { $env:OZDEV_BIN_DIR } else { Join-Path $env:LOCALAPPDATA 'Programs\ozdev' }
$TagPrefix = 'ozdev-v'

function Fail($message) {
  Write-Host ''
  Write-Host "ozdev install failed: $message" -ForegroundColor Red
  exit 1
}

# --- what are we running on? -------------------------------------------------

# Only x64 is published. ARM Windows runs x64 binaries under emulation, so it is
# allowed through rather than refused.
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -notin @('AMD64', 'ARM64', 'x86')) { Fail "unsupported CPU: $arch" }
if ($arch -eq 'x86') { Fail '32-bit Windows is not supported' }
$asset = 'ozdev-windows-x64.exe'

# TLS 1.2 is not the default on Windows PowerShell 5, and GitHub requires it.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --- which version? ----------------------------------------------------------

if ($env:OZDEV_VERSION) {
  $tag = "$TagPrefix$($env:OZDEV_VERSION -replace '^v', '')"
} else {
  try {
    $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases" -Headers @{ 'User-Agent' = 'ozdev-installer' }
  } catch {
    Fail "could not read the release list from $Repo — $($_.Exception.Message)"
  }
  # That repo carries unrelated releases, so ours are found by tag prefix rather
  # than by trusting whichever release happens to be newest.
  $tag = ($releases | Where-Object { $_.tag_name -like "$TagPrefix*" -and -not $_.draft } | Select-Object -First 1).tag_name
  if (-not $tag) { Fail "no $TagPrefix* release found in $Repo" }
}

$base = "https://github.com/$Repo/releases/download/$tag"
Write-Host "Installing ozdev $($tag -replace "^$TagPrefix", '') for windows-x64"

# --- download, verify, install ----------------------------------------------

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("ozdev-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
  $staged = Join-Path $tmp 'ozdev.exe'
  try {
    # The progress bar makes Invoke-WebRequest dramatically slower on a
    # hundred-megabyte download, and it is redrawn far too often to be useful.
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri "$base/$asset" -OutFile $staged -UseBasicParsing
  } catch {
    Fail "could not download $base/$asset — $($_.Exception.Message)"
  }

  # The checksum is the difference between a truncated download and a mystery
  # crash three days later.
  try {
    $sums = (Invoke-WebRequest -Uri "$base/SHA256SUMS" -UseBasicParsing).Content
    $line = ($sums -split "`n" | Where-Object { $_.Trim().EndsWith($asset) } | Select-Object -First 1)
    if ($line) {
      $expected = ($line -split '\s+')[0]
      $actual = (Get-FileHash -Path $staged -Algorithm SHA256).Hash.ToLower()
      if ($expected.ToLower() -ne $actual) {
        Fail "checksum mismatch - expected $expected, got $actual. Nothing was installed."
      }
    }
  } catch {
    if ($_.Exception.Message -like '*checksum mismatch*') { throw }
    Write-Host '  (no SHA256SUMS published - skipping checksum)'
  }

  New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
  $target = Join-Path $BinDir 'ozdev.exe'
  # Move as one step: an interrupted install leaves the old ozdev or the new
  # one, never a half-written file.
  Move-Item -Path $staged -Destination $target -Force
  Write-Host "Installed $target"
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

# --- is it usable? -----------------------------------------------------------

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -split ';' -notcontains $BinDir) {
  # Written to the User scope so it survives a reboot, and mirrored into this
  # session so ozdev works in the window that ran the installer.
  [Environment]::SetEnvironmentVariable('Path', "$userPath;$BinDir", 'User')
  $env:Path = "$env:Path;$BinDir"
  Write-Host ''
  Write-Host "Added $BinDir to your PATH. Open a new terminal for it to stick."
}

Write-Host ''
Write-Host 'Next:  ozdev login'
