$token = "2d44a58f55c1441db17c3800f076cfbc"

# Step 1: 查知识库 id
Write-Host "=== Step 1: list knowledge bases ==="
$kbList = Invoke-RestMethod -Uri "http://localhost:9090/api/sentinel/knowledge-base?current=1&size=50" -Method Get -Headers @{Authorization = $token}
Write-Host "Total: $($kbList.data.total)"
foreach ($kb in $kbList.data.records) {
    Write-Host "  kb_id=$($kb.id) name=$($kb.name) collection=$($kb.collectionName)"
}

$kb = $kbList.data.records | Select-Object -First 1
$kbId = $kb.id
$collectionName = $kb.collectionName
Write-Host "Using: kb_id=$kbId collection=$collectionName"

# Step 2: 7 nodes (English-only to avoid PowerShell encoding issues)
$nodes = @(
    # level=0 根节�?    @{intentCode="kb-llm-security";             name="LLM Agent Security";         level=0; parentCode=$null;                 description="OWASP LLM Top-10 risks for LLM agents: prompt injection, sensitive disclosure, excessive agency, data poisoning, output handling"; examples=@("how to defend against prompt injection","how to prevent agent from exceeding its authority"); collectionNames=@()},
    # level=2 TOPIC 节点 (�?KB collection)
    @{intentCode="kb-llm-prompt-injection";     name="LLM01 Prompt Injection";    level=2; parentCode="kb-llm-security";     description="OWASP LLM01: identify and mitigate prompt injection attacks"; examples=@("how to defend against prompt injection","how to detect prompt injection"); collectionNames=@($collectionName)},
    @{intentCode="kb-llm-sensitive-disclosure"; name="LLM07 Sensitive Disclosure";level=2; parentCode="kb-llm-security";     description="OWASP LLM07: prevent sensitive data from leaking into context or output"; examples=@("how to handle sensitive data sent to external IP","how to redact PII"); collectionNames=@($collectionName)},
    @{intentCode="kb-llm-excessive-agency";     name="LLM06 Excessive Agency";    level=2; parentCode="kb-llm-security";     description="OWASP LLM06: bound agent tool authority and avoid over-autonomy"; examples=@("how to prevent agent exceeding authority","how to constrain tool calls"); collectionNames=@($collectionName)},
    # level=0 根节�?    @{intentCode="kb-credential-compromise";    name="Credential Compromise";     level=0; parentCode=$null;                 description="Credential leakage, credential stuffing, weak passwords, password reset abuse"; examples=@("how to handle credential leakage","how to detect credential stuffing"); collectionNames=@()},
    @{intentCode="kb-session-hijack";           name="Session Hijack";            level=0; parentCode=$null;                 description="Session management, session fixation, session hijacking detection"; examples=@("how to detect session hijack","session fixation attack"); collectionNames=@()},
    @{intentCode="kb-mitre-attack";             name="MITRE ATT&CK";              level=0; parentCode=$null;                 description="MITRE ATT&CK techniques, mitigations, detections"; examples=@("what technique is credential stuffing","how to mitigate credential filling"); collectionNames=@()}
)

# Step 3: create each node
Write-Host ""
Write-Host "=== Step 2: create 7 intent nodes ==="
$success = 0
$failed = 0
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
        if ($r.success) {
            Write-Host "  [OK] $($n.intentCode)"
            $success++
        } else {
            Write-Host "  [FAIL] $($n.intentCode): $($r.message)"
            $failed++
        }
    } catch {
        Write-Host "  [ERR] $($n.intentCode): $($_.Exception.Message)"
        $failed++
    }
}
Write-Host ""
Write-Host "Done: success=$success failed=$failed"
