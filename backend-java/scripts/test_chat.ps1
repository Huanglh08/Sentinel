$body = @{
    question = "How to defend against prompt injection?"
    kbId = $null
    intentCode = $null
} | ConvertTo-Json -Depth 5

try {
    $r = Invoke-WebRequest -Uri "http://localhost:8000/chat" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 10 -UseBasicParsing
    Write-Host "status: $($r.StatusCode)"
    Write-Host "body: $($r.Content)"
} catch {
    Write-Host "error: $($_.Exception.Message)"
    if ($_.Exception.Response) {
        $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
        Write-Host "response body: $($reader.ReadToEnd())"
    }
}
