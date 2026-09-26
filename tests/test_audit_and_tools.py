import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tracking_agent import audit
from tracking_agent.tools import Session, generate_files, make_tools

STOREFRONT_HTML = """
<script src="https://cdn.shopify.com/s/files/theme.js"></script>
<script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-OLD9999');</script>
<script id="web-pixels-manager-setup">webPixelsManagerAPI.init({"pixels":[
  {"id":"1","configuration":"{\\"pixel_id\\":\\"1234567890123456\\"}"},
  {"id":"2","configuration":"{\\"pixelCode\\":\\"C4ABCDEFGHIJKLMNOPQR\\"}"}]})</script>
<script>gtag('config', 'G-ZZZ999ZZZ9');</script>
"""


def test_detect_finds_installed_tags():
    found = audit.detect(STOREFRONT_HTML.replace('\\"', '"'))
    installed = found["installed"]
    assert installed["gtm"][0]["id"] == "GTM-OLD9999"
    assert installed["meta"][0]["id"] == "1234567890123456"
    assert installed["tiktok"][0]["id"] == "C4ABCDEFGHIJKLMNOPQR"
    assert installed["ga4"][0]["id"] == "G-ZZZ999ZZZ9"
    assert found["shopify"]["is_shopify"] and found["shopify"]["web_pixels_manager"]


def test_compare_warns_about_duplicates(config):
    detected = audit.detect(STOREFRONT_HTML.replace('\\"', '"'))
    warnings = " ".join(audit.compare_with_config(detected, config))
    assert "GTM-OLD9999" in warnings  # a second container
    assert "meta: 1234567890123456 is already firing" in warnings  # native app double-count
    assert "ga4: a different ID" in warnings


def _serve(status, body=b"<html>GTM-ABC1234</html>"):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["user_agent"] = self.headers.get("User-Agent")
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_audit_identifies_itself_honestly():
    server, seen = _serve(200)
    try:
        result = audit.audit_url(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
    assert seen["user_agent"].startswith("tracking-agent/")
    assert "Mozilla" not in seen["user_agent"]
    assert result["installed"]["gtm"][0]["id"] == "GTM-ABC1234"


def test_audit_explains_blocked_storefront():
    server, _ = _serve(403, b"blocked")
    try:
        with pytest.raises(RuntimeError, match="bot protection or password page"):
            audit.audit_url(f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()


def _session(tmp_path, config, approve=lambda _s: True):
    import yaml

    path = tmp_path / "tracking.yaml"
    path.write_text(yaml.safe_dump(config))
    return Session(config_path=path, out_dir=tmp_path / "build", approve=approve)


def test_generate_files_writes_outputs(tmp_path, config):
    result = generate_files(_session(tmp_path, config))
    assert result["ok"], result
    for path in result["files"].values():
        assert Path(path).exists()
    assert result["pixel_syntax"] in ("ok", "skipped (node not installed)")


def test_generate_refuses_invalid_config(tmp_path, config):
    config["platforms"]["meta"]["pixel_id"] = ""
    result = generate_files(_session(tmp_path, config))
    assert not result["ok"]
    assert not (tmp_path / "build").exists()


def test_tools_have_schemas_and_set_config_parses_json(tmp_path, config):
    session = _session(tmp_path, config)
    tools = {t.name: t for t in make_tools(session)}
    assert {"get_config", "set_config", "audit_site", "generate_tracking_files", "push_to_gtm"} <= set(tools)
    for tool in tools.values():
        assert tool.to_dict()["input_schema"]["type"] == "object"

    tools["set_config"].call({"path": "platforms.google_ads.conversions", "value": '{"purchase": "LBL"}'})
    tools["set_config"].call({"path": "platforms.meta.pixel_id", "value": "9999999999999999"})
    shown = json.loads(tools["get_config"].call({}))
    assert shown["config"]["platforms"]["google_ads"]["conversions"] == {"purchase": "LBL"}
    assert shown["config"]["platforms"]["meta"]["pixel_id"] == "9999999999999999"


def test_gtm_writes_require_generated_files_and_approval(tmp_path, config):
    declined = []
    session = _session(tmp_path, config, approve=lambda s: declined.append(s) or False)
    tools = {t.name: t for t in make_tools(session)}
    assert "generate_tracking_files first" in tools["push_to_gtm"].call({"workspace_name": "x"})
    assert "push_to_gtm first" in tools["create_gtm_version"].call({"version_name": "v", "notes": ""})
    assert "create_gtm_version first" in tools["publish_gtm_version"].call({})

    class FakeGTM:
        def get_or_create_workspace(self, name):
            return "accounts/1/containers/2/workspaces/3"

        def list_entities(self, ws):
            return {"tag": [], "trigger": [], "variable": []}

        def push_to_workspace(self, *a):  # pragma: no cover - must not be reached
            raise AssertionError("pushed without approval")

    session._gtm_client = FakeGTM()
    generate_files(session)
    assert "declined" in tools["push_to_gtm"].call({"workspace_name": "x"})
    assert "create 69" in declined[0]
