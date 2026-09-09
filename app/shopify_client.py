"""
Cliente de solo lectura para el Admin API de Shopify.

Flujo de autenticación verificado en shopify.dev (agosto 2026): las apps
personalizadas creadas desde el Dev Dashboard usan "client credentials
grant" -> el token de acceso expira a las 24h y se renueva automáticamente
pidiendo uno nuevo con client_id + client_secret. No se usa un token
permanente.

Fuentes consultadas al construir este módulo:
- https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant
- https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/generate-app-access-tokens-admin
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

API_VERSION = "2025-01"
TOKEN_SAFETY_MARGIN_SECONDS = 120  # renovar un poco antes de que caduque


class ShopifyAuthError(RuntimeError):
    pass


class ShopifyAPIError(RuntimeError):
    def __init__(self, message: str, errors: Any = None):
        super().__init__(message)
        self.errors = errors


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # epoch seconds


class ShopifyClient:
    def __init__(self, shop: str, client_id: str, client_secret: str, timeout: float = 20.0):
        """
        shop: dominio de la tienda, con o sin 'https://', p.ej.
              'legionarius-hispania.myshopify.com'
        """
        self.shop = shop.strip().replace("https://", "").replace("http://", "").rstrip("/")
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.timeout = timeout
        self._token: _CachedToken | None = None

    # ---------------------------------------------------------------- auth
    def _fetch_new_token(self) -> _CachedToken:
        url = f"https://{self.shop}/admin/oauth/access_token"
        resp = requests.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise ShopifyAuthError(
                f"No se pudo obtener el token de Shopify (HTTP {resp.status_code}): {resp.text[:300]}"
            )
        data = resp.json()
        access_token = data.get("access_token")
        expires_in = data.get("expires_in", 86399)
        if not access_token:
            raise ShopifyAuthError(f"Respuesta de Shopify sin access_token: {data}")
        return _CachedToken(value=access_token, expires_at=time.time() + int(expires_in))

    def _get_token(self) -> str:
        if self._token is None or time.time() > (self._token.expires_at - TOKEN_SAFETY_MARGIN_SECONDS):
            self._token = self._fetch_new_token()
        return self._token.value

    # ------------------------------------------------------------- graphql
    def graphql(self, query: str, variables: dict | None = None) -> dict:
        token = self._get_token()
        url = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
            },
            json={"query": query, "variables": variables or {}},
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            # token posiblemente revocado/expirado antes de tiempo: forzar renovación una vez
            self._token = None
            token = self._get_token()
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
                json={"query": query, "variables": variables or {}},
                timeout=self.timeout,
            )
        if resp.status_code != 200:
            raise ShopifyAPIError(f"Shopify respondió HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if "errors" in payload:
            raise ShopifyAPIError("Shopify devolvió errores GraphQL", errors=payload["errors"])
        return payload["data"]

    # ------------------------------------------------------------ dominio
    def fetch_all_products_with_variants(self) -> list[dict]:
        """Devuelve todos los productos con sus variantes (id, título, sku,
        precio, inventario disponible, opciones seleccionadas)."""
        query = """
        query AllProducts($cursor: String) {
          products(first: 25, after: $cursor) {
            edges {
              node {
                id
                title
                variants(first: 50) {
                  edges {
                    node {
                      id
                      title
                      sku
                      price
                      inventoryQuantity
                      selectedOptions { name value }
                    }
                  }
                }
              }
            }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        products: list[dict] = []
        cursor = None
        while True:
            data = self.graphql(query, {"cursor": cursor})
            block = data["products"]
            for edge in block["edges"]:
                products.append(edge["node"])
            if block["pageInfo"]["hasNextPage"]:
                cursor = block["pageInfo"]["endCursor"]
            else:
                break
        return products

    def fetch_recent_orders(self, limit: int = 50) -> list[dict]:
        """Devuelve los pedidos más recientes (solo lectura).

        Deliberadamente NO se piden datos del cliente (nombre/email): son
        "datos protegidos de cliente" en Shopify y requieren una
        declaración aparte en el Dev Dashboard que no forma parte de lo
        acordado. El pedido se sincroniza igualmente, solo que sin nombre
        de cliente (se puede añadir a mano si hace falta).
        """
        query = """
        query RecentOrders($first: Int!) {
          orders(first: $first, sortKey: CREATED_AT, reverse: true) {
            edges {
              node {
                id
                name
                createdAt
                displayFulfillmentStatus
                tags
                currentTotalPriceSet { shopMoney { amount currencyCode } }
                lineItems(first: 25) {
                  edges { node { title quantity sku } }
                }
                fulfillments(first: 5) {
                  status
                  trackingInfo(first: 5) { company number url }
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"first": limit})
        return [e["node"] for e in data["orders"]["edges"]]
