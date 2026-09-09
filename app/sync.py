"""
Lógica de sincronización Shopify -> estado compartido (Supabase).

Diseño deliberadamente conservador y transparente:

- Es de SOLO LECTURA hacia Shopify: nunca escribimos nada en la tienda.
- El stock interno (`stock`, campo que el resto de la app decrementa al
  marcar pedidos como "Preparado") NO se sobrescribe automáticamente con el
  inventario de Shopify, para no chocar con ajustes manuales ya hechos en
  el panel. En su lugar, el inventario de Shopify se guarda aparte, en
  `shopifyStock`, como referencia visible en la pantalla de Inventario.
  Un producto sin homólogo local se crea nuevo, y ahí sí toma como stock
  inicial el de Shopify.
- Los pedidos importados desde Shopify se añaden como nuevos SIEMPRE con
  `fulfilled:false` y `stockApplied:false`, igual que hace la importación
  manual por CSV cuando no se marca "Descontar stock". El estado real de
  Shopify se guarda en el campo `status` para que se vea, pero no dispara
  ningún descuento automático de stock. Así, la única vía para descontar
  stock sigue siendo la misma de siempre: marcar "Preparado" a mano en el
  panel (ya sea de un pedido importado o manual).
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from shopify_client import ShopifyClient
from supabase_client import SupabaseClient


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _extract_tiktok_order_id(tags: list) -> str:
    """Los pedidos de TikTok Shop llegan a Shopify (vía el canal de ventas de
    TikTok) con una tag "TikTokOrderID:<id>". La extraemos si está, para
    poder cruzar el pedido con el panel de TikTok Shop Seller Center."""
    for tag in tags or []:
        if isinstance(tag, str) and tag.startswith("TikTokOrderID:"):
            return tag.split(":", 1)[1].strip()
    return ""


def _extract_fulfillment_info(fulfillments: list) -> dict:
    """De la primera entrega con datos de seguimiento, extrae número,
    transportista, URL y el almacén/ubicación de envío. Estos campos SÍ
    los expone Shopify (a diferencia de los plazos internos de TikTok
    Shop, que no llegan a Shopify)."""
    for f in fulfillments or []:
        tracking_list = f.get("trackingInfo") or []
        tracking = tracking_list[0] if tracking_list else {}
        location = f.get("location") or {}
        if tracking.get("number") or location.get("name"):
            return {
                "trackingNumber": tracking.get("number") or "",
                "trackingCompany": tracking.get("company") or "",
                "trackingUrl": tracking.get("url") or "",
                "fulfillmentLocation": location.get("name") or "",
            }
    return {"trackingNumber": "", "trackingCompany": "", "trackingUrl": "", "fulfillmentLocation": ""}


def _variant_to_product_fields(variant: dict, product_title: str, product_id: str) -> dict:
    opts = variant.get("selectedOptions") or []
    variant_title = variant.get("title") or ""
    name = product_title
    if variant_title and variant_title != "Default Title":
        name = f"{product_title} — {variant_title}"
    price = None
    try:
        price = float(variant.get("price")) if variant.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    return {
        "sku": (variant.get("sku") or "").strip(),
        "name": name,
        "shopifyVariantId": variant.get("id"),
        "shopifyPrice": price,
        "shopifyStock": variant.get("inventoryQuantity"),
        "options": {o.get("name"): o.get("value") for o in opts},
        # Bloque 1: identidad del producto Shopify de origen, para poder
        # agrupar por modelo en el panel (aunque Shopify lo fragmente en
        # variantes por color/talla/colocación de emblema).
        "modelName": product_title,
        "shopifyProductId": product_id,
    }


def sync_shopify_into_state(shopify: ShopifyClient, current_state: dict, default_threshold: int) -> tuple[dict, dict]:
    """Descarga productos y pedidos recientes de Shopify (solo lectura) y
    los fusiona sobre una COPIA del estado compartido actual.

    Devuelve (nuevo_estado, resumen) sin escribir nada todavía en Supabase
    -- eso lo hace la persona que llama a esta función.
    """
    products = list(current_state.get("products") or [])
    orders = list(current_state.get("orders") or [])
    movements = list(current_state.get("movements") or [])

    by_sku = {}
    for p in products:
        sku = str(p.get("sku") or "").strip().lower()
        if sku:
            by_sku[sku] = p

    new_products = 0
    updated_products = 0

    shopify_products = shopify.fetch_all_products_with_variants()
    for sp in shopify_products:
        title = sp.get("title") or ""
        product_id = sp.get("id") or ""
        for ve in (sp.get("variants") or {}).get("edges") or []:
            variant = ve["node"]
            fields = _variant_to_product_fields(variant, title, product_id)
            sku_key = fields["sku"].lower()
            if not sku_key:
                continue  # sin SKU no podemos casarlo de forma fiable; se ignora
            existing = by_sku.get(sku_key)
            if existing:
                existing["shopifyStock"] = fields["shopifyStock"]
                existing["shopifyVariantId"] = fields["shopifyVariantId"]
                existing["modelName"] = fields["modelName"]
                existing["shopifyProductId"] = fields["shopifyProductId"]
                existing["options"] = fields["options"]
                if fields["shopifyPrice"] is not None and not existing.get("price"):
                    existing["price"] = fields["shopifyPrice"]
                updated_products += 1
            else:
                new_product = {
                    "id": _uid(),
                    "sku": fields["sku"],
                    "name": fields["name"],
                    "stock": fields["shopifyStock"] if fields["shopifyStock"] is not None else 0,
                    "threshold": default_threshold,
                    "price": fields["shopifyPrice"] or 0,
                    "shopifyStock": fields["shopifyStock"],
                    "shopifyVariantId": fields["shopifyVariantId"],
                    "options": fields["options"],
                    "modelName": fields["modelName"],
                    "shopifyProductId": fields["shopifyProductId"],
                    "source": "shopify",
                }
                products.append(new_product)
                by_sku[sku_key] = new_product
                new_products += 1

    existing_keys = set()
    for o in orders:
        existing_keys.add((o.get("channel") or "") + "|" + str(o.get("externalId") or ""))

    new_orders = 0
    shopify_orders = shopify.fetch_recent_orders(limit=50)
    for so in shopify_orders:
        name = so.get("name") or so.get("id")
        key = "Shopify|" + str(name)
        if key in existing_keys:
            continue
        # Deliberadamente sin nombre de cliente (dato protegido, ver shopify_client.py)
        customer_name = ""
        money = ((so.get("currentTotalPriceSet") or {}).get("shopMoney") or {})
        total = money.get("amount")
        try:
            total = float(total) if total is not None else 0.0
        except (TypeError, ValueError):
            total = 0.0
        items = []
        for le in (so.get("lineItems") or {}).get("edges") or []:
            node = le["node"]
            items.append({
                "sku": node.get("sku") or "",
                "name": node.get("title") or "",
                "qty": node.get("quantity") or 0,
                "price": 0,
            })
        created_at = so.get("createdAt") or ""
        fulfillment_info = _extract_fulfillment_info(so.get("fulfillments"))
        order = {
            "id": _uid(),
            "externalId": name,
            "channel": "Shopify",
            "date": created_at[:10] if created_at else "",
            "customer": customer_name,
            "status": so.get("displayFulfillmentStatus") or "",
            "items": items,
            "total": total,
            "fulfilled": False,       # decisión deliberada: ver docstring
            "stockApplied": False,
            "source": "shopify-sync",
            "importedAt": _now_iso(),
            # Datos adicionales que Shopify sí expone (ninguno es dato
            # protegido de cliente): si el pedido viene del canal de TikTok
            # Shop, tiktokOrderId permite cruzarlo con el panel de TikTok.
            "tiktokOrderId": _extract_tiktok_order_id(so.get("tags")),
            **fulfillment_info,
        }
        orders.append(order)
        existing_keys.add(key)
        new_orders += 1

    next_state = {
        "products": products,
        "orders": orders,
        "movements": movements,
        # Materiales y costes no tienen nada que ver con Shopify: se
        # conservan tal cual para no perderlos en cada sincronización.
        "materials": current_state.get("materials") or [],
        "costs": current_state.get("costs") or {},
        "settings": current_state.get("settings") or {"defaultThreshold": default_threshold},
    }
    summary = {
        "newProducts": new_products,
        "updatedProducts": updated_products,
        "newOrders": new_orders,
        "ordersSeenInShopify": len(shopify_orders),
        "syncedAt": _now_iso(),
    }
    return next_state, summary


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
