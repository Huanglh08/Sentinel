# Realtime progress monitor for e2e-baseline3 (ASCII only, PS 5.1 safe)
# Redraws a progress bar every 3s in this console. Auto-exits when eval finishes.
$ErrorActionPreference = "SilentlyContinue"
Set-Location "c:\Users\30970\Desktop\Folder\sentinel-main\agent-python"
$pidTarget = 36924
$t0 = Get-Date
$done0 = $null

function Bar($frac, $width) {
    $filled = [int][Math]::Floor($frac * $width)
    if ($filled -lt 0) { $filled = 0 }
    if ($filled -gt $width) { $filled = $width }
    return ("#" * $filled) + ("-" * ($width - $filled))
}

while ($true) {
    $alive = [bool](Get-Process -Id $pidTarget)
    $lines = Get-Content e2e_baseline3_err.log
    $doneLines = @($lines | Select-String "pass=")
    $done = $doneLines.Count
    $runLine = ($lines | Select-String "run \d/3" | Select-Object -Last 1)
    $cur = "?"; if ($runLine) { $cur = $runLine.Matches[0].Value }
    $inRun = 0
    $m = [regex]::Match($cur, "run (\d)/3")
    if ($m.Success) { $inRun = [int]$m.Groups[1].Value }
    # items done within current run = total done - 102*(runIndex-1)
    $inRunDone = $done - 102 * ($inRun - 1)
    if ($null -eq $done0) { $done0 = $done }

    $el = ((Get-Date) - $t0).TotalSeconds
    $rate = if ($el -gt 6) { ($done - $done0) / $el } else { 0 }
    $remain = 306 - $done
    $eta = if ($rate -gt 0.01) { [int]($remain / $rate) } else { -1 }
    $etaStr = if ($eta -ge 0) { "~$([int]($eta/60))m$($eta%60)s" } else { "..." }

    $f1 = $inRunDone / 102.0; $ft = $done / 306.0
    # pass rate so far in current run
    $passN = @($doneLines | Select-String "pass=ok|pass=Y").Count

    Clear-Host
    Write-Host "=== e2e-baseline3 realtime progress ===" 
    Write-Host ("run  $cur  [$inRunDone/102]")
    Write-Host ("     [$(Bar $f1 40)] $([int]($f1*100))%")
    Write-Host ("total          [$done/306]")
    Write-Host ("     [$(Bar $ft 40)] $([int]($ft*100))%")
    Write-Host ("rate: {0:N2} items/s   ETA: {1}" -f $rate, $etaStr)
    $last = $doneLines | Select-Object -Last 1
    if ($last) { $s = $last.Line; if ($s.Length -gt 110) { $s = $s.Substring(0,110) }; Write-Host "last: $s" }
    if (-not $alive) {
        Write-Host ""
        Write-Host ">>> eval process exited. Final reports:"
        Get-ChildItem ..\evaluation\reports -Filter "e2e-baseline3*" | ForEach-Object { Write-Host ("    " + $_.Name) }
        Write-Host ">>> monitor will close in 30s."
        Start-Sleep 30
        break
    }
    Start-Sleep 3
}
