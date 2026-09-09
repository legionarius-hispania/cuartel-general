/**
 * Pruebas de extremo a extremo con navegador (Playwright) del flujo
 * completo de un pedido: crear, marcar preparado (y comprobar que se
 * descuenta el stock), y adjuntar/quitar una etiqueta de envío.
 *
 * Añadido tras la auditoría técnica del 2026-09-09, a petición explícita:
 * antes, este tipo de prueba se escribía y se descartaba en cada sesión de
 * cambios (no quedaba guardada para volver a ejecutarla). Se ejecuta contra
 * app/web/index.html y app/tablet/index.html directamente como archivo
 * local (sin necesidad de un servidor), tal como las usa cada superficie.
 *
 * Uso:
 *   npm install        (una vez, instala playwright como devDependency)
 *   npx playwright install chromium   (una vez, descarga el navegador)
 *   npm run test:e2e
 *   (o: node app/tests/test_ui_e2e.js)
 *
 * No requiere pywebview ni credenciales de Supabase/Shopify: se ejecuta en
 * modo "local" (sin guardado permanente), que es justo el modo con el que
 * arranca la app cuando no hay puente nativo — suficiente para probar la
 * lógica de negocio en el navegador.
 */
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');
const os = require('os');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const TARGETS = [
  { name: 'escritorio', file: path.join(REPO_ROOT, 'app', 'web', 'index.html') },
  { name: 'tablet', file: path.join(REPO_ROOT, 'app', 'tablet', 'index.html') },
];

let failures = 0;

function ok(label) {
  console.log('OK: ' + label);
}

function fail(label, detail) {
  failures++;
  console.error('FALLO: ' + label + (detail ? ' -- ' + detail : ''));
}

async function assertEq(actual, expected, label) {
  if (actual === expected) {
    ok(label);
  } else {
    fail(label, `esperado ${JSON.stringify(expected)}, obtenido ${JSON.stringify(actual)}`);
  }
}

async function assertTrue(cond, label) {
  if (cond) {
    ok(label);
  } else {
    fail(label);
  }
}

async function runOrderFlow(target) {
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_PATH || undefined;
  const browser = await chromium.launch(executablePath ? { executablePath } : {});
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on('pageerror', (err) => consoleErrors.push('pageerror: ' + err.message));
  page.on('console', (msg) => {
    const text = msg.text();
    if (msg.type() === 'error' && !text.includes('ERR_TUNNEL_CONNECTION_FAILED') && !text.includes('service worker')) {
      consoleErrors.push('console: ' + text);
    }
  });

  await page.goto('file://' + target.file);
  await page.waitForTimeout(300);

  // --- crear un producto para poder comprobar el descuento de stock ---
  // (el formulario "Añadir producto" está siempre visible en Inventario,
  // no hace falta desplegarlo)
  await page.click('[data-action="tab"][data-tab="inventory"]');
  await page.waitForTimeout(100);
  await page.fill('[data-action="np-field"][data-field="sku"]', 'SKU-E2E');
  await page.fill('[data-action="np-field"][data-field="name"]', 'Producto E2E');
  await page.fill('[data-action="np-field"][data-field="stock"]', '10');
  await page.fill('[data-action="np-field"][data-field="threshold"]', '2');
  await page.click('[data-action="np-submit"]');
  await page.waitForTimeout(150);

  // --- crear un pedido manual con ese SKU ---
  await page.click('[data-action="tab"][data-tab="orders"]');
  await page.click('[data-action="toggle-new-order"]');
  await page.fill('[data-action="mo-item"][data-field="sku"][data-idx="0"]', 'SKU-E2E');
  await page.fill('[data-action="mo-item"][data-field="name"][data-idx="0"]', 'Producto E2E');
  await page.fill('[data-action="mo-item"][data-field="qty"][data-idx="0"]', '3');
  await page.fill('[data-action="mo-item"][data-field="price"][data-idx="0"]', '9.5');
  await page.click('[data-action="mo-submit"]');
  await page.waitForTimeout(150);

  // La fila de un pedido en la tabla solo muestra "N líneas" (colapsada);
  // el nombre del producto solo aparece al desplegarla con
  // "toggle-order-items", así que hay que desplegarla antes de comprobar
  // que el pedido recién creado contiene el producto esperado.
  const hasToggleItemsBtn = await page.evaluate(() => !!document.querySelector('[data-action="toggle-order-items"]'));
  await assertTrue(hasToggleItemsBtn, `[${target.name}] el pedido creado aparece en la lista`);
  if (hasToggleItemsBtn) {
    // Cada clic vuelve a pintar toda la tabla (render() sustituye el DOM),
    // así que hay que volver a buscar el botón en cada paso en vez de
    // reutilizar el mismo ElementHandle (queda desconectado del DOM).
    await page.click('[data-action="toggle-order-items"]');
    await page.waitForTimeout(150);
  }
  const orderHasProduct = await page.evaluate(() => document.body.innerText.includes('Producto E2E'));
  await assertTrue(orderHasProduct, `[${target.name}] al desplegar el pedido se ve el producto añadido`);
  // volver a colapsar antes de continuar, para dejar la tabla en el estado
  // esperado por los siguientes pasos
  if (hasToggleItemsBtn) {
    await page.click('[data-action="toggle-order-items"]');
    await page.waitForTimeout(100);
  }

  // --- receta de producto y material: crear una camiseta en blanco y un
  // DTF, asignar el DTF a la receta del producto de prueba, y comprobar
  // que al completar el checklist de preparación se descuenta solo (sin
  // marcar nada a mano) tanto el stock del producto como el de esos dos
  // materiales. Auditoría del 2026-09-09: "Preparado" ya no es una
  // casilla propia, y el stock de Material se conecta con los pedidos. ---
  await page.evaluate(() => window.__setProductColorForTests('SKU-E2E', 'Blanca', 'M'));
  await page.waitForTimeout(100);

  await page.click('[data-action="tab"][data-tab="material"]');
  await page.waitForTimeout(100);
  await page.selectOption('[data-action="blank-field"][data-field="color"]', 'Blanca');
  await page.selectOption('[data-action="blank-field"][data-field="size"]', 'M');
  await page.fill('[data-action="blank-field"][data-field="stock"]', '5');
  await page.click('[data-action="blank-submit"]');
  await page.waitForTimeout(150);

  await page.fill('[data-action="dtf-field"][data-field="name"]', 'DTF E2E receta');
  await page.selectOption('[data-action="dtf-field"][data-field="placement"]', 'Pecho');
  await page.fill('[data-action="dtf-field"][data-field="stock"]', '5');
  await page.click('[data-action="dtf-submit"]');
  await page.waitForTimeout(150);

  await page.click('[data-action="tab"][data-tab="inventory"]');
  await page.waitForTimeout(100);
  await page.click('[data-action="open-edit"]');
  await page.waitForTimeout(100);
  const hasRecipeCheckbox = await page.evaluate(() => !!document.querySelector('[data-action="ed-recipe-toggle"]'));
  await assertTrue(hasRecipeCheckbox, `[${target.name}] al editar el producto, aparece la casilla para asignarle el DTF a la receta`);
  if (hasRecipeCheckbox) {
    await page.check('[data-action="ed-recipe-toggle"]');
    await page.waitForTimeout(100);
  }
  await page.click('[data-action="ed-save"]');
  await page.waitForTimeout(150);

  async function readRowText(needle) {
    return page.evaluate((n) => {
      const row = Array.from(document.querySelectorAll('tr')).find((tr) => tr.textContent.includes(n));
      return row ? row.textContent : null;
    }, needle);
  }

  // --- completar el checklist de preparación: "Preparado" debe activarse
  // solo, sin que exista ya ninguna casilla aparte para marcarlo a mano ---
  await page.click('[data-action="tab"][data-tab="orders"]');
  await page.waitForTimeout(100);
  const hasFulfilledCheckbox = await page.evaluate(() => !!document.querySelector('[data-action="toggle-fulfilled"]'));
  await assertTrue(!hasFulfilledCheckbox, `[${target.name}] ya no existe una casilla "Preparado" aparte (se activa sola al completar el checklist)`);

  await page.click('[data-action="toggle-order-prep"]');
  await page.waitForTimeout(100);
  const checklistFields = ['camiseta', 'etiquetaCuello', 'bandera', 'escudo', 'estampado', 'perfume', 'embolsada', 'publicidad', 'etiquetaRopa'];
  for (const field of checklistFields) {
    // page.check() (a diferencia de un ElementHandle obtenido con page.$)
    // vuelve a localizar el elemento si hace falta, por si el "change"
    // dispara un render() síncrono que sustituye el DOM antes de que
    // termine la acción.
    await page.check(`[data-action="prep-toggle"][data-field="${field}"]`);
    await page.waitForTimeout(50);
  }
  await page.selectOption('[data-action="prep-caja"]', '1');
  await page.waitForTimeout(50);
  await page.check('[data-action="prep-toggle"][data-field="etiquetaTrackingOk"]');
  await page.waitForTimeout(150);

  const fulfilledAfterChecklist = await page.evaluate(() => document.body.innerText.includes('Preparado — stock descontado'));
  await assertTrue(fulfilledAfterChecklist, `[${target.name}] al completar el checklist, el pedido queda "Preparado" solo (sin marcarlo a mano)`);

  await page.click('[data-action="tab"][data-tab="inventory"]');
  await page.waitForTimeout(100);
  let stockText = await readRowText('SKU-E2E');
  await assertTrue(!!stockText && stockText.includes('7'), `[${target.name}] el stock del producto bajó de 10 a 7 al completarse el checklist (3 unidades del pedido)`);

  await page.click('[data-action="tab"][data-tab="material"]');
  await page.waitForTimeout(100);
  let blankStockText = await readRowText('Blanca');
  await assertTrue(!!blankStockText && blankStockText.includes('2'), `[${target.name}] el stock de la camiseta en blanco Blanca/M bajó de 5 a 2 con la misma preparación`);
  let dtfStockText = await readRowText('DTF E2E receta');
  await assertTrue(!!dtfStockText && dtfStockText.includes('2'), `[${target.name}] el stock del DTF de la receta bajó de 5 a 2 con la misma preparación`);

  // --- "Deshacer preparación": revierte los tres descuentos de stock ---
  await page.click('[data-action="tab"][data-tab="orders"]');
  await page.waitForTimeout(100);
  await page.click('[data-action="undo-fulfilled"]');
  await page.waitForTimeout(150);
  const fulfilledAfterUndo = await page.evaluate(() => document.body.innerText.includes('Preparado — stock descontado'));
  await assertTrue(!fulfilledAfterUndo, `[${target.name}] "Deshacer preparación" quita el aviso de "Preparado"`);

  await page.click('[data-action="tab"][data-tab="inventory"]');
  await page.waitForTimeout(100);
  stockText = await readRowText('SKU-E2E');
  await assertTrue(!!stockText && stockText.includes('10'), `[${target.name}] "Deshacer preparación" devuelve el stock del producto a 10`);

  await page.click('[data-action="tab"][data-tab="material"]');
  await page.waitForTimeout(100);
  blankStockText = await readRowText('Blanca');
  await assertTrue(!!blankStockText && blankStockText.includes('5'), `[${target.name}] "Deshacer preparación" devuelve el stock de la camiseta en blanco a 5`);
  dtfStockText = await readRowText('DTF E2E receta');
  await assertTrue(!!dtfStockText && dtfStockText.includes('5'), `[${target.name}] "Deshacer preparación" devuelve el stock del DTF a 5`);

  // El checklist en sí sigue completo tras "deshacer" (solo se quita el
  // indicador "Preparado", no las casillas); basta con volver a tocar un
  // campo para que se recalcule y quede "Preparado" otra vez, dejando el
  // stock en el estado (10→7 / 5→2 / 5→2) que esperan las comprobaciones
  // siguientes (el panel del checklist ya está abierto, no hace falta
  // volver a pulsar "toggle-order-prep").
  await page.click('[data-action="tab"][data-tab="orders"]');
  await page.waitForTimeout(100);
  await page.uncheck('[data-action="prep-toggle"][data-field="camiseta"]');
  await page.waitForTimeout(100);
  await page.check('[data-action="prep-toggle"][data-field="camiseta"]');
  await page.waitForTimeout(150);
  const fulfilledAfterRecheck = await page.evaluate(() => document.body.innerText.includes('Preparado — stock descontado'));
  await assertTrue(fulfilledAfterRecheck, `[${target.name}] al volver a completar el checklist, "Preparado" se activa de nuevo`);

  // --- marcar "Enviado" (columna independiente de "Preparado", no afecta al stock) ---
  await page.click('[data-action="tab"][data-tab="orders"]');
  await page.waitForTimeout(100);
  const hasShippedCheckbox = await page.evaluate(() => !!document.querySelector('[data-action="toggle-shipped"]'));
  await assertTrue(hasShippedCheckbox, `[${target.name}] existe la casilla "Enviado" del pedido`);
  if (hasShippedCheckbox) {
    await page.check('[data-action="toggle-shipped"]');
    await page.waitForTimeout(150);
    const shippedChecked = await page.evaluate(() => {
      const cb = document.querySelector('[data-action="toggle-shipped"]');
      return !!cb && cb.checked;
    });
    await assertTrue(shippedChecked, `[${target.name}] la casilla "Enviado" queda marcada tras pulsarla`);
    await page.click('[data-action="tab"][data-tab="inventory"]');
    await page.waitForTimeout(100);
    const stockAfterShipped = await readRowText('SKU-E2E');
    await assertTrue(!!stockAfterShipped && stockAfterShipped.includes('7'), `[${target.name}] marcar "Enviado" no cambia el stock (sigue en 7)`);
    await page.click('[data-action="tab"][data-tab="orders"]');
    await page.waitForTimeout(100);
  }

  // cerrar el panel del checklist antes de continuar: el siguiente bloque
  // (etiqueta de envío / fotos de referencia) vuelve a pulsar
  // "toggle-order-prep" asumiendo que empieza cerrado.
  await page.click('[data-action="toggle-order-prep"]');
  await page.waitForTimeout(100);

  // --- adjuntar y quitar una etiqueta de envío ---
  await page.click('[data-action="tab"][data-tab="orders"]');
  await page.click('[data-action="toggle-order-items"]');
  await page.waitForTimeout(100);
  const tmpPdf = path.join(os.tmpdir(), 'e2e_label.pdf');
  fs.writeFileSync(tmpPdf, '%PDF-1.4 archivo de prueba');
  const fileInput = await page.$('input[data-action="order-label-file"]');
  await assertTrue(!!fileInput, `[${target.name}] existe el selector de archivo para la etiqueta de envío`);
  if (fileInput) {
    await fileInput.setInputFiles(tmpPdf);
    await page.waitForTimeout(250);
    const hasLink = await page.evaluate(() => !!document.querySelector('a[href^="data:"]'));
    await assertTrue(hasLink, `[${target.name}] tras adjuntar el PDF aparece el enlace "Ver..."`);

    await page.click('[data-action="order-label-remove"]');
    await page.waitForTimeout(150);
    const removed = await page.evaluate(() => document.body.innerText.includes('Sin adjuntar todavía.'));
    await assertTrue(removed, `[${target.name}] tras pulsar "Quitar" vuelve a verse "Sin adjuntar todavía."`);
  }
  fs.unlinkSync(tmpPdf);

  // --- checklist de preparación: adjuntar y quitar una foto de referencia
  // de DTF en uno de sus elementos (p. ej. "Etiqueta de cuello") ---
  await page.click('[data-action="toggle-order-prep"]');
  await page.waitForTimeout(100);
  const prepImageInput = await page.$('input[data-action="prep-image-file"][data-field="etiquetaCuello"]');
  await assertTrue(!!prepImageInput, `[${target.name}] existe el selector de foto de referencia en "Etiqueta de cuello" del checklist`);
  if (prepImageInput) {
    const tmpImg = path.join(os.tmpdir(), 'e2e_prep_img.png');
    // PNG válido de 1x1 píxel (necesario: la app intenta cargarlo como
    // imagen real antes de comprimirlo, no basta con bytes cualquiera).
    const PNG_1X1_BASE64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
    fs.writeFileSync(tmpImg, Buffer.from(PNG_1X1_BASE64, 'base64'));
    await prepImageInput.setInputFiles(tmpImg);
    await page.waitForTimeout(250);
    const hasPrepImg = await page.evaluate(() => !!document.querySelector('[data-action="prep-image-remove"][data-field="etiquetaCuello"]'));
    await assertTrue(hasPrepImg, `[${target.name}] tras adjuntar la foto aparece la miniatura y el botón "Quitar" en "Etiqueta de cuello"`);

    await page.click('[data-action="prep-image-remove"][data-field="etiquetaCuello"]');
    await page.waitForTimeout(150);
    const prepImgRemoved = await page.evaluate(() => !!document.querySelector('input[data-action="prep-image-file"][data-field="etiquetaCuello"]'));
    await assertTrue(prepImgRemoved, `[${target.name}] tras pulsar "Quitar" vuelve a verse el selector de archivo en "Etiqueta de cuello"`);
    fs.unlinkSync(tmpImg);
  }

  // --- base de imágenes de referencia DTF (Material): crear una, marcarla
  // para "Estampado", y elegirla desde el checklist de preparación del
  // pedido en vez de subir una foto suelta ---
  await page.click('[data-action="tab"][data-tab="material"]');
  await page.waitForTimeout(100);
  await page.fill('[data-action="refimg-draft-field"][data-field="name"]', 'Estampado ref E2E');

  // Las constantes de tamaño (IMAGE_ATTACH_MAX_BYTES, etc.) se cuelgan
  // explícitamente de "window.X = ..." dentro de app_logic.js (todo ese
  // archivo va envuelto en un IIFE, así que una "var" normal ahí NO
  // llegaría a "window" — se quedaría encerrada dentro de esa función y
  // no se podría sobrescribir desde aquí). Gracias a ese "window.X"
  // explícito sí se pueden sobrescribir desde la prueba, para no tener
  // que escribir de verdad archivos de 200 MB en cada ejecución — basta
  // con un archivo pequeño y un límite bajado.
  const PNG_1X1_BASE64_REF = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
  const tmpRefImg = path.join(os.tmpdir(), 'e2e_ref_img.png');
  fs.writeFileSync(tmpRefImg, Buffer.from(PNG_1X1_BASE64_REF, 'base64'));

  // Regresión: una imagen por encima del límite (ahora 200 MB) debe avisar
  // con un mensaje claro en vez de "elegirla y que no pase nada" (el fallo
  // original reportado).
  await page.evaluate(() => { window.IMAGE_ATTACH_MAX_BYTES = 10; });
  const refImgInputForOversized = await page.$('input[data-action="refimg-draft-image-file"]');
  if (refImgInputForOversized) {
    await refImgInputForOversized.setInputFiles(tmpRefImg);
    await page.waitForTimeout(200);
    const oversizedWarned = await page.evaluate(() => document.body.innerText.includes('más de 200 MB'));
    await assertTrue(oversizedWarned, `[${target.name}] una imagen por encima del límite avisa en vez de fallar en silencio`);
    const noThumbnailYet = await page.evaluate(() => !document.querySelector('button[data-action="refimg-draft-image-remove"]'));
    await assertTrue(noThumbnailYet, `[${target.name}] la imagen por encima del límite no se llega a adjuntar`);
  }
  await page.evaluate(() => { window.IMAGE_ATTACH_MAX_BYTES = 200 * 1024 * 1024; });

  // Regresión: con un archivo "grande" (según el aviso de proceso), se
  // avisa de que puede tardar, y ese aviso desaparece solo al terminar.
  await page.evaluate(() => { window.IMAGE_ATTACH_PROCESSING_NOTICE_BYTES = 10; });
  const refImgInputForNotice = await page.$('input[data-action="refimg-draft-image-file"]');
  if (refImgInputForNotice) {
    await refImgInputForNotice.setInputFiles(tmpRefImg);
    const noticeShown = await page.evaluate(() => document.body.innerText.includes('Procesando imagen grande'));
    await assertTrue(noticeShown, `[${target.name}] al procesar una imagen "grande" se avisa de que puede tardar`);
    await page.waitForTimeout(250);
    const noticeCleared = await page.evaluate(() => !document.body.innerText.includes('Procesando imagen grande'));
    await assertTrue(noticeCleared, `[${target.name}] el aviso de "procesando" desaparece al terminar`);
  }
  await page.evaluate(() => { window.IMAGE_ATTACH_PROCESSING_NOTICE_BYTES = 15 * 1024 * 1024; });

  // Regresión: si el navegador no soporta createImageBitmap (algún webview
  // antiguo), el método de reserva (FileReader + <img> + canvas) debe
  // seguir adjuntando la imagen igualmente.
  await page.evaluate(() => { window.__origCreateImageBitmap = window.createImageBitmap; window.createImageBitmap = undefined; });
  const refImgInputNoBitmap = await page.$('input[data-action="refimg-draft-image-file"]');
  if (refImgInputNoBitmap) {
    await refImgInputNoBitmap.setInputFiles(tmpRefImg);
    await page.waitForTimeout(250);
    const attachedWithoutBitmap = await page.evaluate(() => !!document.querySelector('button[data-action="refimg-draft-image-remove"]'));
    await assertTrue(attachedWithoutBitmap, `[${target.name}] sin createImageBitmap, la imagen se adjunta igualmente por el método de reserva`);
    await page.click('[data-action="refimg-draft-image-remove"]');
    await page.waitForTimeout(150);
  }
  await page.evaluate(() => { window.createImageBitmap = window.__origCreateImageBitmap; });

  const refImgInput = await page.$('input[data-action="refimg-draft-image-file"]');
  await assertTrue(!!refImgInput, `[${target.name}] existe el formulario para añadir una imagen a la base de referencia DTF`);
  if (refImgInput) {
    await refImgInput.setInputFiles(tmpRefImg);
    await page.waitForTimeout(250);
    // Regresión: la compresión debe conservar el formato PNG (no
    // convertir a JPEG), para no perder la transparencia de los diseños
    // DTF recortados con fondo transparente.
    const previewIsPng = await page.evaluate(() => {
      const img = document.querySelector('img[src^="data:"]');
      return !!img && img.src.indexOf('data:image/png') === 0;
    });
    await assertTrue(previewIsPng, `[${target.name}] la imagen adjuntada conserva el formato PNG (no se convierte a JPEG)`);
    await page.check('[data-action="refimg-draft-tag"][data-field="estampado"]');
    await page.click('[data-action="refimg-submit"]');
    await page.waitForTimeout(150);
    const refImageSaved = await page.evaluate(() => document.body.innerText.includes('Estampado ref E2E'));
    await assertTrue(refImageSaved, `[${target.name}] la imagen de referencia queda guardada en la base, dentro de Material`);

    await page.click('[data-action="tab"][data-tab="orders"]');
    await page.waitForTimeout(100);
    const hasEstampadoPicker = await page.evaluate(() => !!document.querySelector('select[data-action="prep-image-pick"][data-field="estampado"]'));
    await assertTrue(hasEstampadoPicker, `[${target.name}] al preparar el pedido, "Estampado" ofrece elegir de la base`);
    if (hasEstampadoPicker) {
      await page.selectOption('select[data-action="prep-image-pick"][data-field="estampado"]', { label: 'Estampado ref E2E' });
      await page.waitForTimeout(150);
      const estampadoApplied = await page.evaluate(() => !!document.querySelector('[data-action="prep-image-remove"][data-field="estampado"]'));
      await assertTrue(estampadoApplied, `[${target.name}] al elegir la imagen de la base, se aplica a "Estampado" sin subir un archivo nuevo`);
    }
  }
  fs.unlinkSync(tmpRefImg);

  // --- Regresión: filtrar en automático la foto de referencia según el
  // color de la camiseta del pedido, detectado a través de Inventario (a
  // petición explícita: usar Inventario para no tener que elegir a mano
  // entre la versión de un DTF para camisas blancas u oscuras). ---
  await page.click('[data-action="prep-image-remove"][data-field="estampado"]');
  await page.waitForTimeout(150);

  await page.click('[data-action="tab"][data-tab="material"]');
  await page.waitForTimeout(100);

  const tmpColorImg = path.join(os.tmpdir(), 'e2e_ref_img_color.png');
  fs.writeFileSync(tmpColorImg, Buffer.from(PNG_1X1_BASE64_REF, 'base64'));

  async function addTaggedRefImage(name, colorField) {
    await page.fill('[data-action="refimg-draft-field"][data-field="name"]', name);
    const input = await page.$('input[data-action="refimg-draft-image-file"]');
    await input.setInputFiles(tmpColorImg);
    await page.waitForTimeout(200);
    await page.check('[data-action="refimg-draft-tag"][data-field="estampado"]');
    await page.check(`[data-action="refimg-draft-tag"][data-field="${colorField}"]`);
    await page.click('[data-action="refimg-submit"]');
    await page.waitForTimeout(150);
  }
  await addTaggedRefImage('Estampado E2E blancas', 'blancas');
  await addTaggedRefImage('Estampado E2E oscuras', 'oscuras');

  await page.click('[data-action="tab"][data-tab="orders"]');
  await page.waitForTimeout(100);

  // No hay ningún formulario en la UI para poner el color de un producto
  // (normalmente llega de Shopify); se usa el ayudante expuesto para
  // pruebas (ver window.__setProductColorForTests en app_logic.js).
  await page.evaluate(() => window.__setProductColorForTests('SKU-E2E', 'Blanca'));
  await page.waitForTimeout(150);
  let optionTexts = await page.$eval('select[data-action="prep-image-pick"][data-field="estampado"]', (el) => Array.from(el.options).map((o) => o.textContent));
  await assertTrue(
    optionTexts.includes('Estampado E2E blancas') && !optionTexts.includes('Estampado E2E oscuras'),
    `[${target.name}] con el producto del pedido en "Blanca", el checklist solo ofrece la foto de referencia marcada para camisas blancas`
  );
  const colorHintBlancas = await page.evaluate(() => document.body.innerText.includes('Color de camiseta detectado en este pedido: blancas'));
  await assertTrue(colorHintBlancas, `[${target.name}] se avisa de qué color de camiseta se ha detectado automáticamente en el pedido`);

  await page.evaluate(() => window.__setProductColorForTests('SKU-E2E', 'Negra'));
  await page.waitForTimeout(150);
  optionTexts = await page.$eval('select[data-action="prep-image-pick"][data-field="estampado"]', (el) => Array.from(el.options).map((o) => o.textContent));
  await assertTrue(
    optionTexts.includes('Estampado E2E oscuras') && !optionTexts.includes('Estampado E2E blancas'),
    `[${target.name}] con el producto del pedido en "Negra", el checklist solo ofrece la foto de referencia marcada para camisas oscuras`
  );

  await page.evaluate(() => window.__setProductColorForTests('SKU-E2E', ''));
  await page.waitForTimeout(150);
  optionTexts = await page.$eval('select[data-action="prep-image-pick"][data-field="estampado"]', (el) => Array.from(el.options).map((o) => o.textContent));
  const noColorHint = await page.evaluate(() => !document.body.innerText.includes('Color de camiseta detectado en este pedido'));
  await assertTrue(
    optionTexts.includes('Estampado E2E blancas') && optionTexts.includes('Estampado E2E oscuras') && noColorHint,
    `[${target.name}] si no se puede determinar el color del producto, se ofrecen todas las fotos y no se muestra ningún color detectado`
  );

  fs.unlinkSync(tmpColorImg);

  await assertEq(consoleErrors.length, 0, `[${target.name}] sin errores de consola durante todo el flujo${consoleErrors.length ? ': ' + consoleErrors.join(' | ') : ''}`);

  await browser.close();
}

(async () => {
  for (const target of TARGETS) {
    console.log(`\n--- ${target.name} (${target.file}) ---`);
    await runOrderFlow(target);
  }
  console.log('');
  if (failures > 0) {
    console.error(`${failures} prueba(s) fallaron.`);
    process.exit(1);
  }
  console.log('Todas las pruebas de extremo a extremo pasaron.');
})();
