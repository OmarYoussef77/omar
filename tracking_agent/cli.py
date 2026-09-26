"""Command line entry point.

  tracking-agent chat       # talk to the agent (default)
  tracking-agent generate   # build files from the config, no LLM involved
  tracking-agent audit URL  # scan a storefront for installed tracking
  tracking-agent capi-server [--host H] [--port P]  # server-side conversions
  tracking-agent test-checkout [--purchase]         # real-browser tracking test
  tracking-agent health [--synthetic] [--alert]     # health checks (for cron)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import audit, config as cfg


def _approve(summary: str) -> bool:
    print("\n" + "=" * 70)
    print("APPROVAL NEEDED\n")
    print(summary)
    print("=" * 70)
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _chat(args: argparse.Namespace) -> None:
    import anthropic

    from .agent import run_turn
    from .tools import Session, make_tools

    # Tool exceptions are returned to the model as errors; keep tracebacks off the console.
    logging.getLogger("anthropic").setLevel(logging.CRITICAL)

    session = Session(config_path=Path(args.config), out_dir=Path(args.out), approve=_approve)
    tools = make_tools(session)
    client = anthropic.Anthropic()
    messages: list = []

    print("Tracking agent - sets up GA4, Google Ads, Meta, TikTok, Snapchat and LinkedIn via GTM + Shopify.")
    print(f"Config: {args.config} | Output: {args.out}/ | Type 'exit' to quit.\n")
    first = "Hi, help me set up tracking for my Shopify store."
    while True:
        if first:
            user_input, first = first, None
            print(f"you> {user_input}")
        else:
            try:
                user_input = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if user_input.lower() in ("exit", "quit"):
            return
        if not user_input:
            continue
        messages.append({"role": "user", "content": user_input})
        try:
            run_turn(
                client,
                tools,
                messages,
                on_text=lambda text: print(f"\nagent> {text}"),
                on_tool=lambda name, _input: print(f"  [tool] {name}"),
            )
        except anthropic.APIConnectionError as exc:
            print(f"\n[connection error: {exc}] Try again.")
            messages.pop()
        except anthropic.RateLimitError:
            print("\n[rate limited] Wait a moment and try again.")
            messages.pop()
        except anthropic.APIStatusError as exc:
            print(f"\n[API error {exc.status_code}: {exc.message}]")
            messages.pop()


def _generate(args: argparse.Namespace) -> None:
    from .tools import Session, generate_files

    session = Session(config_path=Path(args.config), out_dir=Path(args.out), approve=lambda _s: False)
    result = generate_files(session)
    print(json.dumps(result, indent=2))
    if not result.get("ok"):
        sys.exit(1)


def _audit(args: argparse.Namespace) -> None:
    config = cfg.load_config(args.config) if Path(args.config).exists() else None
    print(json.dumps(audit.audit_url(args.url, config), indent=2))


def _capi_server(args: argparse.Namespace) -> None:
    from .capi.server import serve

    serve(args.config, host=args.host, port=args.port)


def _test_checkout(args: argparse.Namespace) -> None:
    from .capi.platforms import load_secrets
    from .checkout_test.runner import run_checkout_test
    from .health import verify_order_server_side

    config = cfg.load_config(args.config)
    if args.purchase and not args.yes and not _approve(
        f"Place a TEST ORDER on {config['checkout_test'].get('store_url') or config['store'].get('domain')} "
        "using Shopify's Bogus Gateway (payments must be in test mode).\n"
        "Browser pixels will send real events to your ad platforms."
    ):
        sys.exit("Cancelled.")
    report = run_checkout_test(config, purchase=args.purchase, headless=not args.headed)
    if args.purchase and report.get("order_id"):
        report["server_side"] = verify_order_server_side(config, load_secrets(), report["order_id"])
        if report["server_side"].get("status") == "fail":
            report["status"] = "fail"
    if not args.show_hits:
        report.pop("hits", None)
    print(json.dumps(report, indent=2))
    sys.exit({"pass": 0, "warn": 0}.get(report["status"], 1))


def _health(args: argparse.Namespace) -> None:
    from .capi.platforms import load_secrets
    from .health import AlertThrottle, alert_if_needed, run_health, summarize

    config = cfg.load_config(args.config)
    secrets = load_secrets()
    result = run_health(config, secrets, synthetic=args.synthetic)
    print(summarize(result, config["store"].get("name", "")))
    if args.json:
        print(json.dumps(result, indent=2))
    if args.alert:
        webhook = secrets.get("health_alert_webhook_url", "")
        if not webhook:
            sys.exit("--alert needs HEALTH_ALERT_WEBHOOK_URL")
        throttle = AlertThrottle(Path(args.state_file), float(config["health"].get("alert_every_hours", 6)))
        if alert_if_needed(result, webhook, throttle, config["store"].get("name", "")):
            print("Alert sent.")
    sys.exit({"ok": 0, "warn": 1, "fail": 2}[result["status"]])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tracking-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="tracking.yaml", help="config file (default: tracking.yaml)")
    parser.add_argument("--out", default="build", help="output folder (default: build)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="interactive agent (default)")
    sub.add_parser("generate", help="generate files from the config without the LLM")
    audit_parser = sub.add_parser("audit", help="scan a storefront for installed tracking")
    audit_parser.add_argument("url")
    server_parser = sub.add_parser("capi-server", help="run the server-side conversions server")
    server_parser.add_argument("--host", default="0.0.0.0")
    server_parser.add_argument("--port", type=int, default=8080)
    test_parser = sub.add_parser("test-checkout", help="drive a real browser through the store and verify tracking")
    test_parser.add_argument("--purchase", action="store_true", help="place a test order (Bogus Gateway)")
    test_parser.add_argument("--yes", action="store_true", help="don't ask before placing the test order")
    test_parser.add_argument("--headed", action="store_true", help="show the browser window")
    test_parser.add_argument("--show-hits", action="store_true", help="include every captured request")
    health_parser = sub.add_parser("health", help="run health checks (exit 0 ok / 1 warn / 2 fail)")
    health_parser.add_argument("--synthetic", action="store_true", help="also run a browse-mode browser test")
    health_parser.add_argument("--alert", action="store_true", help="post to HEALTH_ALERT_WEBHOOK_URL on problems")
    health_parser.add_argument("--state-file", default=".tracking-health.json", help="alert throttling state")
    health_parser.add_argument("--json", action="store_true", help="also print the full result as JSON")
    args = parser.parse_args(argv)

    commands = {
        "generate": _generate,
        "audit": _audit,
        "capi-server": _capi_server,
        "test-checkout": _test_checkout,
        "health": _health,
    }
    commands.get(args.command, _chat)(args)


if __name__ == "__main__":
    main()
