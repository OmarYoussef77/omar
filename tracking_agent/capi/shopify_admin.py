"""Register the orders/paid webhook through the Shopify Admin GraphQL API.

Uses a custom app's Admin API access token (SHOPIFY_ADMIN_TOKEN). Webhooks
created by that app are signed with the app's API secret key, which is
what the CAPI server needs as SHOPIFY_WEBHOOK_SECRET.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

LIST_QUERY = """
query ExistingOrdersPaidWebhooks {
  webhookSubscriptions(first: 50, topics: [ORDERS_PAID]) {
    nodes { id uri }
  }
}
"""

CREATE_MUTATION = """
mutation CreateOrdersPaidWebhook($uri: String!) {
  webhookSubscriptionCreate(
    topic: ORDERS_PAID
    webhookSubscription: { uri: $uri, format: JSON }
  ) {
    webhookSubscription { id topic uri }
    userErrors { field message }
  }
}
"""


def webhook_uri(server_url: str) -> str:
    return server_url.rstrip("/") + "/webhooks/shopify/orders-paid"


class ShopifyAdmin:
    def __init__(self, shop_domain: str, token: str, api_version: str, endpoint: str | None = None):
        if not shop_domain.endswith(".myshopify.com"):
            raise ValueError("shopify.myshopify_domain must be <shop>.myshopify.com")
        if not token:
            raise ValueError("SHOPIFY_ADMIN_TOKEN is not set")
        self.url = endpoint or f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
        self.token = token

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": self.token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Shopify Admin API HTTP {exc.code}: {exc.read()[:300]!r}") from exc
        if payload.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {payload['errors']}")
        return payload["data"]

    def existing_webhook(self, uri: str) -> str | None:
        nodes = self._graphql(LIST_QUERY)["webhookSubscriptions"]["nodes"]
        return next((n["id"] for n in nodes if n["uri"] == uri), None)

    def create_webhook(self, uri: str) -> dict[str, Any]:
        result = self._graphql(CREATE_MUTATION, {"uri": uri})["webhookSubscriptionCreate"]
        if result["userErrors"]:
            raise RuntimeError(f"Shopify rejected the webhook: {result['userErrors']}")
        return result["webhookSubscription"]
