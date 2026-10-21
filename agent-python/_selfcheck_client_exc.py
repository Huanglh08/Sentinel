"""Self-check: ClientException global handler registered in server.py, dead handler removed from controller.py.

Run: python _selfcheck_client_exc.py
"""
import ast
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent


def main() -> int:
    # 1) server.py parses and registers the handler + import
    server_src = (ROOT / "server.py").read_text(encoding="utf-8")
    ast.parse(server_src)
    assert "from core.agent_profile.service import ClientException" in server_src, "import missing"
    assert "@app.exception_handler(ClientException)" in server_src, "handler not registered"

    # 2) controller.py parses and the dead handler is gone
    ctl_src = (ROOT / "core" / "agent_profile" / "controller.py").read_text(encoding="utf-8")
    ast.parse(ctl_src)
    assert "_client_exception_handler" not in ctl_src, "dead handler still present"
    assert "HTTPException" not in ctl_src, "HTTPException import not cleaned"

    # 3) behavior: handler returns 400 + passthrough message
    from core.agent_profile.service import ClientException
    from server import client_exception_handler

    req = SimpleNamespace(state=SimpleNamespace(request_id="selfcheck"))
    resp = asyncio.run(client_exception_handler(req, ClientException("智能体名称不能为空")))
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}"
    assert body["code"] == "CLIENT_ERROR", body
    assert body["message"] == "智能体名称不能为空", body

    print("PASS: 6/6 asserts (syntax x2, import, register, dead-code removed, 400+message)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
