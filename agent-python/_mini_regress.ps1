# Mini regression after KB doc update (ASCII only, PS 5.1 safe)
# ponytail: script-triggered (one command after upload), not server-side auto-run
#   after doc upload/refresh. Upgrade path: Java upload callback -> agent endpoint.
# Usage: .\_mini_regress.ps1 [-Docs "LLM03,prompt"]
#   Docs = comma-separated substrings matched against expected_doc_ids; empty = full set.
param([string]$Docs = "")
$ErrorActionPreference = "Stop"
Set-Location "c:\Users\30970\Desktop\Folder\sentinel-main\agent-python"
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\30970\AppData\Local\Programs\Python\Python312\python.exe"
$docsArgs = @()
if ($Docs -ne "") { $docsArgs = @("--docs", $Docs) }

Write-Output "=== [1/2] retrieval subset (deterministic, Recall@5/HitRate/MRR) ==="
& $py -X utf8 -u -m eval.retrieval_eval @docsArgs --name "mini-regress-retrieval"
if ($LASTEXITCODE -ne 0) { throw "retrieval_eval failed" }

Write-Output "=== [2/2] e2e subset with judge (faith/rel, self-compare baseline) ==="
& $py -X utf8 -u -m eval.e2e_eval --seed "..\evaluation\seeds\e2e-seeds.jsonl" --runs 1 @docsArgs --name "mini-regress-e2e"
if ($LASTEXITCODE -ne 0) { throw "e2e_eval failed" }

Write-Output "Done. Reports: ..\evaluation\reports\mini-regress-*.md"
