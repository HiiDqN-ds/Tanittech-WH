// Service worker for the standalone "chat-only" app (/assistant/app).
//
// Scope is deliberately narrow: it only caches the static shell (this app's
// HTML/CSS/JS/icons), never API responses. /assistant/api/chat and
// /assistant/api/transcribe always go straight to the network — caching
// chat replies or audio would be meaningless (every message is unique) and
// could leak business data into the browser's cache storage.
const CACHE_NAME = 'assistant-chat-app-v2';
const SHELL_ASSETS = [
  '/assistant/app',
  '/static/css/styles.css',
  '/static/js/assistant.js',
  '/static/images/logo.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept API calls, login, or logout — those must always hit
  // the live server so auth/session state and assistant replies stay
  // correct.
  if (url.pathname.startsWith('/assistant/api/') || url.pathname.startsWith('/login') || url.pathname.startsWith('/logout')) {
    return;
  }

  if (event.request.method !== 'GET') return;

  // HTML page loads (navigations, and the shell page itself) must always
  // reflect the CURRENT server-side session — including the site language
  // set via /set-language/<lang> — so these go network-first. Only if the
  // network is unreachable (offline) do we fall back to whatever was
  // cached before.
  const isPageRequest = event.request.mode === 'navigate' ||
    event.request.destination === 'document' ||
    url.pathname === '/assistant/app';

  if (isPageRequest) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Static assets (CSS/JS/icons) rarely change and aren't session-dependent,
  // so cache-first here is safe and keeps things fast.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response && response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
