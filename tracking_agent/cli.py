"""Command line entry point.

  tracking-agent chat       # talk to the agent (default)
  tracking-agent generate   # build files from the config, no LLM involved
  tracking-agent audit URL  # scan a storefront for installed tracking
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tracking-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="tracking.yaml", help="config file (default: tracking.yaml)")
    parser.add_argument("--out", default="build", help="output folder (default: build)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="interactive agent (default)")
    sub.add_parser("generate", help="generate files from the config without the LLM")
    audit_parser = sub.add_parser("audit", help="scan a storefront for installed tracking")
    audit_parser.add_argument("url")
    args = parser.parse_args(argv)

    {"generate": _generate, "audit": _audit}.get(args.command, _chat)(args)


if __name__ == "__main__":
    main()
