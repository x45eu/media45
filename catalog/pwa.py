"""Installable PWA metadata with no private data and no offline caching."""
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def manifest(request):
    response = JsonResponse({
        "id": "/",
        "name": "media45",
        "short_name": "media45",
        "description": "Privater Film- und Serienkatalog",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#10151c",
        "theme_color": "#19222c",
        "icons": [
            {"src": "/static/catalog/pwa-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/catalog/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/catalog/pwa-icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }, content_type="application/manifest+json")
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def service_worker(request):
    # Deliberately no fetch handler and no CacheStorage: the catalog,
    # authentication pages, metadata and cover images always require the server.
    js = '''"use strict";
// media45 PWA: installation only; no offline data or private response caching.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key.startsWith("media45-")).map((key) => caches.delete(key)));
    await self.clients.claim();
  })());
});
'''
    response = HttpResponse(js, content_type="text/javascript; charset=utf-8")
    response["Cache-Control"] = "no-store, max-age=0"
    response["Service-Worker-Allowed"] = "/"
    response["X-Content-Type-Options"] = "nosniff"
    return response
