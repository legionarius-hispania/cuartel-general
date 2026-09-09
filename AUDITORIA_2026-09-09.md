# Auditoría técnica — Cuartel General (Legionarius Hispania)

Fecha: 2026-09-09. Alcance: todo el repositorio tal como está en este momento (app de escritorio en Python/pywebview, PWA de tablet, cliente Shopify, cliente Supabase, pipeline de GitHub Actions y pruebas automatizadas). Metodología: lectura directa del código fuente completo (no until unas muestras), sin acceso al proyecto real de Supabase ni a los datos de producción — donde una afirmación depende de esos datos u otra configuración externa, lo marco como [NV] (no verificable desde aquí). Las conclusiones marcadas [SL] son deducciones lógicas a partir de código que sí he leído y cito con archivo y línea.

Antes de entrar en el detalle: el diseño general (Python/pywebview + Supabase compartido + sincronización de solo lectura con Shopify) es sólido y está bien documentado dentro del propio código — casi todos los módulos empiezan con un docstring explicando el porqué de cada decisión, lo cual ha facilitado mucho esta auditoría. Los problemas que siguen son reales y verificados, pero no cuestionan esa base.

## Resumen de severidad

| # | Hallazgo | Severidad |
|---|---|---|
| 1 | Los fallos de guardado/lectura contra Supabase se silencian en el escritorio y se muestran como "Guardado" | Crítica |
| 2 | Sin control de concurrencia entre dispositivos: el último guardado gana y puede borrar cambios de otro sin avisar | Crítica |
| 3 | No hay ningún límite real de tamaño del estado compartido, pese a existir un aviso de "datos demasiado grandes" que nunca se activa | Alta |
| 4 | `app/web/index.html` y `app/tablet/index.html` son casi el mismo archivo mantenido a mano por duplicado | Alta |
| 5 | `index.html` en la raíz del repo es una copia antigua, huérfana y desincronizada | Media |
| 6 | Cobertura de pruebas automatizadas muy limitada; el módulo con el bug más grave no tiene ninguna prueba | Media |
| 7 | Seguridad de la clave "anon" de Supabase: depende enteramente de una configuración externa que no puedo verificar | Media (a confirmar) |
| 8 | Mejoras menores (copias de seguridad automáticas, credenciales en texto plano local, stock sin suelo en 0) | Baja |

A continuación, el detalle de cada uno.

## 1. Crítico: los fallos al hablar con Supabase se ocultan y la app dice "Guardado" igualmente

Esto es, con diferencia, lo más importante que he encontrado. En `app/main.py`:

```python
def get_state(self) -> dict:
    if not self.cfg.is_supabase_configured:
        return dict(DEFAULT_STATE)
    try:
        rows = self._supabase().select("app_state", {"select": "data", "id": "eq.1"})
        if rows:
            data = rows[0].get("data") or {}
            return _normalize(data)
    except SupabaseError:
        pass
    return dict(DEFAULT_STATE)
```
(`app/main.py`, líneas 102-112)

```python
def save_state(self, next_state: dict) -> dict:
    next_state = _normalize(next_state)
    if not self.cfg.is_supabase_configured:
        return next_state
    try:
        self._supabase().update("app_state", {"id": 1}, {"data": next_state})
    except SupabaseError:
        pass
    return next_state
```
(`app/main.py`, líneas 114-122)

En ambos métodos, si la llamada a Supabase falla (caída momentánea de red, credenciales revocadas, el proyecto en pausa, etc.), la excepción `SupabaseError` se captura y se descarta con un simple `pass` — sin registrar nada, sin avisar a quien llama. La función devuelve igualmente un valor "normal": en `get_state`, el estado vacío por defecto (`DEFAULT_STATE`, sin productos ni pedidos); en `save_state`, el propio `next_state` que se intentó guardar, tal cual, como si el guardado hubiera funcionado.

El lado JavaScript confía en que una promesa resuelta significa éxito:

```js
callPy('save_state', next).then(function(saved){
    state = normalizeState(saved || next);
    setStatus('ready'); render();
}).catch(function(){
    setStatus('unsaved'); render();
});
```
(`app/web/index.html`, líneas 508-513, dentro de `commit()`)

Como `save_state` nunca relanza la excepción, esa rama `.catch()` no se ejecuta cuando Supabase falla — solo se ejecutaría ante un fallo distinto (por ejemplo, un error de JavaScript). Y el estado `'ready'` se pinta en verde con el texto "Guardado" (`app/web/index.html`, línea 559, `ready: {cls:'ok', text:'Guardado'}`). Es decir: **el usuario ve "Guardado" exactamente en el caso en que el guardado ha fallado**.

El caso de `get_state` es todavía más delicado, porque ocurre al arrancar la app y en cada sondeo cada 20 segundos (`POLL_MS = 20000`, línea 304): si en ese instante Supabase no responde, la app recibe un estado vacío (`DEFAULT_STATE`: sin productos, sin pedidos, sin materiales) y lo trata como bueno, mostrando también "Guardado". Si en ese momento el usuario hace cualquier cambio — o simplemente ya tenía algo escrito y pulsa cualquier acción que dispare `commit()` — ese `commit()` parte de ese estado vacío, lo guarda de vuelta en Supabase, y **sobrescribe todos los datos reales del negocio con un estado en blanco**. No he podido reproducir esto con datos reales (necesitaría forzar un fallo de red contra tu proyecto de Supabase, algo que no voy a hacer), pero la cadena de causa-efecto se sigue directamente leyendo el código citado arriba: [SL].

Un dato que refuerza que esto es una omisión y no una decisión deliberada: en el mismo archivo, los métodos de diagnóstico sí hacen lo correcto — `test_shopify_connection` y `test_supabase_connection` capturan sus errores y devuelven explícitamente `{"ok": False, "error": ...}` (`app/main.py`, líneas 74-96). Y en la versión de tablet, que no usa Python sino que habla con Supabase directamente desde el navegador, el `save_state` equivalente si hace lo correcto:

```js
save_state: function(nextState){
    ...
    return fetch(baseUrl() + '/rest/v1/app_state?id=eq.1', {...})
      .then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      ...
}
```
(`app/tablet/index.html`, líneas 2602-2611)

Aquí, si la petición falla, se lanza un error y la promesa se rechaza correctamente, así que en la tablet sí se ve el aviso "No se pudo guardar". Es decir, **la versión de escritorio es la única de las dos que oculta el fallo**, y el propio patrón correcto ya existe en el código, en otro sitio.

**Mejora propuesta**: en `get_state` y `save_state` de `app/main.py`, relanzar el error (o devolver un valor que la JS interprete como fallo, p. ej. `{"ok": False, "error": str(e)}` y ajustar el `.then()`/`.catch()` del lado JS para tratarlo así) en vez de `except SupabaseError: pass`. Como mínimo, registrar el error con `traceback.print_exc()` (ya se usa en `sync_shopify`, línea 142) para que quede constancia en la consola de depuración (`debug=True` en `webview.start`, línea 264, ya la deja accesible con clic derecho → Inspeccionar).

## 2. Crítico: sin control de concurrencia entre dispositivos — el último guardado gana

El diseño multi-equipo (varios PCs y/o la tablet compartiendo el mismo estado vía Supabase) no tiene ningún mecanismo de control de versiones ni de fusión. Cada `commit()` parte del estado que ese dispositivo tenía cargado en memoria (que puede tener hasta 20 segundos de antigüedad, el intervalo de `POLL_MS`), le aplica el cambio local, y sobrescribe por completo la fila `app_state` en Supabase con un `PATCH` (`app/supabase_client.py`, método `update`, líneas 79-89) que no comprueba si alguien más ha guardado algo mientras tanto.

Ejemplo concreto: si en el ordenador A alguien crea un pedido manual a las 12:00:03, y en el ordenador B (o en la tablet) alguien más marca "Preparado" otro pedido distinto a las 12:00:05 usando datos leídos a las 12:00:00 (antes de que el pedido nuevo existiera), el guardado de B sobrescribirá el de A: el pedido nuevo creado en A desaparecerá sin ningún aviso para nadie, porque B nunca lo tenía en su copia del estado. Esto no es un caso extremo: con dos personas trabajando a la vez (tú y Noelia, según tengo entendido de conversaciones anteriores) y una ventana de 20 segundos, es plausible que ocurra en el uso normal, sobre todo al preparar varios pedidos seguidos.

El mismo patrón afecta a `sync_shopify()` (`app/main.py`, líneas 125-146): si se pulsa "Sincronizar ahora" casi a la vez desde dos equipos, cada uno decide qué pedidos son "nuevos" comparando contra su propia copia del estado, y el que guarde en segundo lugar puede sobrescribir cambios hechos por el primero (o, en el peor caso, importar el mismo pedido dos veces si ambos lo consideraron nuevo antes de que el otro guardara).

**Mejora propuesta** (de más sencilla a más completa):
- Mínimo viable: antes de guardar, volver a leer el estado actual de Supabase y comparar un campo de versión/marca de tiempo; si ha cambiado desde la última lectura de este dispositivo, avisar ("Alguien ha guardado cambios mientras editabas; recarga antes de continuar") en vez de sobrescribir a ciegas.
- Más robusto: añadir una columna `updated_at`/`version` en la tabla `app_state` y usarla como condición en el `PATCH` (`Prefer: return=representation` ya se usa; se podría añadir un filtro `updated_at=eq.<valor leído>` a la query de Supabase, de forma que el `PATCH` no afecte a ninguna fila — y por tanto no sobrescriba nada — si alguien más ya guardó entretanto).
- Alternativa de fondo: mover de "guardar el documento entero" a "aplicar solo la operación" (por ejemplo, updates a nivel de fila en tablas separadas para pedidos/productos/movimientos en vez de un único JSON monolítico), que es como evitan este problema la mayoría de apps multiusuario — pero esto es un cambio de arquitectura considerable, no algo para hacer de forma incremental.

## 3. Alta: no hay ningún límite real de tamaño del estado, y el aviso que lo sugiere está muerto

Ya lo señalé al entregar la función de adjuntar etiquetas de TikTok Shop, y esta auditoría lo confirma de forma más amplia: el estado compartido completo (productos, pedidos, movimientos, materiales, costes, y ahora también fotos e imágenes de etiquetas en base64) viaja entero en cada guardado y cada sondeo. No existe ninguna poda ni archivado automático: `orders` y `movements` solo se reducen cuando se borra un pedido a mano (`deleteOrder`, `app/web/index.html` líneas 2088-2097); de lo contrario, ambos arrays solo crecen, para siempre, con cada pedido importado de Shopify y cada movimiento de stock generado.

Existe un estado de interfaz `'toolarge'` con su propio texto ("Datos demasiado grandes") y su propio aviso ("Los datos han crecido demasiado para guardarse. Exporta una copia de seguridad y considera archivar pedidos antiguos.", línea 1611) — pero he comprobado que `setStatus('toolarge')` no se llama en ningún punto del código. Es decir, ese aviso existe en la interfaz pero nunca se activa: si el estado llega a un tamaño problemático (por el límite que sea que tenga tu proyecto de Supabase o el propio PostgREST — no puedo confirmar cuál es ese límite exacto desde aquí [NV]), la app no lo detectará ni avisará; el guardado simplemente empezará a fallar, y ese fallo, además, quedaría oculto por el problema del punto 1.

**Mejora propuesta**: calcular el tamaño aproximado del JSON antes de guardar (`JSON.stringify(next).length`) y, si supera un umbral razonable (a definir; por ejemplo unos pocos MB), activar de verdad `setStatus('toolarge')` en vez de intentar el guardado. A medio plazo, combinarlo con un archivado real: mover a un histórico aparte (o simplemente dejar de sincronizar) los pedidos ya completados y con más de X meses de antigüedad, en vez de conservarlos todos indefinidamente en el mismo documento que se lee y escribe constantemente.

## 4. Alta: `app/web/index.html` y `app/tablet/index.html` son, en la práctica, el mismo archivo duplicado a mano

He extraído y comparado el bloque de lógica (`<script id="app-script">`) de ambos archivos: **2503 líneas en la versión de escritorio, 2485 en la de tablet, y solo 44 líneas distintas entre ambas** (menos del 2%). Las diferencias reales son las esperadas y deliberadas — la tablet no pide credenciales de Shopify, tiene otro texto en el pie de página, etc. — pero el 98% restante es idéntico carácter por carácter.

Esto significa que cualquier función nueva o corrección (como la del checklist de preparación, la paginación, o la de adjuntar etiquetas de esta misma sesión) hay que implementarla dos veces, a mano, en dos archivos de más de 2500 líneas cada uno, confiando en localizar el punto exacto equivalente en ambos y en no introducir una pequeña discrepancia. Es exactamente el trabajo que he tenido que hacer yo mismo en esta sesión para la función de etiquetas: localizar la misma función en el segundo archivo, verificar que el contexto era idéntico, y aplicar el cambio por separado. Tarde o temprano, con suficientes cambios, es cuestión de tiempo que un cambio se aplique en un archivo y se olvide en el otro, y las dos versiones empiecen a divergir en silencio (nadie lo notará hasta que alguien use la tablet y vea que le falta algo que sí está en el ordenador, o viceversa).

**Mejora propuesta**: extraer ese bloque de lógica compartida (`app-script`) a un único archivo fuente (por ejemplo `app/shared/app-logic.js`), y generar `app/web/index.html` y `app/tablet/index.html` a partir de una plantilla + ese archivo compartido en el momento de compilar/desplegar (un script sencillo de Python o Node que inserte el contenido en la plantilla correspondiente, ejecutado antes de `pyinstaller` y antes de publicar en GitHub Pages). Así solo se edita una vez, y las dos superficies quedan garantizadas idénticas en la parte común por construcción, no por disciplina manual.

## 5. Media: `index.html` en la raíz del repositorio es una copia antigua y huérfana

Además de `app/web/index.html` y `app/tablet/index.html`, existe un tercer `index.html` en la raíz del proyecto. Lo he comparado con `app/web/index.html`: **le faltan al menos estas mejoras ya entregadas** (comprobado por diferencia textual directa):
- El ancho máximo del contenido sigue fijo en 1180px (`max-width:1180px`, línea 80 de este archivo) en vez de los 1920px ya corregidos para aprovechar pantallas grandes.
- No tiene la regla de contraste para inputs/selects sueltos (el bug de texto invisible ya corregido en `app/web/index.html`).
- No tiene la reestructuración del checklist de preparación en 3 partes (clases `.prep-part`).
- No tiene el coste por unidad (`unitCost`) de camisetas en blanco por color/talla.
- (No he comprobado el resto línea a línea, pero dado que le faltan estas cinco mejoras consecutivas de sesiones anteriores, es razonable asumir que también le faltan las más recientes: paginación de 10, campos de ID de TikTok/nº de seguimiento/agencia, y la función de adjuntar etiquetas — [SL].)

Ningún flujo de trabajo de GitHub Actions lo usa: `build-windows.yml` empaqueta explícitamente la carpeta `app/web` (`--add-data "web;web"`, referenciada desde `app/main.py`), y `deploy-tablet.yml` publica explícitamente la carpeta `app/tablet`. Este archivo de la raíz no aparece en ningún paso de ningún workflow, así que, hasta donde puedo comprobar en este repositorio, no se está sirviendo a nadie [NV: no puedo descartar que exista algún otro despliegue externo, fuera de este repositorio, que sí lo use].

**Mejora propuesta**: si de verdad no se usa (lo más probable, a juzgar por el propio repositorio), eliminarlo o, si prefieres conservarlo como referencia histórica, moverlo a una carpeta claramente marcada como archivo/histórico, para que ni una futura sesión de IA ni tú mismo lo confundáis con la versión activa.

## 6. Media: cobertura de pruebas muy limitada, y justo el módulo con el bug crítico no tiene ninguna

`app/tests/test_logic.py` tiene 6 pruebas, todas centradas en `shopify_client.py` (caché y renovación de token), `supabase_client.py` (forma de las peticiones REST) y `sync.py` (fusión de productos/pedidos de Shopify). Están bien escritas y son útiles, pero:
- No importan ni prueban nada de `app/main.py` — ni la clase `Api`, ni `_normalize`, ni (crucialmente) el comportamiento de `get_state`/`save_state` ante un fallo de Supabase, que es exactamente el bug del punto 1. Una prueba que simulara un `SupabaseError` en `save_state` y comprobara que el resultado refleja el fallo (en vez de devolver `next_state` como si nada) habría detectado este problema antes de que llegara a producción.
- No hay ninguna prueba automatizada de la lógica de negocio en JavaScript (deducción de stock al marcar "Preparado", cálculo de costes, checklist de preparación, paginación), más allá de las comprobaciones puntuales de sintaxis (`node --check`) y las pruebas manuales con Playwright que he ido escribiendo y descartando en cada sesión de cambios — que no quedan guardadas para volver a ejecutarlas en el futuro.
- No hay ninguna prueba, ni siquiera manual/documentada, de `config.py`.

**Mejora propuesta**: añadir un test que fuerce un `SupabaseError` en `save_state`/`get_state` y compruebe que el fallo se refleja (esto además serviría como prueba de regresión una vez corregido el punto 1). Y considerar conservar como archivo permanente (en vez de crear y borrar en cada sesión) al menos una versión de la prueba Playwright que verifica el flujo completo de un pedido (crear, marcar preparado, comprobar descuento de stock, adjuntar etiqueta), para poder ejecutarla automáticamente en cada cambio futuro del `app-script`.

## 7. Media (a confirmar): seguridad de la clave "anon" de Supabase

Esto depende de una configuración que está en tu proyecto de Supabase, no en este repositorio, así que lo marco explícitamente como [NV] en lo que no puedo verificar. Lo que sí puedo confirmar leyendo el código: tanto la app de escritorio como la de tablet acceden a Supabase directamente desde el cliente usando la clave "anon" / "publishable" (`app/supabase_client.py`, `app/tablet/index.html` líneas 2550-2558), sin ninguna capa adicional de autenticación de usuario. El propio código de la tablet advierte correctamente de usar la clave "anon" y nunca la "service_role" (comentario en `app/tablet/index.html`, líneas 2524-2527), lo cual es la precaución correcta.

Pero esa clave "anon", por sí sola, en una tabla de Supabase con Row Level Security (RLS) desactivado o mal configurado, permite leer y escribir cualquier fila de esa tabla a quien la tenga — y esa clave vive en texto plano en `%APPDATA%\LegionariusHispania\config.json` en cada PC (`app/config.py`, línea 84, `path.write_text(json.dumps(...))`, sin cifrar) y en el `localStorage` del navegador de cada tablet (`app/tablet/index.html`, línea 2532, `LS_KEY`). No he podido comprobar si tu proyecto de Supabase tiene RLS activado en la tabla `app_state` — eso solo se ve desde el propio panel de Supabase, no desde este repositorio.

**Qué te recomiendo verificar tú mismo** (no puedo hacerlo yo desde aquí): entra en tu proyecto de Supabase → Authentication/Table Editor → tabla `app_state` → comprueba si "Row Level Security" está activado y si hay alguna política que restrinja el acceso. Si está desactivado, cualquiera que obtenga esa clave anon (por ejemplo, inspeccionando el tráfico de red de la tablet, o el archivo de configuración de un PC) podría leer o modificar todos los pedidos, inventario y costes del negocio desde fuera, sin necesitar ninguna otra credencial.

## 8. Baja: mejoras menores

- **Copias de seguridad**: existe la función de exportar/restaurar backup (`save_backup_file`/`pick_backup_file`, `app/main.py` líneas 148-174), pero es manual — solo se genera si alguien pulsa el botón. Dado el riesgo de los puntos 1-3, sería razonable programar una exportación automática periódica (por ejemplo, una vez al día, la primera vez que se abre la app) además de la manual.
- **Stock sin suelo en 0**: `applyOrderStockOnState` (`app/web/index.html`, líneas 2027-2046) resta del stock sin comprobar que no baje de 0. Esto puede ser intencional (reflejar ventas en negativo hasta que se reponga), pero si no lo es, valdría la pena decidirlo explícitamente y, si se quiere avisar, mostrar un aviso cuando el stock de un producto quede en negativo.
- **Credenciales en texto plano**: tanto `config.json` (escritorio) como el `localStorage` de la tablet guardan las claves sin cifrar. Es una práctica común para herramientas internas de un equipo pequeño y el propio código ya dice explícitamente que esos datos "nunca se envían a ningún servidor de Anthropic/Claude" (`app/config.py`, líneas 4-7); lo señalo solo como algo a tener en cuenta si en algún momento estos dispositivos dejan de ser de confianza exclusiva del negocio.

## Qué haría primero

Si tuviera que priorizar solo tres cosas: primero el punto 1 (dejar de ocultar los fallos de Supabase — es un cambio pequeño y acota el mayor riesgo de pérdida de datos), después el punto 3 (activar de verdad el aviso de "datos demasiado grandes", que además es la base para poder abordar con seguridad el archivado de pedidos antiguos), y en tercer lugar el punto 4 (unificar la lógica compartida de escritorio y tablet en un solo archivo) porque, aunque no es urgente, cuanto más tiempo pase, más caro será deshacer la duplicación ya acumulada.

No he tocado ningún archivo del proyecto al hacer esta auditoría — es un análisis de solo lectura. Dime si quieres que implemente alguna de estas mejoras y por cuál empezamos.
