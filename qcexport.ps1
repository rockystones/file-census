<#
.SYNOPSIS
  Recursive metadata listing of a folder tree to CSV, for import by qcimport.py.

.DESCRIPTION
  For machines where qc.py cannot read a tree (permission scoping, no Python, an
  OneDrive root that only this logged-on user can enumerate). Metadata only: it
  lists directory entries and never opens or reads a file, so nothing is modified
  and no cloud placeholder is hydrated.

  Runs from cmd.exe:
    powershell -ExecutionPolicy Bypass -File qcexport.ps1 -Path "C:\Users\me\OneDrive"

.PARAMETER Path
  Folder to list (repeatable via comma: -Path "A","B").

.PARAMETER Out
  CSV output path. Default: .\qcexport_<foldername>_<timestamp>.csv

.PARAMETER MaxDepth
  Safety cap on recursion depth (default 64).

.PARAMETER FollowLinks
  Descend into junctions/symlinks too. Off by default (cycle-safe); a visited-path
  guard is applied either way.

.PARAMETER Retries
  Retries for a folder listing that fails transiently (default 2). Network shares drop
  connections briefly; without a retry those folders silently become gaps.

.PARAMETER SkipCloud
  Do NOT descend into cloud placeholder folders (OneDrive online-only). By default
  they ARE descended, because listing them is usually the reason for running this.
  Cloud folders carry the same ReparsePoint attribute as junctions, so they are told
  apart by LinkType: a real junction/symlink reports one, a cloud folder does not.
  Enumeration reads metadata only and never downloads file content.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [string]$Out,
    [int]$MaxDepth = 64,
    [int]$Retries = 2,
    [switch]$FollowLinks,
    [switch]$SkipCloud
)

$ErrorActionPreference = 'Continue'
$FILE_ATTRIBUTE_DIRECTORY    = 0x10
$FILE_ATTRIBUTE_REPARSE_POINT = 0x400

if (-not $Out) {
    $tag = (Split-Path $Path[0] -Leaf) -replace '[^A-Za-z0-9._-]', '_'
    $Out = Join-Path (Get-Location) ("qcexport_{0}_{1}.csv" -f $tag, (Get-Date -Format 'yyyyMMddHHmm'))
}
$errPath = [System.IO.Path]::ChangeExtension($Out, '.errors.txt')

function Get-RootIdentity([string]$p) {
    # A network share's identity is its UNC target, not the drive letter it happens to
    # have on this machine for this user. Resolve it so the catalogue can record it.
    $info = [ordered]@{ path = $p; unc = $null; driveType = 'local';
                        volumeLabel = $null; fileSystem = $null; serial = $null }
    if ($p -like '\\*') {
        $info.unc = ($p -replace '^(\\\\[^\\]+\\[^\\]+).*$', '$1')
        $info.driveType = 'remote'
        return $info
    }
    $letter = ($p -split ':')[0]
    if ($letter.Length -ne 1) { return $info }
    try {
        $psd = Get-PSDrive -Name $letter -ErrorAction Stop
        if ($psd.DisplayRoot -like '\\*') { $info.unc = $psd.DisplayRoot; $info.driveType = 'remote' }
    } catch { }
    try {
        $ld = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$letter`:'" -ErrorAction Stop
        if ($ld) {
            $info.volumeLabel = $ld.VolumeName
            $info.fileSystem  = $ld.FileSystem
            if ($ld.VolumeSerialNumber) { $info.serial = $ld.VolumeSerialNumber.ToLower() }
            if (-not $info.unc -and $ld.ProviderName) { $info.unc = $ld.ProviderName; $info.driveType = 'remote' }
            if ($ld.DriveType -eq 4) { $info.driveType = 'remote' }
        }
    } catch { }
    return $info
}

$rows = New-Object System.Collections.Generic.List[object]
$errors = New-Object System.Collections.Generic.List[string]
$rootInfos = New-Object System.Collections.Generic.List[object]
$seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$nFiles = 0; $nDirs = 0; [long]$nBytes = 0; $nCloud = 0; $nLinks = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()

foreach ($rootRaw in $Path) {
    $root = (Resolve-Path -LiteralPath $rootRaw -ErrorAction SilentlyContinue)
    if (-not $root) { $errors.Add("$rootRaw`tnot found"); continue }
    $root = $root.ProviderPath
    $rid = Get-RootIdentity $root
    $rootInfos.Add($rid)
    if ($rid.driveType -eq 'remote') {
        Write-Host ("  network share: {0}{1}" -f $root, $(if ($rid.unc -and $rid.unc -ne $root) { " -> $($rid.unc)" } else { "" }))
        Write-Host "  (network listings are latency-bound: expect this to take much longer than a local tree)"
    }

    # emit the root itself: Get-ChildItem only ever returns children, and the importer
    # needs the root row to know what was actually listed (the scan's scope)
    try {
        $ri = Get-Item -LiteralPath $root -Force -ErrorAction Stop
        $rows.Add([PSCustomObject]@{
            FullName         = $ri.FullName
            IsDir            = 1
            Length           = ''
            LastWriteTimeUtc = $ri.LastWriteTimeUtc.ToString('o')
            CreationTimeUtc  = $ri.CreationTimeUtc.ToString('o')
            Attributes       = [int]$ri.Attributes
        })
        $nDirs++
    } catch {
        $errors.Add("$root`t$($_.Exception.GetType().Name): $($_.Exception.Message)")
    }

    # stack of [path, depth]
    $stack = New-Object System.Collections.Stack
    $stack.Push(@($root, 0))

    while ($stack.Count -gt 0) {
        $frame = $stack.Pop()
        $dir = $frame[0]; $depth = $frame[1]
        if (-not $seen.Add($dir)) { continue }          # cycle guard
        if ($depth -gt $MaxDepth) { $errors.Add("$dir`tdepth cap $MaxDepth reached"); continue }

        # Retry transient failures: a share can drop a connection for a moment, and an
        # unretried folder becomes a silent gap in the listing.
        $kids = $null
        for ($try = 0; $try -le $Retries; $try++) {
            try {
                $kids = Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop
                break
            } catch [System.UnauthorizedAccessException] {
                $errors.Add("$dir`tUnauthorizedAccessException: $($_.Exception.Message)")
                break
            } catch {
                if ($try -eq $Retries) {
                    $errors.Add("$dir`t$($_.Exception.GetType().Name) after $Retries retries: $($_.Exception.Message)")
                } else {
                    Start-Sleep -Milliseconds (250 * [Math]::Pow(2, $try))
                }
            }
        }
        if ($null -eq $kids) { continue }

        foreach ($k in $kids) {
            $attr = [int]$k.Attributes
            $isDir = ($attr -band $FILE_ATTRIBUTE_DIRECTORY) -ne 0
            $isReparse = ($attr -band $FILE_ATTRIBUTE_REPARSE_POINT) -ne 0
            # A OneDrive placeholder folder carries ReparsePoint just like a junction.
            # Only real junctions/symlinks report a LinkType, so use that to tell them
            # apart: cloud folders are worth descending, link cycles are not.
            $linkType = $null
            if ($isReparse) { try { $linkType = $k.LinkType } catch { } }
            $isRealLink = $isReparse -and ($linkType -and $linkType -ne 'HardLink')
            $isCloud = $isReparse -and -not $isRealLink
            $rows.Add([PSCustomObject]@{
                FullName         = $k.FullName
                IsDir            = [int]$isDir
                Length           = if ($isDir) { '' } else { [long]$k.Length }
                LastWriteTimeUtc = $k.LastWriteTimeUtc.ToString('o')
                CreationTimeUtc  = $k.CreationTimeUtc.ToString('o')
                Attributes       = $attr
            })
            if ($isDir) {
                $nDirs++
                $descend = (-not $isReparse) -or $FollowLinks -or ($isCloud -and -not $SkipCloud)
                if ($isRealLink) { $nLinks++ }
                elseif ($isCloud) { $nCloud++ }
                if ($descend) { $stack.Push(@($k.FullName, $depth + 1)) }
            } else {
                $nFiles++; $nBytes += $k.Length
            }
            if ((($nFiles + $nDirs) % 2000) -eq 0) {
                Write-Host -NoNewline ("`r  {0:N0} files, {1:N0} dirs, {2:N1} GiB " -f $nFiles, $nDirs, ($nBytes / 1GB))
            }
        }
    }
}

$rows | Export-Csv -LiteralPath $Out -NoTypeInformation -Encoding UTF8
if ($errors.Count -gt 0) { $errors | Set-Content -LiteralPath $errPath -Encoding UTF8 }

# sidecar: what was listed and which volume/share it came from, so the importer can
# record identity instead of guessing from a drive letter
$metaPath = [System.IO.Path]::ChangeExtension($Out, '.meta.json')
[ordered]@{
    tool      = 'qcexport.ps1'
    machine   = $env:COMPUTERNAME
    user      = $env:USERNAME
    listedUtc = (Get-Date).ToUniversalTime().ToString('o')
    roots     = $rootInfos
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $metaPath -Encoding UTF8

Write-Host ("`r  {0:N0} files, {1:N0} dirs, {2:N1} GiB, {3} unreadable, {4:N1}s" -f `
    $nFiles, $nDirs, ($nBytes / 1GB), $errors.Count, $sw.Elapsed.TotalSeconds)
if ($nCloud -gt 0) {
    $verb = if ($SkipCloud) { 'skipped (-SkipCloud)' } else { 'descended into' }
    Write-Host ("  {0:N0} cloud placeholder folder(s) {1} - metadata only, no content downloaded" -f $nCloud, $verb)
}
if ($nLinks -gt 0) {
    $verb = if ($FollowLinks) { 'followed (-FollowLinks)' } else { 'recorded but not followed' }
    Write-Host ("  {0:N0} junction/symlink folder(s) {1}" -f $nLinks, $verb)
}
if ($nDirs -gt 0 -and $rows.Count -le ($nDirs + $nFiles) -and $errors.Count -gt 0) {
    Write-Host "  NOTE: some folders could not be listed - see the errors file below"
}
Write-Host "  CSV: $Out"
Write-Host "  meta: $metaPath"
if ($errors.Count -gt 0) { Write-Host "  errors: $errPath  <- check these before trusting totals" }
Write-Host "  next: python qcimport.py `"$Out`""
