#!/usr/bin/env python3
"""
Genera app/web/index.html y app/tablet/index.html a partir de la lógica
compartida (app/shared/app_logic.js) y de sus respectivas plantillas
(app/web/template.html, app/tablet/template.html).

Por qué existe este script (auditoría del 2026-09-09): antes, esos dos
archivos HTML se mantenían a mano por separado, duplicando ~2500 líneas de
lógica idénticas en un 98%. Cualquier cambio (una función nueva, una
corrección) había que aplicarlo dos veces, a mano, confiando en no
introducir una pequeña discrepancia. Ahora la lógica vive en un solo sitio
(app/shared/app_logic.js) y este script genera los dos archivos finales,
sustituyendo únicamente la constante IS_TABLET.

Uso:
    python3 tools/build_html.py            # genera ambos archivos
    python3 tools/build_html.py --check    # no escribe nada; sale con
                                            # código de error si los
                                            # archivos generados no
                                            # coincidirían con los ya
                                            # existentes (para CI: detecta
                                            # si alguien editó a mano
                                            # app/web/index.html o
                                            # app/tablet/index.html en vez
                                            # de app/shared/app_logic.js)

Nota sobre despliegues parciales (p. ej. aplicar primero solo los cambios de
escritorio y dejar la tablet para más adelante): si la plantilla de un
destino (app/web/template.html o app/tablet/template.html) todavía no
existe en el repositorio, ese destino se omite con un aviso en vez de fallar
— así este script y el workflow de build de Windows funcionan aunque
app/tablet/template.html no se haya añadido todavía.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED_LOGIC_PATH = ROOT / "app" / "shared" / "app_logic.js"

TARGETS = [
    {
        "name": "escritorio",
        "template": ROOT / "app" / "web" / "template.html",
        "output": ROOT / "app" / "web" / "index.html",
        "is_tablet": False,
    },
    {
        "name": "tablet",
        "template": ROOT / "app" / "tablet" / "template.html",
        "output": ROOT / "app" / "tablet" / "index.html",
        "is_tablet": True,
    },
]

IS_TABLET_LINE_RE = re.compile(r"var IS_TABLET = (?:true|false);")


def render_logic(is_tablet: bool) -> str:
    logic = SHARED_LOGIC_PATH.read_text(encoding="utf-8")
    replacement = f"var IS_TABLET = {'true' if is_tablet else 'false'};"
    new_logic, count = IS_TABLET_LINE_RE.subn(replacement, logic, count=1)
    if count != 1:
        raise RuntimeError(
            "No se encontró (o se encontró más de una vez) la línea "
            "'var IS_TABLET = ...;' en app/shared/app_logic.js — revisa que "
            "no se haya borrado ni duplicado por accidente."
        )
    return new_logic


def render_html(target: dict) -> str:
    template = target["template"].read_text(encoding="utf-8")
    if "{{APP_LOGIC}}" not in template:
        raise RuntimeError(f"La plantilla {target['template']} no tiene el marcador {{{{APP_LOGIC}}}}.")
    logic = render_logic(target["is_tablet"])
    return template.replace("{{APP_LOGIC}}", logic, 1)


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    mismatches = []
    skipped = []
    processed = []
    for target in TARGETS:
        if not target["template"].exists():
            # Despliegue parcial (p. ej. solo escritorio por ahora): no es un
            # error, simplemente este destino todavía no está listo.
            skipped.append(target)
            print(
                f"Aviso: no existe {target['template'].relative_to(ROOT)}; "
                f"se omite la generación de {target['name']} por ahora."
            )
            continue
        processed.append(target)
        html = render_html(target)
        if check_only:
            current = target["output"].read_text(encoding="utf-8") if target["output"].exists() else None
            if current != html:
                mismatches.append(target)
        else:
            target["output"].write_text(html, encoding="utf-8")
            print(f"Generado: {target['output'].relative_to(ROOT)} ({target['name']})")

    if not processed:
        print("No hay ninguna plantilla presente (ni app/web/template.html ni app/tablet/template.html); nada que generar.")
        return 1

    if check_only:
        if mismatches:
            print("DESACTUALIZADO — estos archivos no coinciden con lo que generaría app/shared/app_logic.js:")
            for t in mismatches:
                print(f"  - {t['output'].relative_to(ROOT)}")
            print("Ejecuta: python3 tools/build_html.py")
            return 1
        names = " y ".join(t["output"].relative_to(ROOT).as_posix() for t in processed)
        print(f"OK: {names} están al día con app/shared/app_logic.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
