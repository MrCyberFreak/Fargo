<#
.SYNOPSIS
  Shallow-clone (or update) sibling pool-data repos into _ref/ for local Windows dev.

.DESCRIPTION
  Lets Fargo read a sibling's COMMITTED data without touching its local working copy
  (the hard project boundary). Nothing under _ref/ is committed; upstream code is
  never executed. Mirrors scripts/sync_ref.sh (the CI/bash twin).

.EXAMPLE
  pwsh scripts/sync_ref.ps1 NAPA APA-Scraper
  # set $env:REF_TOKEN first for a private sibling repo (optional)
#>
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Names)

$ErrorActionPreference = 'Stop'
$owner = 'MrCyberFreak'
$root = Split-Path -Parent $PSScriptRoot
$refDir = Join-Path $root '_ref'
New-Item -ItemType Directory -Force -Path $refDir | Out-Null

$auth = ''
if ($env:REF_TOKEN) { $auth = "x-access-token:$($env:REF_TOKEN)@" }

foreach ($name in $Names) {
  $dest = Join-Path $refDir $name
  $url = "https://$auth" + "github.com/$owner/$name.git"
  if (Test-Path (Join-Path $dest '.git')) {
    Write-Host "updating _ref/$name"
    git -C $dest fetch --depth 1 origin
    git -C $dest reset --hard FETCH_HEAD
  } else {
    Write-Host "cloning _ref/$name"
    git clone --depth 1 $url $dest
  }
}
