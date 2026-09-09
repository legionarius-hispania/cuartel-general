"""
Legionarius Hispania — Cuartel General (app de escritorio)

Punto de entrada. Abre una ventana nativa (pywebview) con la misma interfaz
del panel web, pero con los datos guardados en Supabase (compartido entre
varios ordenadores) y con sincronización directa y de solo lectura con
Shopify.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback

import webview

from config import AppConfig, load_config, save_config
from shopify_client import ShopifyClient, ShopifyAuthError, ShopifyAPIError
from supabase_client import SupabaseClient, SupabaseError
from sync import sync_shopify_into_state

APP_TITLE = "Cuartel General — Legionarius Hispania"
DEFAULT_STATE = {
    "products": [],
    "orders": [],
    "movements": [],
    "materials": [],
    "dtfImages": [],
    "settings": {"defaultThreshold": 5},
    "costs": {
        "rates": {"blankShirtCost": 0, "dtfCostPerMeter": 0},
        "expenses": [],
        "machines": [],
        "ledger": [],
    },
}


def _web_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "web")


class Api:
    def __init__(self):
        self.cfg: AppConfig = load_config()
        self._sync_lock = threading.Lock()

    # ------------------------------------------------------------ config
    def get_config(self) -> dict:
        return self.cfg.to_public_dict()

    def save_config_fields(self, fields: dict) -> dict:
        """Guarda los campos recibidos desde la pantalla de Ajustes.
        Un client_secret vacío no borra el ya guardado (para no forzar a
        re-teclearlo cada vez que se abren los ajustes)."""
        if "shopify_shop" in fields:
            self.cfg.shopify_shop = (fields.get("shopify_shop") or "").strip()
        if "shopify_client_id" in fields:
            self.cfg.shopify_client_id = (fields.get("shopify_client_id") or "").strip()
        if fields.get("shopify_client_secret"):
            self.cfg.shopify_client_secret = fields["shopify_client_secret"].strip()
        if "supabase_url" in fields:
            self.cfg.supabase_url = (fields.get("supabase_url") or "").strip()
        if fields.get("supabase_secret_key"):
            self.cfg.supabase_anon_key = fields["supabase_secret_key"].strip()
        save_config(self.cfg)
        return {"ok": True, "config": self.cfg.to_public_dict()}

    def test_shopify_connection(self) -> dict:
        if not self.cfg.is_shopify_configured:
            return {"ok": False, "error": "Faltan credenciales de Shopify."}
        try:
            client = ShopifyClient(self.cfg.shopify_shop, self.cfg.shopify_client_id, self.cfg.shopify_client_secret)
            data = client.graphql("query { shop { name } }")
            return {"ok": True, "shopName": data["shop"]["name"]}
        except (ShopifyAuthError, ShopifyAPIError) as e:
            return {"ok": False, "error": _shopify_error_message(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"Error inesperado: {e}"}

    def test_supabase_connection(self) -> dict:
        if not self.cfg.is_supabase_configured:
            return {"ok": False, "error": "Faltan credenciales de Supabase."}
        try:
            client = SupabaseClient(self.cfg.supabase_url, self.cfg.supabase_anon_key)
            ok = client.health_check()
            return {"ok": ok, "error": None if ok else "La tabla app_state no respondió correctamente."}
        except SupabaseError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"Error inesperado: {e}"}

    # ------------------------------------------------------------- state
    def _supabase(self) -> SupabaseClient:
        return SupabaseClient(self.cfg.supabase_url, self.cfg.supabase_anon_key)

    def get_state(self) -> dict:
        """Lee el estado compartido desde Supabase.

        Importante (corregido tras auditoría del 2026-09-09): un fallo real
        de Supabase (red caída, credenciales revocadas, proyecto en pausa...)
        se registra y se propaga como excepción, en vez de devolver
        silenciosamente un estado vacío como si fuera bueno. Devolver aquí
        DEFAULT_STATE ante un error real haría que la app mostrase "Guardado"
        con los datos en blanco, y un guardado posterior podría sobrescribir
        en Supabase todos los datos reales del negocio con ese estado vacío.
        Solo se devuelve DEFAULT_STATE cuando Supabase respondió correctamente
        pero todavía no existe ninguna fila (primer arranque)."""
        if not self.cfg.is_supabase_configured:
            return dict(DEFAULT_STATE)
        try:
            rows = self._supabase().select("app_state", {"select": "data", "id": "eq.1"})
        except SupabaseError:
            traceback.print_exc()
            raise
        if rows:
            data = rows[0].get("data") or {}
            return _normalize(data)
        return dict(DEFAULT_STATE)

    def save_state(self, next_state: dict) -> dict:
        """Guarda el estado compartido en Supabase.

        Igual que en get_state: un fallo real ya no se silencia (antes se
        capturaba y se devolvía next_state como si se hubiera guardado bien,
        con lo que la interfaz mostraba "Guardado" aunque el guardado
        hubiera fallado de verdad). Ahora se registra y se propaga, para que
        el lado JavaScript (commit(), en app-script) lo trate como el fallo
        que es y muestre el aviso correspondiente."""
        next_state = _normalize(next_state)
        if not self.cfg.is_supabase_configured:
            return next_state
        try:
            self._supabase().update("app_state", {"id": 1}, {"data": next_state})
        except SupabaseError:
            traceback.print_exc()
            raise
        return next_state

    # -------------------------------------------------------------- sync
    def sync_shopify(self) -> dict:
        if not self._sync_lock.acquire(blocking=False):
            return {"ok": False, "error": "Ya hay una sincronización en curso."}
        try:
            if not self.cfg.is_shopify_configured:
                return {"ok": False, "error": "Faltan credenciales de Shopify. Configúralas en Ajustes."}
            if not self.cfg.is_supabase_configured:
                return {"ok": False, "error": "Faltan credenciales de Supabase. Configúralas en Ajustes."}
            client = ShopifyClient(self.cfg.shopify_shop, self.cfg.shopify_client_id, self.cfg.shopify_client_secret)
            current = self.get_state()
            try:
                next_state, summary = sync_shopify_into_state(client, current, self.cfg.default_threshold)
            except (ShopifyAuthError, ShopifyAPIError) as e:
                return {"ok": False, "error": _shopify_error_message(e)}
            self.save_state(next_state)
            return {"ok": True, **summary}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return {"ok": False, "error": f"Error inesperado: {e}"}
        finally:
            self._sync_lock.release()

    # ------------------------------------------------------------ files
    def save_backup_file(self, args: dict) -> dict:
        payload = (args or {}).get("payload", "")
        suggested_name = (args or {}).get("filename", "backup.json")
        window = webview.windows[0]
        result = window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=suggested_name,
            file_types=("Archivos JSON (*.json)", "Todos los archivos (*.*)"),
        )
        path = result[0] if isinstance(result, (list, tuple)) and result else result
        if not path:
            return {"ok": False, "cancelled": True}
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        return {"ok": True, "path": path}

    def pick_backup_file(self) -> dict:
        window = webview.windows[0]
        result = window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Archivos JSON (*.json)", "Todos los archivos (*.*)"),
        )
        path = result[0] if isinstance(result, (list, tuple)) and result else None
        if not path:
            return {"ok": False, "cancelled": True}
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"ok": True, "content": content}


def _shopify_error_message(e) -> str:
    """Incluye el detalle real de los errores GraphQL de Shopify (si los hay)
    en vez de solo el mensaje genérico, para poder diagnosticar sin volver
    a compilar la app cada vez."""
    base = str(e)
    errors = getattr(e, "errors", None)
    if errors:
        try:
            details = "; ".join(
                (err.get("message") if isinstance(err, dict) else str(err)) for err in errors
            )
            return f"{base}: {details}"
        except Exception:  # noqa: BLE001
            pass
    return base


def _normalize(raw: dict) -> dict:
    state = dict(raw) if isinstance(raw, dict) else {}
    if not isinstance(state.get("products"), list):
        state["products"] = []
    if not isinstance(state.get("orders"), list):
        state["orders"] = []
    # Checklist de preparación por pedido, organizada en 3 partes:
    # Parte 1 (camiseta, etiqueta de cuello, bandera, escudo, estampado),
    # Parte 2 (perfume, embolsado, publicidad, etiqueta de ropa) y
    # Parte 3 (tipo de caja, bolsa de envío -solo cajas de 2/3-, y
    # etiqueta de envío, siempre al final del flujo).
    _default_prep = {
        "camiseta": False, "etiquetaCuello": False, "bandera": False, "escudo": False,
        "estampado": False, "perfume": False, "embolsada": False, "publicidad": False,
        "etiquetaRopa": False, "cajaTipo": "", "bolsaCaja": False,
        "etiquetaTrackingOk": False,
    }
    for order in state["orders"]:
        if not isinstance(order, dict):
            continue
        prep = order.get("prep")
        if not isinstance(prep, dict):
            prep = {}
        merged = dict(_default_prep)
        merged.update({k: v for k, v in prep.items() if k in _default_prep})
        order["prep"] = merged
        # Usuario (rango de la Legión) que gestiona el pedido.
        if not isinstance(order.get("usuario"), str):
            order["usuario"] = ""
    if not isinstance(state.get("movements"), list):
        state["movements"] = []
    if not isinstance(state.get("materials"), list):
        state["materials"] = []
    # Base de imágenes de referencia de DTF (biblioteca dentro de Material):
    # cada una lleva unos checks de para qué elementos del checklist de
    # preparación vale (etiquetaCuello/bandera/escudo/estampado).
    if not isinstance(state.get("dtfImages"), list):
        state["dtfImages"] = []
    if not isinstance(state.get("settings"), dict):
        state["settings"] = {"defaultThreshold": 5}
    if not isinstance(state["settings"].get("defaultThreshold"), (int, float)):
        state["settings"]["defaultThreshold"] = 5
    # Bloque 1 (modelo de datos): equivalencias de nombres de color y lista
    # de tallas esperadas por modelo, usadas para agrupar por modelo y
    # detectar tallas ausentes en la vista de Inventario.
    if not isinstance(state["settings"].get("colorAliases"), list):
        state["settings"]["colorAliases"] = []
    if not isinstance(state["settings"].get("expectedSizes"), list):
        state["settings"]["expectedSizes"] = []
    # Costes: tarifas de referencia (camiseta en blanco, DTF por metro
    # lineal), gastos por categoría (planchas, cajas, embalaje, flyers,
    # mano de obra...), máquinas compradas, e ingresos/gastos generales.
    if not isinstance(state.get("costs"), dict):
        state["costs"] = {}
    if not isinstance(state["costs"].get("rates"), dict):
        state["costs"]["rates"] = {}
    if not isinstance(state["costs"]["rates"].get("blankShirtCost"), (int, float)):
        state["costs"]["rates"]["blankShirtCost"] = 0
    if not isinstance(state["costs"]["rates"].get("dtfCostPerMeter"), (int, float)):
        state["costs"]["rates"]["dtfCostPerMeter"] = 0
    if not isinstance(state["costs"].get("expenses"), list):
        state["costs"]["expenses"] = []
    if not isinstance(state["costs"].get("machines"), list):
        state["costs"]["machines"] = []
    if not isinstance(state["costs"].get("ledger"), list):
        state["costs"]["ledger"] = []
    return state


def main():
    api = Api()
    index_path = os.path.join(_web_dir(), "index.html")
    webview.create_window(APP_TITLE, index_path, js_api=api, width=1280, height=820, min_size=(960, 600))
    # debug=True habilita el clic derecho -> "Inspeccionar" (F12), para poder
    # ver errores de JavaScript directamente en la ventana si algo falla.
    webview.start(debug=True)


def _show_fatal_error(message: str) -> None:
    """Muestra el error incluso en un build --windowed (sin consola), para
    que nunca falle en silencio en el ordenador de un usuario no técnico."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "Legionarius Hispania — Error", 0x10)
        else:
            print(message, file=sys.stderr)
    except Exception:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        _show_fatal_error(
            "No se pudo iniciar la aplicación.\n\n"
            f"Detalle técnico: {exc}\n\n"
            "Si el problema persiste, comprueba que 'Microsoft Edge WebView2 Runtime' "
            "esté instalado (viene incluido en Windows 10/11 actualizados)."
        )
        sys.exit(1)
