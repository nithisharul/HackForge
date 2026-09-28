# Frontend

`index.html` is the whole dashboard: plain HTML + JS + Chart.js (from jsDelivr), no build step.
The backend serves it at `/`. To host it elsewhere, open `index.html?api=http://backend-host:8000`.

* Live data: WebSocket `/ws/metrics` and `/ws/alerts`, auto-reconnect with backoff, and
  falls back to polling `/metrics` + `/alerts` every 3 s if WebSockets are blocked.
* History on load: `GET /alerts`, `GET /incidents`, `GET /benchmark`.
* Light/dark theme follows the OS, with a toggle.
See `docs/api-contract.md` for payloads.
