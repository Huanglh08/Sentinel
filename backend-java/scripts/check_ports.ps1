Get-NetTCPConnection -State Listen | Where-Object { @(8000, 9090, 9099, 5173) -contains $_.LocalPort } | Format-Table LocalAddress, LocalPort, OwningProcess -AutoSize
