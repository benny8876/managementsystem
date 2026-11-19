// Service worker that lets the whole app be installed (PWA) on PCs, tablets
// and phones. Nothing is cached — counters must always see live data — so
// page loads go to the network, with a small notice when there is no connection.
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return
  event.respondWith(
    fetch(event.request).catch(() => new Response(
      '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f1f6ef;color:#0f5f0a;text-align:center">' +
      '<div><h2>အင်တာနက် မရှိပါ</h2><p>No connection — please try again.</p></div></body>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' } },
    )),
  )
})
