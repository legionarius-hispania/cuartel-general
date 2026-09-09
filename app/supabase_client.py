"""
Cliente ligero para la API REST (PostgREST) de Supabase.

Deliberadamente no usamos el SDK oficial 'supabase-py' para mantener el
ejecutable ligero (evita dependencias transitivas de websockets/gotrue que
no necesitamos para un simple select/insert/update). Se habla directamente
con el endpoint REST autogenerado, documentado en:
https://supabase.com/docs/guides/api
"""
from __future__ import annotations

from typing import Any

import requests


class SupabaseError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SupabaseClient:
    def __init__(self, url: str, anon_key: str, timeout: float = 20.0):
        self.base_url = url.strip().rstrip("/")
        self.anon_key = anon_key.strip()
        self.timeout = timeout

    def _headers(self, prefer: str | None = None) -> dict:
        h = {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.anon_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            h["Prefer"] = prefer
        return h

    def _url(self, table: str) -> str:
        return f"{self.base_url}/rest/v1/{table}"

    def _raise_if_error(self, resp: requests.Response) -> None:
        if resp.status_code >= 400:
            raise SupabaseError(
                f"Supabase respondió HTTP {resp.status_code} en {resp.url}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )

    def select(self, table: str, params: dict | None = None) -> list[dict]:
        resp = requests.get(
            self._url(table), headers=self._headers(), params=params or {}, timeout=self.timeout
        )
        self._raise_if_error(resp)
        return resp.json()

    def insert(self, table: str, rows: list[dict] | dict) -> list[dict]:
        resp = requests.post(
            self._url(table),
            headers=self._headers(prefer="return=representation"),
            json=rows,
            timeout=self.timeout,
        )
        self._raise_if_error(resp)
        return resp.json()

    def upsert(self, table: str, rows: list[dict] | dict, on_conflict: str) -> list[dict]:
        resp = requests.post(
            self._url(table),
            headers=self._headers(prefer=f"resolution=merge-duplicates,return=representation"),
            params={"on_conflict": on_conflict},
            json=rows,
            timeout=self.timeout,
        )
        self._raise_if_error(resp)
        return resp.json()

    def update(self, table: str, match: dict, values: dict) -> list[dict]:
        params = {f"{k}": f"eq.{v}" for k, v in match.items()}
        resp = requests.patch(
            self._url(table),
            headers=self._headers(prefer="return=representation"),
            params=params,
            json=values,
            timeout=self.timeout,
        )
        self._raise_if_error(resp)
        return resp.json()

    def delete(self, table: str, match: dict) -> None:
        params = {f"{k}": f"eq.{v}" for k, v in match.items()}
        resp = requests.delete(self._url(table), headers=self._headers(), params=params, timeout=self.timeout)
        self._raise_if_error(resp)

    def health_check(self) -> bool:
        """Comprueba credenciales/conectividad contra la tabla app_state."""
        resp = requests.get(
            self._url("app_state"), headers=self._headers(), params={"limit": 1}, timeout=self.timeout
        )
        return resp.status_code == 200
