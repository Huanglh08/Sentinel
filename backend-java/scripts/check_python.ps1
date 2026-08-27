Write-Host "=== Test Python service ==="
try {
    $r = Invoke-WebRequest -Uri "http://localhost:8000/health" -Method Get -TimeoutSec 5 -UseBasicParsing
    Write-Host "health: $($r.StatusCode) $($r.Content)"
} catch {
    Write-Host "health error: $($_.Exception.Message)"
}

try {
    $r = Invoke-WebRequest -Uri "http://localhost:8000/docs" -Method Get -TimeoutSec 5 -UseBasicParsing
    Write-Host "docs: $($r.StatusCode)"
} catch {
    Write-Host "docs error: $($_.Exception.Message)"
}
