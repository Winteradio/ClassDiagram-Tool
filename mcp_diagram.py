"""Claude Code(-p)에 다이어그램 편집 도구를 제공하는 최소 MCP stdio 서버.

    python mcp_diagram.py --port 8765 --repo <repo 경로>

도구 실행은 로컬 툴 서버의 /api/tool 로 넘겨서, 다이어그램 저장이 한 곳(서버)에서만 일어나게 함.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

# 코드 읽기는 Claude Code 자체의 Read/Grep/Glob을 쓰므로 다이어그램 도구만 노출함
DIAGRAM_TOOLS = {"list_classes", "get_class", "upsert_class", "remove_class", "add_members", "edit_member",
                 "remove_members", "restore", "add_relation", "remove_relation"}


def post(port, route, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{route}", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"text": f"Error: {json.loads(e.read()).get('error', e.reason)}", "isError": True}
    except OSError as e:
        return {"text": f"Error: diagram server unreachable ({e})", "isError": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    tools = post(args.port, "tool_list", {"repo": args.repo}).get("tools", [])
    tools = [t for t in tools if t["name"] in DIAGRAM_TOOLS]

    def send(msg):
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue  # notifications/initialized 등 알림은 응답하지 않음
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "diagram", "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in tools]}})
        elif method == "tools/call":
            r = post(args.port, "tool", {"repo": args.repo, "name": params.get("name"), "args": params.get("arguments") or {}})
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": r.get("text", "")}], "isError": bool(r.get("isError"))}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
