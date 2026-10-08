# ASCII only (PS 5.1 safe). Live e2e progress bar.
$Log = "c:\Users\30970\Desktop\Folder\sentinel-main\agent-python\_e2e_err.log"
$PID_E2E = 7276
$W = 40
$start = Get-Date
$prev = 0
$prevT = Get-Date

function Bar($n, $total, $w) {
    if ($total -le 0) { $total = 1 }
    if ($n -gt $total) { $n = $total }
    $f = [int]([math]::Round($n * $w / $total))
    $e = $w - $f
    return ("#" * $f) + ("-" * $e)
}

while ($true) {
    Clear-Host
    $p = Get-Process -Id $PID_E2E -ErrorAction SilentlyContinue
    $done = -not $p

    $n = 0
    $last = ""
    if (Test-Path $Log) {
        $lines = Get-Content $Log -Encoding utf8 -ErrorAction SilentlyContinue
        if ($lines) {
            foreach ($l in $lines) {
                if ($l -match "(\d+)/(\d+)") {
                    $n = [int]$Matches[1]
                }
            }
            $last = $lines[$lines.Count - 1]
        }
    }

    $total = 102
    $pct = [math]::Round($n * 100 / $total, 1)
    Write-Output "=== e2e_eval progress (seed total=$total) ==="
    Write-Output ("[" + (Bar $n $total $W) + "] " + $n + "/" + $total + "  " + $pct + "%")
    Write-Output ""

    $el = (Get-Date) - $start
    if ($el.TotalSeconds -gt 6 -and $n -gt $prev) {
        $dn = $n - $prev
        $dt = ((Get-Date) - $prevT).TotalSeconds
        if ($dt -gt 0) {
            $rate = $dn / $dt
            $rem = $total - $n
            if ($rate -gt 0) {
                $eta = [int]($rem / $rate)
                Write-Output ("rate: " + [math]::Round($rate, 2) + " items/s   ETA: " + $eta + "s")
            }
        }
    }
    if ($n -ne $prev) { $prev = $n; $prevT = Get-Date }

    Write-Output ("elapsed: " + [int]$el.TotalSeconds + "s")
    Write-Output ""
    if ($last.Length -gt 110) { $last = $last.Substring(0, 110) }
    Write-Output ("last: " + $last)

    if ($done) {
        Write-Output ""
        Write-Output "=== e2e FINISHED ==="
        $rep = Get-ChildItem "c:\Users\30970\Desktop\Folder\sentinel-main\evaluation\reports" -Filter "mini-regress-e2e*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 4
        foreach ($r in $rep) { Write-Output $r.Name }
        Start-Sleep -Seconds 30
        break
    }
    Start-Sleep -Seconds 3
}
