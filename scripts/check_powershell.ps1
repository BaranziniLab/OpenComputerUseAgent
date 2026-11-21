# Parse the Windows helper's runtime script without running it (pwsh on any OS).
#   pwsh -NoProfile -File scripts/check_powershell.ps1 build/source.noindex/apps/OpenComputerUseWindows/runtime.ps1
param([Parameter(Mandatory = $true)][string]$Path)
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $Path), [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error ("{0}:{1}: {2}" -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message) }
    exit 1
}
Write-Output ("parsed {0}: {1} tokens, no syntax errors" -f $Path, $tokens.Count)
