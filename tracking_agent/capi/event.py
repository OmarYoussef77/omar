"""Turn a Shopify orders/paid webhook (+ the browser beacon) into one
platform-neutral purchase event.

Deduplication: the browser pixel sends the purchase with event ID
"purchase-<order id>" (see shopify_pixel.py), and every server-side event
reuses exactly that ID, so each platform keeps one copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse


def order_number(order_id: Any) -> str:
    """'gid://shopify/Order/123' / 'gid://shopify/OrderIdentity/123' / 123 -> '123'."""
    match = re.search(r"(\d+)\s*$", str(order_id or ""))
    return match.group(1) if match else ""


def purchase_event_id(order_id: Any) -> str:
    return f"purchase-{order_number(order_id)}"


def item_id(product_id: Any, variant_id: Any, sku: str | None, source: str, feed_country: str) -> str:
    if source == "product_id":
        return str(product_id)
    if source == "sku":
        return str(sku or variant_id)
    if source == "shopify_feed":
        return f"shopify_{feed_country}_{product_id}_{variant_id}"
    return str(variant_id)


@dataclass
class Item:
    id: str
    name: str
    quantity: int
    price: float


@dataclass
class PurchaseEvent:
    order_id: str
    event_id: str
    event_time: int
    value: float
    currency: str
    items: list[Item]
    source_url: str
    email: str | None = None
    phone: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None
    external_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    # Click IDs from the landing URL and first-party cookies from the beacon.
    click_ids: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    # True/False from the browser's consent state; None = no beacon arrived.
    marketing_allowed: bool | None = None

    @property
    def content_ids(self) -> list[str]:
        return [item.id for item in self.items]

    @property
    def num_items(self) -> int:
        return sum(item.quantity for item in self.items)


def _money(price_set: dict | None, fallback_amount: Any, fallback_currency: str | None) -> tuple[float, str | None]:
    presentment = (price_set or {}).get("presentment_money") or {}
    if presentment.get("amount") is not None:
        return float(presentment["amount"]), presentment.get("currency_code")
    return float(fallback_amount or 0), fallback_currency


def _timestamp(value: str | None) -> int:
    if not value:
        return int(datetime.now(timezone.utc).timestamp())
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


_CLICK_ID_PARAMS = ("fbclid", "ttclid", "ScCid", "gclid", "gbraid", "wbraid", "li_fat_id")


def _click_ids(landing_site: str | None) -> dict[str, str]:
    query = parse_qs(urlparse(landing_site or "").query)
    return {key: query[key][0] for key in _CLICK_ID_PARAMS if query.get(key)}


def from_shopify_order(
    order: dict[str, Any],
    beacon: dict[str, Any] | None,
    *,
    item_id_source: str = "variant_id",
    feed_country: str = "US",
    shop_domain: str = "",
) -> PurchaseEvent:
    beacon = beacon or {}
    value, currency = _money(order.get("total_price_set"), order.get("total_price"), order.get("currency"))
    items = []
    for line in order.get("line_items") or []:
        price, _ = _money(line.get("price_set"), line.get("price"), None)
        items.append(
            Item(
                id=item_id(line.get("product_id"), line.get("variant_id"), line.get("sku"), item_id_source, feed_country),
                name=line.get("title") or line.get("name") or "",
                quantity=int(line.get("quantity") or 1),
                price=price,
            )
        )

    customer = order.get("customer") or {}
    address = order.get("billing_address") or order.get("shipping_address") or {}
    client = order.get("client_details") or {}
    number = order_number(order.get("id") or order.get("admin_graphql_api_id"))
    source_url = beacon.get("page_url") or order.get("order_status_url") or (f"https://{shop_domain}/" if shop_domain else "")

    return PurchaseEvent(
        order_id=number,
        event_id=purchase_event_id(number),
        event_time=_timestamp(order.get("processed_at") or order.get("created_at")),
        value=value,
        currency=currency or order.get("presentment_currency") or order.get("currency") or "USD",
        items=items,
        source_url=source_url,
        email=order.get("email") or order.get("contact_email") or customer.get("email"),
        phone=order.get("phone") or customer.get("phone") or address.get("phone"),
        first_name=address.get("first_name") or customer.get("first_name"),
        last_name=address.get("last_name") or customer.get("last_name"),
        city=address.get("city"),
        state=address.get("province_code"),
        zip=address.get("zip"),
        country=address.get("country_code"),
        external_id=str(customer["id"]) if customer.get("id") else None,
        ip=order.get("browser_ip") or beacon.get("ip"),
        user_agent=client.get("user_agent") or beacon.get("user_agent"),
        click_ids=_click_ids(order.get("landing_site")),
        cookies={k: str(v) for k, v in (beacon.get("cookies") or {}).items() if v},
        marketing_allowed=beacon.get("marketing_allowed") if beacon else None,
    )
