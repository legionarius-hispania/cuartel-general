"""
Gestión de configuración local de la app.

La configuración (credenciales de Shopify y Supabase) se guarda SOLO en este
equipo, en un fichero JSON dentro de la carpeta de datos de aplicación del
usuario de Windows (%APPDATA%\\LegionariusHispania\\config.json). Nunca se
envía a ningún servidor de Anthropic/Claude ni se incluye en el ejecutable.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

APP_DIR_NAME = "LegionariusHispania"
CONFIG_FILENAME = "config.json"


def _app_data_dir() -> Path:
    """Devuelve la carpeta de datos de la app, creándola si no existe.

    En Windows usa %APPDATA%. Si la variable no existe (p.ej. al probar en
    Linux/Mac durante el desarrollo), cae a ~/.legionarius-hispania.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / APP_DIR_NAME
    else:
        base = Path.home() / f".{APP_DIR_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def config_path() -> Path:
    return _app_data_dir() / CONFIG_FILENAME


@dataclass
class AppConfig:
    shopify_shop: str = ""            # p.ej. "legionarius-hispania.myshopify.com"
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    supabase_url: str = ""            # p.ej. "https://xxxx.supabase.co"
    supabase_anon_key: str = ""
    default_threshold: int = 5
    sync_interval_minutes: int = 15

    @property
    def is_shopify_configured(self) -> bool:
        return bool(self.shopify_shop and self.shopify_client_id and self.shopify_client_secret)

    @property
    def is_supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    def to_public_dict(self) -> dict:
        """Versión para mostrar en la UI: no exponemos los secretos completos."""
        d = asdict(self)
        secret = d.get("shopify_client_secret") or ""
        if secret:
            d["shopify_client_secret"] = "•" * max(6, len(secret) - 4) + secret[-4:]
        anon = d.get("supabase_anon_key") or ""
        if anon:
            d["supabase_anon_key"] = "•" * max(6, len(anon) - 4) + anon[-4:]
        return d


def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        return AppConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return AppConfig()
    known = {f: data.get(f) for f in AppConfig.__dataclass_fields__ if f in data}
    return AppConfig(**known)


def save_config(cfg: AppConfig) -> None:
    path = config_path()
    path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
