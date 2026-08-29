$token = "2d44a58f55c1441db17c3800f076cfbc"
$kbId = "2092516325183238144"
$collectionName = "riskdocs"

$nodes = @(
    @{intentCode="kb-llm-security";             name="LLM Agent Security";         level=0; parentCode=$null;                 description="OWASP LLM Top-10 risks for LLM agents: prompt injection, sensitive disclosure, excessive agency, data poisoning, output handling"; examples=@("how to defend against prompt injection","how to prevent agent from exceeding its authority"); collectionNames=@()},
    @{intentCode="kb-credential-compromise";    name="Credential Compromise";     level=0; parentCode=$null;                 description="Credential leakage, credential stuffing, weak passwords, password reset abuse"; examples=@("how to handle credential leakage","how to detect credential stuffing"); collectionNames=@()}
)

Write-Host "=== Creating 2 missing level-0 nodes ==="
foreach ($n in $nodes) {
    $body = @{
        kbId = $kbId
        intentCode = $n.intentCode
        name = $n.name
        level = $n.level
        parentCode = $n.parentCode
        description = $n.description
        examples = $n.examples
        collectionNames = $n.collectionNames
        kind = 0
        enabled = 1
    } | ConvertTo-Json -Depth 5

    try {
        $r = Invoke-RestMethod -Uri "http://localhost:9090/api/sentinel/intent-tree" -Method Post -ContentType "application/json" -Body $body -Headers @{Authorization = $token}
        Write-Host "  $($n.intentCode) success=$($r.success) data=$($r.data) message=$($r.message)"
    } catch {
        Write-Host "  $($n.intentCode) ERROR: $($_.Exception.Message)"
        if ($_.Exception.Response) {
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $reader.ReadToEnd() | Write-Host
        }
    }
}
