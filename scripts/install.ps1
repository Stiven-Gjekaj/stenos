# Installs the stenos executable from the latest GitHub release.
#
# Usage, for the newest stable release:
#   irm https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.ps1 | iex
#
# For the newest pre-release, which is what every alpha is published as, or for
# one exact version:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.ps1))) -Pre
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.ps1))) -Version v0.1.3.19
#
# It refuses rather than guesses: an unsupported architecture, a missing
# checksum, or a checksum that does not match all stop the script. A wrong
# executable installed quietly is worse than no executable.

[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$Pre,
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\stenos"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = "Stiven-Gjekaj/stenos"

function Fail($message) {
    Write-Error "install.ps1: $message"
    exit 1
}

# --- Work out the platform ---------------------------------------------------

if ([Environment]::Is64BitOperatingSystem -ne $true) {
    Fail "no prebuilt executable for 32 bit Windows. Install from source with uv sync."
}

$architecture = (Get-CimInstance Win32_Processor | Select-Object -First 1).Architecture
if ($architecture -eq 12) {
    Fail "no prebuilt executable for Windows on ARM. Install from source with uv sync."
}

$target = "windows-x86_64"

# --- Work out the version ----------------------------------------------------

if (-not $Version) {
    if ($Pre) {
        # The full list is ordered newest first and omits drafts, so its first
        # entry is the newest release of any kind.
        $releases = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases"
        $Version = ($releases | Select-Object -First 1).tag_name
        if (-not $Version) {
            Fail "no releases have been published yet."
        }
    }
    else {
        # releases/latest is defined as the newest release that is neither a
        # draft nor a pre-release, which is exactly the stable one wanted here.
        # It answers 404 when there is no stable release, which is not an error
        # worth a stack trace when the advice is simply to pass -Pre.
        try {
            $latest = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
            $Version = $latest.tag_name
        }
        catch {
            $Version = ""
        }
        if (-not $Version) {
            Fail "no stable release yet. Every release so far is a pre-release, so run this again with -Pre to install the newest of those."
        }
    }
}

$archive = "stenos-$target.zip"
$base = "https://github.com/$Repo/releases/download/$Version"

Write-Host "Installing stenos $Version for $target"

# --- Download and check ------------------------------------------------------

$temp = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $temp -Force | Out-Null

try {
    $archivePath = Join-Path $temp $archive
    try {
        Invoke-WebRequest "$base/$archive" -OutFile $archivePath
    } catch {
        Fail "cannot download $archive. Check that $Version has a build for $target."
    }

    $sumsPath = Join-Path $temp "SHA256SUMS"
    try {
        Invoke-WebRequest "$base/SHA256SUMS" -OutFile $sumsPath
    } catch {
        Fail "cannot download SHA256SUMS. Refusing to install an unchecked executable."
    }

    $actual = (Get-FileHash $archivePath -Algorithm SHA256).Hash.ToLower()
    $expected = ""
    foreach ($line in Get-Content $sumsPath) {
        if ($line -match "\s\*?$([regex]::Escape($archive))$") {
            $expected = ($line -split "\s+")[0].ToLower()
            break
        }
    }

    if (-not $expected) {
        Fail "SHA256SUMS has no entry for $archive"
    }
    if ($actual -ne $expected) {
        Fail "the checksum does not match.`n  expected $expected`n  actual   $actual`nDo not use this download."
    }
    Write-Host "Checksum matches."

    # --- Install -------------------------------------------------------------

    Expand-Archive -Path $archivePath -DestinationPath $temp -Force
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item (Join-Path $temp "stenos-$target\stenos.exe") (Join-Path $InstallDir "stenos.exe") -Force

    Write-Host "Installed to $InstallDir\stenos.exe"

    # Tell the user only when it is true. A message about the PATH that appears
    # every time gets ignored the one time it matters.
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$InstallDir*") {
        Write-Host ""
        Write-Host "$InstallDir is not on your PATH. Add it with:"
        Write-Host "    setx PATH `"$InstallDir;`$env:PATH`""
    }

    & (Join-Path $InstallDir "stenos.exe") --version
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
