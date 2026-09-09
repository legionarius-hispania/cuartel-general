/* Service worker mínimo: solo cachea el "cascarón" de la app (HTML, iconos,
   manifest) para que abra rápido y funcione sin conexión momentánea. Los
   datos reales (llamadas a Supabase) NUNCA se cachean aquí: para esos
   pedidos, este service worker no intercepta nada y deja pasar la
   petición directa a la red, para no servir nunca datos viejos como si
   fueran actuales. */
const CACHE_NAME = 'cuartel-general-tablet-v1';
const SHELL_FILES = [
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-180.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Solo nos ocupamos de peticiones a nuestro propio origen (el cascarón
  // de la app). Todo lo demás (Supabase, Google Fonts...) se deja pasar
  // sin tocar: ni se cachea ni se sirve desde caché.
  if (url.origin !== self.location.origin) return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match('./index.html')))
  );
});
