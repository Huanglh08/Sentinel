Get-NetTCPConnection -State Listen | Where-Object { @(5173, 9090, 8001, 8002) -contains $_.LocalPort } | Format-Table LocalAddress, LocalPort, OwningProcess -AutoSize
