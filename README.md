# Cuartel General — Legionarius Hispania

App interna de pedidos, inventario, material y costes. Backend en Python (`app/main.py`, pywebview) con estado compartido en Supabase y sincronización de solo lectura con Shopify. Además de la app de escritorio para Windows, existe una versión PWA para tablet (`app/tablet/`), desplegada en GitHub Pages.

## Estructura del código de la interfaz (importante)

**`app/web/index.html` y `app/tablet/index.html` son archivos GENERADOS. No se editan a mano.**

Desde la unificación del 2026-09-09 (auditoría técnica), toda la lógica de la interfaz (pantallas, estado, checklist de preparación, costes, pedidos...) vive en un único archivo:

- `app/shared/app_logic.js` — la lógica compartida, con una constante `IS_TABLET` que marca los pocos puntos donde escritorio y tablet se comportan de forma distinta (p. ej. la tablet no pide credenciales de Shopify).

Las plantillas HTML de cada superficie son:

- `app/web/template.html` — cabecera y estructura de la app de escritorio.
- `app/tablet/template.html` — cabecera, estructura, y el `tablet-bridge` (el puente que habla con Supabase directamente desde el navegador, ya que la tablet no tiene el backend de Python).

Para generar `app/web/index.html` y `app/tablet/index.html` a partir de esos archivos:

```bash
python3 tools/build_html.py
```

Este paso ya está integrado en ambos workflows de GitHub Actions (`build-windows.yml` y `deploy-tablet.yml`), así que no hace falta ejecutarlo a mano antes de un despliegue — pero sí hace falta ejecutarlo a mano después de editar `app/shared/app_logic.js` (o cualquier plantilla) para poder probar los cambios localmente abriendo `app/web/index.html` en el navegador.

Para comprobar que los archivos generados están al día sin sobrescribir nada (por ejemplo, si alguien ha editado `app/web/index.html` o `app/tablet/index.html` directamente por error):

```bash
python3 tools/build_html.py --check
```

### Por qué existe esto

Antes, `app/web/index.html` y `app/tablet/index.html` se mantenían como dos copias casi idénticas (más de 2500 líneas cada una, con menos de un 2% de diferencia real entre ambas), editadas a mano por separado. Cualquier función nueva había que implementarla dos veces, confiando en no introducir una pequeña discrepancia entre las dos versiones. Ahora solo se edita `app/shared/app_logic.js` una vez.

## Pruebas

- `python3 app/tests/test_logic.py` — pruebas de la lógica de Python (sincronización con Shopify, cliente de Supabase, y que los fallos de guardado se detecten correctamente en vez de ocultarse).
- Pruebas de extremo a extremo con navegador (Playwright): ver `app/tests/README.md`.

## Historial

`archive/` contiene una copia antigua y ya no usada de la interfaz, conservada solo como referencia (ver `archive/README.md`).
