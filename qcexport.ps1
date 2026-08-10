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
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [string]$Out,
    [int]$MaxDepth = 64,
    [switch]$FollowLinks
)

$ErrorActionPreference = 'Continue'
$FILE_ATTRIBUTE_DIRECTORY    = 0x10
$FILE_ATTRIBUTE_REPARSE_POINT = 0x400

if (-not $Out) {
    $tag = (Split-Path $Path[0] -Leaf) -replace '[^A-Za-z0-9._-]', '_'
    $Out = Join-Path (Get-Location) ("qcexport_{0}_{1}.csv" -f $tag, (Get-Date -Format 'yyyyMMddHHmm'))
}
$errPath = [System.IO.Path]::ChangeExtension($Out, '.errors.txt')

$rows = New-Object System.Collections.Generic.List[object]
$errors = New-Object System.Collections.Generic.List[string]
$seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$nFiles = 0; $nDirs = 0; [long]$nBytes = 0
$sw = [System.Diagnostics.Stopwatch]::StartNew()

foreach ($rootRaw in $Path) {
    $root = (Resolve-Path -LiteralPath $rootRaw -ErrorAction SilentlyContinue)
    if (-not $root) { $errors.Add("$rootRaw`tnot found"); continue }
    $root = $root.ProviderPath

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

        try {
            $kids = Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop
        } catch {
            $errors.Add("$dir`t$($_.Exception.GetType().Name): $($_.Exception.Message)")
            continue
        }

        foreach ($k in $kids) {
            $attr = [int]$k.Attributes
            $isDir = ($attr -band $FILE_ATTRIBUTE_DIRECTORY) -ne 0
            $isLink = ($attr -band $FILE_ATTRIBUTE_REPARSE_POINT) -ne 0
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
                if (-not $isLink -or $FollowLinks) { $stack.Push(@($k.FullName, $depth + 1)) }
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

Write-Host ("`r  {0:N0} files, {1:N0} dirs, {2:N1} GiB, {3} unreadable, {4:N1}s" -f `
    $nFiles, $nDirs, ($nBytes / 1GB), $errors.Count, $sw.Elapsed.TotalSeconds)
Write-Host "  CSV: $Out"
if ($errors.Count -gt 0) { Write-Host "  errors: $errPath" }
Write-Host "  next: python qcimport.py `"$Out`""
