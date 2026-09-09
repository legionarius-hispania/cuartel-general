"""
Pruebas rápidas con HTTP simulado (sin red real) para verificar:
- Renovación de token OAuth de Shopify (client credentials) y su caché.
- Construcción correcta de las llamadas REST a Supabase.
- La lógica de fusión de sync_shopify_into_state (productos/pedidos).
- Que un fallo real de Supabase en get_state/save_state se propague en vez
  de ocultarse (regresión del hallazgo #1 de la auditoría del 2026-09-09).

Ejecutar con: python3 -m pytest tests/test_logic.py -v
(o simplemente: python3 tests/test_logic.py)
"""
import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# main.py hace "import webview" (pywebview), que solo hace falta para la
# ventana nativa real y no está instalado en todos los entornos donde se
# ejecutan estas pruebas (p.ej. este propio entorno de desarrollo, en
# Linux). Como Api.get_state/save_state no usan webview.windows para nada,
# se sustituye por un doble antes de importar main, para poder probar esa
# lógica sin depender de que pywebview esté instalado.
if "webview" not in sys.modules:
    sys.modules["webview"] = MagicMock()

from shopify_client import ShopifyClient
from supabase_client import SupabaseClient, SupabaseError
from sync import sync_shopify_into_state
from main import Api


def fake_response(json_data, status=200, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.text = text or str(json_data)
    r.url = "https://fake"
    return r


def test_shopify_token_cached_and_refreshed():
    client = ShopifyClient("mitienda.myshopify.com", "cid", "csecret")
    calls = {"token": 0, "graphql": 0}

    def fake_post(url, headers=None, data=None, json=None, timeout=None):
        if url.endswith("/admin/oauth/access_token"):
            calls["token"] += 1
            return fake_response({"access_token": f"tok{calls['token']}", "expires_in": 86399})
        if url.endswith("/graphql.json"):
            calls["graphql"] += 1
            assert headers["X-Shopify-Access-Token"] == f"tok{calls['token']}"
            return fake_response({"data": {"shop": {"name": "Legionarius Hispania"}}})
        raise AssertionError("URL inesperada: " + url)

    with patch("shopify_client.requests.post", side_effect=fake_post):
        data1 = client.graphql("query { shop { name } }")
        data2 = client.graphql("query { shop { name } }")
        assert data1["shop"]["name"] == "Legionarius Hispania"
        assert calls["token"] == 1, "el token debe reutilizarse, no pedirse dos veces"
        assert calls["graphql"] == 2

    print("OK: test_shopify_token_cached_and_refreshed")


def test_shopify_token_expiry_triggers_refresh():
    client = ShopifyClient("mitienda.myshopify.com", "cid", "csecret")
    calls = {"token": 0}

    def fake_post(url, headers=None, data=None, json=None, timeout=None):
        if url.endswith("/admin/oauth/access_token"):
            calls["token"] += 1
            # token ya caducado casi al instante para forzar renovación en la 2a llamada
            return fake_response({"access_token": f"tok{calls['token']}", "expires_in": 1})
        return fake_response({"data": {"shop": {"name": "X"}}})

    with patch("shopify_client.requests.post", side_effect=fake_post):
        client.graphql("query { shop { name } }")
        client._token.expires_at = time.time() - 10  # forzar caducidad
        client.graphql("query { shop { name } }")
        assert calls["token"] == 2, "debe pedir un token nuevo cuando el anterior caducó"

    print("OK: test_shopify_token_expiry_triggers_refresh")


def test_supabase_update_uses_patch_with_eq_filter():
    client = SupabaseClient("https://xxx.supabase.co", "secretkey")
    captured = {}

    def fake_patch(url, headers=None, params=None, json=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        captured["headers"] = headers
        return fake_response([{"id": 1, "data": json["data"]}])

    with patch("supabase_client.requests.patch", side_effect=fake_patch):
        result = client.update("app_state", {"id": 1}, {"data": {"products": []}})
        assert captured["params"] == {"id": "eq.1"}
        assert captured["headers"]["apikey"] == "secretkey"
        assert result[0]["id"] == 1

    print("OK: test_supabase_update_uses_patch_with_eq_filter")


def test_sync_merges_products_without_clobbering_local_stock():
    shopify = MagicMock()
    shopify.fetch_all_products_with_variants.return_value = [
        {
            "title": "Camiseta Legión",
            "variants": {"edges": [
                {"node": {"id": "gid://v1", "title": "M", "sku": "LG-ALM-EE-M",
                          "price": "19.90", "inventoryQuantity": 42,
                          "selectedOptions": [{"name": "Talla", "value": "M"}]}},
                {"node": {"id": "gid://v2", "title": "L", "sku": "LG-ALM-EE-L",
                          "price": "19.90", "inventoryQuantity": 7,
                          "selectedOptions": [{"name": "Talla", "value": "L"}]}},
            ]}
        }
    ]
    shopify.fetch_recent_orders.return_value = [
        {"id": "gid://o1", "name": "#1001", "createdAt": "2026-08-20T10:00:00Z",
         "displayFulfillmentStatus": "FULFILLED",
         "customer": {"firstName": "Ana", "lastName": "Pérez"},
         "currentTotalPriceSet": {"shopMoney": {"amount": "39.80", "currencyCode": "EUR"}},
         "lineItems": {"edges": [{"node": {"title": "Camiseta Legión - M", "quantity": 2, "sku": "LG-ALM-EE-M"}}]}}
    ]

    current_state = {
        "products": [
            {"id": "local1", "sku": "LG-ALM-EE-M", "name": "Camiseta Legión — M", "stock": 3, "threshold": 5, "price": 19.9},
        ],
        "orders": [],
        "movements": [{"id": "m1", "sku": "LG-ALM-EE-M", "delta": -2, "reason": "ajuste previo"}],
        "settings": {"defaultThreshold": 5},
    }

    next_state, summary = sync_shopify_into_state(shopify, current_state, default_threshold=5)

    existing = next(p for p in next_state["products"] if p["sku"] == "LG-ALM-EE-M")
    assert existing["stock"] == 3, "el stock local gestionado a mano NO debe sobrescribirse"
    assert existing["shopifyStock"] == 42, "pero sí debe verse el stock de Shopify como referencia"

    new_one = next(p for p in next_state["products"] if p["sku"] == "LG-ALM-EE-L")
    assert new_one["stock"] == 7, "un producto nuevo sí debe tomar el stock de Shopify como inicial"

    assert summary["newProducts"] == 1
    assert summary["updatedProducts"] == 1
    assert summary["newOrders"] == 1
    order = next_state["orders"][0]
    assert order["fulfilled"] is False, "un pedido recién sincronizado no debe marcarse preparado automáticamente"
    assert order["stockApplied"] is False
    assert order["status"] == "FULFILLED", "el estado real de Shopify se guarda como referencia visible"

    # movimientos de stock existentes no deben tocarse por la sincronización
    assert len(next_state["movements"]) == 1

    print("OK: test_sync_merges_products_without_clobbering_local_stock")


def test_sync_preserves_materials_and_costs():
    """Los materiales (camisetas en blanco/DTF) y los datos de Costes no
    tienen nada que ver con Shopify: una sincronización no debe borrarlos."""
    shopify = MagicMock()
    shopify.fetch_all_products_with_variants.return_value = []
    shopify.fetch_recent_orders.return_value = []

    current_state = {
        "products": [], "orders": [], "movements": [],
        "settings": {"defaultThreshold": 5},
        "materials": [{"id": "mat1", "kind": "blank", "color": "Blanca", "size": "M", "stock": 10, "threshold": 2}],
        "costs": {
            "rates": {"blankShirtCost": 2.5, "dtfCostPerMeter": 1.2},
            "expenses": [{"id": "e1", "date": "2026-08-01", "category": "Cajas", "amount": 15.0, "note": ""}],
            "machines": [],
            "ledger": [],
        },
    }

    next_state, _ = sync_shopify_into_state(shopify, current_state, default_threshold=5)

    assert next_state["materials"] == current_state["materials"], "los materiales no deben perderse al sincronizar"
    assert next_state["costs"] == current_state["costs"], "los datos de costes no deben perderse al sincronizar"

    print("OK: test_sync_preserves_materials_and_costs")


def test_sync_skips_orders_already_imported():
    shopify = MagicMock()
    shopify.fetch_all_products_with_variants.return_value = []
    shopify.fetch_recent_orders.return_value = [
        {"id": "gid://o1", "name": "#1001", "createdAt": "2026-08-20T10:00:00Z",
         "displayFulfillmentStatus": "FULFILLED", "customer": {}, "currentTotalPriceSet": {"shopMoney": {"amount": "10"}},
         "lineItems": {"edges": []}}
    ]
    current_state = {
        "products": [], "movements": [], "settings": {"defaultThreshold": 5},
        "orders": [{"id": "x", "externalId": "#1001", "channel": "Shopify", "items": []}],
    }
    next_state, summary = sync_shopify_into_state(shopify, current_state, default_threshold=5)
    assert summary["newOrders"] == 0
    assert len(next_state["orders"]) == 1
    print("OK: test_sync_skips_orders_already_imported")


def _configured_api():
    """Api() con credenciales de Supabase de mentira, suficientes para que
    is_supabase_configured sea True y se intenten las llamadas reales
    (que estas pruebas interceptan con mocks)."""
    api = Api()
    api.cfg.supabase_url = "https://xxx.supabase.co"
    api.cfg.supabase_anon_key = "clave-de-prueba"
    return api


def test_get_state_propagates_supabase_error_instead_of_hiding_it():
    """Regresión del hallazgo #1 de la auditoría del 2026-09-09: antes, un
    fallo real de Supabase en get_state se capturaba y se devolvía
    silenciosamente DEFAULT_STATE (vacío) como si fuera un estado bueno, y
    la interfaz mostraba "Guardado" con los datos en blanco. Ahora debe
    propagarse como excepción."""
    api = _configured_api()
    with patch.object(SupabaseClient, "select", side_effect=SupabaseError("fallo simulado de red")):
        try:
            api.get_state()
        except SupabaseError:
            pass
        else:
            raise AssertionError("get_state debe propagar SupabaseError, no ocultarlo devolviendo un estado vacío")
    print("OK: test_get_state_propagates_supabase_error_instead_of_hiding_it")


def test_save_state_propagates_supabase_error_instead_of_hiding_it():
    """Regresión del hallazgo #1 de la auditoría del 2026-09-09: antes, un
    fallo real de Supabase en save_state se capturaba y se devolvía
    next_state tal cual, como si el guardado hubiera funcionado. Ahora debe
    propagarse como excepción para que la interfaz muestre el fallo real."""
    api = _configured_api()
    with patch.object(SupabaseClient, "update", side_effect=SupabaseError("fallo simulado de red")):
        try:
            api.save_state({"products": [], "orders": []})
        except SupabaseError:
            pass
        else:
            raise AssertionError("save_state debe propagar SupabaseError, no devolver next_state como si se hubiera guardado")
    print("OK: test_save_state_propagates_supabase_error_instead_of_hiding_it")


def test_get_state_still_returns_default_when_no_row_exists_yet():
    """La corrección del hallazgo #1 no debe romper el caso legítimo: si
    Supabase responde bien pero todavía no hay ninguna fila (primer
    arranque), get_state debe seguir devolviendo el estado por defecto,
    sin lanzar ningún error."""
    api = _configured_api()
    with patch.object(SupabaseClient, "select", return_value=[]):
        result = api.get_state()
    assert result["products"] == []
    assert result["orders"] == []
    print("OK: test_get_state_still_returns_default_when_no_row_exists_yet")


if __name__ == "__main__":
    test_shopify_token_cached_and_refreshed()
    test_shopify_token_expiry_triggers_refresh()
    test_supabase_update_uses_patch_with_eq_filter()
    test_sync_merges_products_without_clobbering_local_stock()
    test_sync_preserves_materials_and_costs()
    test_sync_skips_orders_already_imported()
    test_get_state_propagates_supabase_error_instead_of_hiding_it()
    test_save_state_propagates_supabase_error_instead_of_hiding_it()
    test_get_state_still_returns_default_when_no_row_exists_yet()
    print("\nTodas las pruebas pasaron.")
