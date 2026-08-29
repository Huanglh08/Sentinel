$token = "2d44a58f55c1441db17c3800f076cfbc"

Write-Host "=== 验证意图树是否被后端识别 ==="
$q = [uri]::EscapeDataString("How to defend against prompt injection?")
try {
    $r = Invoke-RestMethod -Uri "http://localhost:9090/api/sentinel/rag/eval?question=$q" -Method Get -Headers @{Authorization = $token} -ErrorAction Stop
    Write-Host "intentLeafIds: $($r.data.intentLeafIds -join ', ')"
    Write-Host "retrievedDocIds: $($r.data.retrievedDocIds -join ', ')"
    Write-Host "hasKb: $($r.data.hasKb)"
    Write-Host "subIntents: $($r.data.subIntents -join ', ')"
    Write-Host "latencyMs: $($r.data.latencyMs)"
} catch {
    Write-Host "ERROR: $($_.Exception.Message)"
}
