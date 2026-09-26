"""Drive the agent loop against a local fake Messages API (no API key needed)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import yaml

from tracking_agent.agent import MODEL, run_turn
from tracking_agent.tools import Session, make_tools


def _message(content, stop_reason):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


RESPONSES = [
    _message(
        [
            {"type": "text", "text": "Let me look at your config."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_config", "input": {}},
        ],
        "tool_use",
    ),
    _message([{"type": "text", "text": "Your config is complete."}], "end_turn"),
]


def test_run_turn_executes_tools_and_mirrors_history(tmp_path, config):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"body": body, "beta": self.headers.get("anthropic-beta", "")})
            payload = json.dumps(RESPONSES[len(requests) - 1]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config_path = tmp_path / "tracking.yaml"
        config_path.write_text(yaml.safe_dump(config))
        session = Session(config_path=config_path, out_dir=tmp_path / "build", approve=lambda _s: False)
        client = anthropic.Anthropic(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
        messages = [{"role": "user", "content": "set up tracking"}]
        texts, tool_calls = [], []
        run_turn(client, make_tools(session), messages, texts.append, lambda n, i: tool_calls.append(n))
    finally:
        server.shutdown()

    assert tool_calls == ["get_config"]
    assert texts == ["Let me look at your config.", "Your config is complete."]
    first = requests[0]["body"]
    assert first["model"] == MODEL
    assert first["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in requests[0]["beta"]
    assert first["thinking"] == {"type": "adaptive"}
    assert {t["name"] for t in first["tools"]} >= {"get_config", "push_to_gtm"}

    # Second request carries the tool result; local history mirrors the whole exchange.
    tool_result = requests[1]["body"]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert "GTM-ABC1234" in json.dumps(tool_result["content"])
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
