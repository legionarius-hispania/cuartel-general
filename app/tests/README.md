# Pruebas

## Lógica de Python

```bash
python3 app/tests/test_logic.py
```

Cubre la sincronización con Shopify, el cliente de Supabase, y que los fallos de guardado
se detecten correctamente (auditoría del 2026-09-09) en vez de ocultarse como si el guardado
hubiera ido bien.

No requiere red ni credenciales: los clientes externos (Shopify, Supabase) están simulados
(`unittest.mock`).

## Extremo a extremo con navegador (Playwright)

`app/tests/test_ui_e2e.js` prueba el flujo completo de un pedido —crear, marcar "Preparado"
(y comprobar que se descuenta el stock), adjuntar y quitar una etiqueta de envío— directamente
sobre `app/web/index.html` y `app/tablet/index.html`, tal como los abre cada superficie
(como archivo local, sin servidor, en modo "local" sin guardado permanente).

Instalación (una sola vez):

```bash
npm install
npx playwright install chromium
```

Ejecución:

```bash
npm run test:e2e
```

(equivalente a `node app/tests/test_ui_e2e.js`)

Notas:

- No requiere pywebview ni credenciales de Shopify/Supabase.
- Si `app/shared/app_logic.js` cambió, ejecuta antes `python3 tools/build_html.py` para que
  `app/web/index.html` y `app/tablet/index.html` reflejen el cambio (ver README.md en la raíz
  del proyecto) — esta prueba lee esos dos archivos generados, no el archivo compartido.
- La variable de entorno opcional `PLAYWRIGHT_CHROMIUM_PATH` permite indicar la ruta al
  ejecutable de Chromium si Playwright no lo encuentra automáticamente.
