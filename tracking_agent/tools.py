"""Tools the agent can call. Anything that writes to an external system goes
through Session.approve(), so the user confirms every GTM change."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from anthropic import beta_tool

from . import audit, checklist, config as cfg, gtm_builder, shopify_pixel

Approver = Callable[[str], bool]


@dataclass
class Session:
    config_path: Path
    out_dir: Path
    approve: Approver
    audit_warnings: list[str] = field(default_factory=list)
    workspace_path: str = ""
    version_path: str = ""
    _gtm_client: Any = None

    def config(self) -> dict[str, Any]:
        return cfg.load_config(self.config_path)

    def gtm(self):
        if self._gtm_client is None:
            from .gtm_api import GTMClient

            gtm = self.config()["gtm"]
            if not (gtm.get("account_id") and gtm.get("container_id")):
                raise RuntimeError(
                    "gtm.account_id and gtm.container_id are required for API access. They are the "
                    "numbers in the GTM URL: tagmanager.google.com/#/container/accounts/<account_id>/containers/<container_id>/"
                )
            self._gtm_client = GTMClient(gtm["account_id"], gtm["container_id"], gtm.get("credentials_file", ""))
        return self._gtm_client


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def generate_files(session: Session) -> dict[str, Any]:
    config = session.config()
    validation = cfg.validate_config(config)
    if validation["errors"]:
        return {"ok": False, **validation}

    session.out_dir.mkdir(parents=True, exist_ok=True)
    container = gtm_builder.build_container(config)
    container_problems = gtm_builder.validate_container(container)
    pixel = shopify_pixel.build_pixel(config)
    guide = checklist.build_checklist(config, session.audit_warnings)

    paths = {
        "gtm_container": session.out_dir / "gtm-container.json",
        "shopify_pixel": session.out_dir / "shopify-custom-pixel.js",
        "setup_checklist": session.out_dir / "SETUP.md",
    }
    paths["gtm_container"].write_text(json.dumps(container, indent=2))
    paths["shopify_pixel"].write_text(pixel)
    paths["setup_checklist"].write_text(guide)

    return {
        "ok": not container_problems,
        "files": {k: str(p) for k, p in paths.items()},
        "container_summary": gtm_builder.summarize_container(container),
        "container_problems": container_problems,
        "pixel_syntax": check_js_syntax(paths["shopify_pixel"]),
        "warnings": validation["warnings"],
    }


def check_js_syntax(path: Path) -> str:
    node = shutil.which("node")
    if not node:
        return "skipped (node not installed)"
    # The pixel is a script body, not a module; wrap it the way Shopify does.
    wrapped = path.with_suffix(".check.js")
    wrapped.write_text("(function(analytics, init, customerPrivacy){\n" + path.read_text() + "\n})")
    try:
        result = subprocess.run([node, "--check", str(wrapped)], capture_output=True, text=True, timeout=30)
    finally:
        wrapped.unlink(missing_ok=True)
    return "ok" if result.returncode == 0 else f"syntax error: {result.stderr.strip()}"


def make_tools(session: Session) -> list:
    @beta_tool
    def get_config() -> str:
        """Show the current tracking config and what is still missing or invalid.

        Call this first, and again after changes, to see which IDs the user still needs to provide.
        """
        config = session.config()
        return _json(
            {
                "config_file": str(session.config_path),
                "config": config,
                "enabled_platforms": cfg.enabled_platforms(config),
                "validation": cfg.validate_config(config),
            }
        )

    @beta_tool
    def set_config(path: str, value: str) -> str:
        """Set one config value and save it to the config file.

        Only store values the user actually gave you - never invent pixel IDs or labels.

        Args:
            path: Dotted path, e.g. "platforms.meta.pixel_id", "platforms.meta.enabled",
                "gtm.container_public_id", "store.item_id_source",
                "platforms.google_ads.conversions" or "platforms.linkedin.conversions".
            value: JSON value. Strings may be given bare. Examples: "1234567890123456", true,
                {"purchase": "AbC-D_efG", "add_to_cart": "XyZ"} for Google Ads conversion labels,
                {"purchase": 12345678} for LinkedIn conversion IDs.
        """
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
            parsed = value.strip()  # IDs are strings; 16-digit pixel IDs must not become numbers
        config = session.config()
        cfg.set_value(config, path, parsed)
        cfg.save_config(config, session.config_path)
        return _json({"saved": {path: parsed}, "validation": cfg.validate_config(config)})

    @beta_tool
    def audit_site(url: str) -> str:
        """Scan a live storefront for tracking that is already installed (GTM, GA4, Google Ads,
        Meta, TikTok, Snapchat, LinkedIn) and flag conflicts with the planned setup.

        Args:
            url: Storefront URL, e.g. https://mystore.com
        """
        result = audit.audit_url(url, session.config())
        session.audit_warnings = result.get("warnings", [])
        return _json(result)

    @beta_tool
    def plan_events() -> str:
        """Show the event mapping that will be deployed: each Shopify event, the dataLayer event it
        becomes, and the event each enabled platform receives."""
        config = session.config()
        platforms = cfg.enabled_platforms(config)
        shopify_source = {
            "page_view": "page_viewed",
            "view_item_list": "collection_viewed",
            "view_item": "product_viewed",
            "search": "search_submitted",
            "add_to_cart": "product_added_to_cart",
            "view_cart": "cart_viewed",
            "begin_checkout": "checkout_started",
            "add_shipping_info": "checkout_shipping_info_submitted",
            "add_payment_info": "payment_info_submitted",
            "purchase": "checkout_completed",
        }
        per_platform = {
            "ga4": lambda e: "page_view (via Google tag)" if e == "page_view" else e,
            "google_ads": lambda e: (
                f"conversion label {config['platforms']['google_ads']['conversions'][e]}"
                if e in (config["platforms"]["google_ads"].get("conversions") or {})
                else ("Google tag + conversion linker" if e == "page_view" else None)
            ),
            "meta": gtm_builder.META_EVENTS.get,
            "tiktok": lambda e: "page()" if e == "page_view" else gtm_builder.TIKTOK_EVENTS.get(e),
            "snapchat": gtm_builder.SNAP_EVENTS.get,
            "linkedin": lambda e: (
                "Insight Tag page view"
                if e == "page_view"
                else (
                    f"conversion {config['platforms']['linkedin']['conversions'][e]}"
                    if e in (config["platforms"]["linkedin"].get("conversions") or {})
                    else None
                )
            ),
        }
        rows = []
        for event in cfg.CANONICAL_EVENTS:
            row = {"shopify_event": shopify_source[event], "datalayer_event": event}
            for p in platforms:
                row[p] = per_platform[p](event) or "-"
            rows.append(row)
        return _json({"platforms": platforms, "events": rows})

    @beta_tool
    def generate_tracking_files() -> str:
        """Generate the GTM container JSON, the Shopify custom pixel and the SETUP.md checklist into
        the output folder, then validate them. Local only - nothing is published.
        """
        return _json(generate_files(session))

    @beta_tool
    def push_to_gtm(workspace_name: str) -> str:
        """Upload the generated container into a GTM workspace (a draft - not live). Asks the user
        to approve first. Run generate_tracking_files before this.

        Args:
            workspace_name: Workspace to create or reuse, e.g. "tracking-agent setup".
        """
        container_file = session.out_dir / "gtm-container.json"
        if not container_file.exists():
            return "No generated container found. Call generate_tracking_files first."
        export = json.loads(container_file.read_text())
        client = session.gtm()
        from .gtm_api import plan_push

        workspace = client.get_or_create_workspace(workspace_name)
        plan = plan_push(export, client.list_entities(workspace))
        summary = (
            f"Write to GTM workspace '{workspace_name}' ({workspace}):\n"
            f"  create {len(plan['create'])}: " + ", ".join(plan["create"]) + "\n"
            f"  update {len(plan['update'])}: " + ", ".join(plan["update"])
        )
        if not session.approve(summary):
            return "User declined the GTM push. Nothing was written."
        result = client.push_to_workspace(export, workspace)
        session.workspace_path = workspace
        return _json(result)

    @beta_tool
    def create_gtm_version(version_name: str, notes: str) -> str:
        """Snapshot the pushed workspace as a GTM container version (still not live). Asks the user
        to approve first.

        Args:
            version_name: e.g. "Shopify pixel tracking v1".
            notes: What changed, for the GTM version history.
        """
        if not session.workspace_path:
            return "Nothing pushed in this session. Call push_to_gtm first."
        if not session.approve(f"Create GTM version '{version_name}' from {session.workspace_path}"):
            return "User declined. No version created."
        version = session.gtm().create_version(session.workspace_path, version_name, notes)
        session.version_path = version["path"]
        return _json(version)

    @beta_tool
    def publish_gtm_version() -> str:
        """Publish the version created in this session so it goes LIVE on the storefront. Asks the
        user to approve first. Only use after the user has reviewed the workspace in GTM."""
        if not session.version_path:
            return "No version created in this session. Call create_gtm_version first."
        if not session.approve(
            f"PUBLISH {session.version_path} - this makes the tags live for all visitors."
        ):
            return "User declined. Nothing was published."
        return _json(session.gtm().publish_version(session.version_path))

    return [
        get_config,
        set_config,
        audit_site,
        plan_events,
        generate_tracking_files,
        push_to_gtm,
        create_gtm_version,
        publish_gtm_version,
    ]
