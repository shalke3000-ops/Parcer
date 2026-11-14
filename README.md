# Lead Hub

A personal, single-user lead tracker for a freelance developer. It has two tabs:

- **Заказы**: read-only monitoring of Telegram chats you are already in, public subreddits and job-board RSS feeds. It looks for posts where someone wants to hire a developer, scores them by keywords, de-duplicates them and stores them in SQLite.
- **Бизнесы рядом**: finds small local businesses through OpenStreetMap (Nominatim + Overpass) and flags the ones with no website and no messenger contact on the map.

Both lists have a status (new / contacted / replied / client / rejected), notes, filters and CSV export, all behind a single password.

Design decisions (palette hex values with contrast ratios, fonts, 3D placement) are in [PLAN.md](PLAN.md).

## Desktop app (Windows)

Double-click the **Lead Hub** shortcut on the desktop. It opens a native window (Edge WebView2, no console, no browser tab) with the site and the order search running inside it. Closing the window stops everything; on the next start it catches up as described below. Your login is remembered in `data\webview`, and the log is in `data\leadhub.log`.

To recreate the shortcut, point it at `.venv\Scripts\pythonw.exe` with the arguments `-m leadhub.desktop`, the project folder as "Start in" and `leadhub\static\app.ico` as the icon. Telegram login still has to happen once beforehand: `.venv\Scripts\python -m leadhub.tg_login`.

## Fastest way to run (Windows)

Double-click **`Lead Hub.bat`**. It opens one window that runs the site *and* the order search, and opens the browser. Everything works while that window is open; closing it (or Ctrl+C) stops everything.

Each time you start it, it **catches up on what appeared while it was closed**: RSS/Reddit feeds immediately, and Telegram history for the last `TG_BACKFILL_HOURS` hours (default 24). Already-seen posts are skipped, so nothing is duplicated. Telegram notifications are still sent only for posts younger than 24 h.

Equivalent command: `.venv\Scripts\python -m leadhub.run`. Don't run it together with a separate `python -m leadhub.monitor`: two instances can't share one Telegram session.

## Quick start (separate processes)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env              # then set LEADHUB_PASSWORD and LEADHUB_SECRET_KEY
python -m leadhub                 # web UI → http://127.0.0.1:8000
python -m leadhub.monitor         # monitors, in a second terminal (Ctrl+C to stop)
```

`python -m leadhub.monitor --once` runs a single RSS/Reddit pass and exits. Use it to test your feeds and keywords.

The web app and the monitor are separate processes that share `data/leadhub.sqlite3` (WAL mode). The monitor is separate because Telethon needs an interactive login in a terminal the first time it runs.

## Environment variables

Every variable is documented in [.env.example](.env.example). Secrets are read only on the server from the environment or `.env`. They never reach the frontend: `/api/sources` returns only "configured: true/false".

| Variable | Required | Purpose |
|---|---|---|
| `LEADHUB_PASSWORD` | **yes** (≥ 10 chars) | The single login password. The app refuses to start without it. |
| `LEADHUB_SECRET_KEY` | recommended | Signs the session cookie. If unset, a random key is used and you are logged out on every restart. |
| `RSS_FEEDS` | for RSS | Comma-separated public feed URLs. Works with no keys. |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | for Reddit | A "script" app from reddit.com/prefs/apps. Runs in read-only mode. |
| `REDDIT_SUBREDDITS` | no | Default: `forhire,slavelabour,DoneDirtCheap`. |
| `TG_API_ID` / `TG_API_HASH` | for Telegram | From my.telegram.org. Used by the user client, which only listens. |
| `TG_CHATS` | no | Restricts monitoring to listed chats. They must already be in your account. |
| `TG_BOT_TOKEN` / `TG_NOTIFY_CHAT_ID` | for notifications | Pushes matches scoring ≥ `NOTIFY_MIN_SCORE` (default 8) to **your own** chat. |
| `MIN_SCORE` | no | Minimum score to store a post (default 5, max 12). |
| `LEADHUB_COOKIE_SECURE` | if HTTPS | Set to `1` behind an HTTPS reverse proxy. |

## What works now and what needs your keys

| Feature | Status |
|---|---|
| Web UI, password gate, statuses, notes, filters, CSV export | **Fully working.** Tested end to end in a browser. |
| RSS monitoring | **Fully working.** Verified live against We Work Remotely feeds. Reddit's own `.rss` endpoints work but are rate-limited (HTTP 429); use the API instead. |
| OSM business finder | **Fully working.** Verified live (Tallinn 1 km: 147 businesses; Kazan 1 km: 429). One automatic retry on Overpass 502/503/504. |
| Reddit monitoring | Code complete with unit tests on its mapping logic, **not run live**: no API keys were available at build time. Shows "не настроен" in the UI until keys are set. |
| Telegram monitoring | Code complete with unit tests on link building, **not run live**: needs your API ID/hash and a one-time phone login in the monitor terminal. Shows "не настроен" until then. |
| Bot notifications | Code complete and tested against a mocked Bot API. Disabled until `TG_BOT_TOKEN` and `TG_NOTIFY_CHAT_ID` are set. |

The UI never invents numbers. Stats, counts and source states come from the database. Empty states tell you how to start the monitor.

## Compliance: what Lead Hub will and won't do

- **Read-only.** No code path posts, replies, reacts, votes, joins a chat or marks anything as read. `tests/test_monitors.py::test_monitor_package_never_calls_send_apis` statically guards this. The only outbound message is the Bot API notification to your own chat.
- **Telegram:** listens only to groups and channels already in your account. `TG_CHATS` entries you are not a member of are logged and skipped, never joined. Uses the official MTProto client (Telethon) with your own API credentials.
- **Reddit:** official API through PRAW, created without username/password, so PRAW cannot write.
- **Not supported, on purpose:** Upwork/Fiverr/Freelancer-style bidding platforms (automation breaks their ToS) and Discord self-bots (against Discord ToS).
- **OpenStreetMap:** Nominatim calls are rate-limited to 1/s and cached per city. Requests carry an identifying User-Agent. The UI shows ODbL attribution and a disclaimer that OSM data is incomplete: "no website" may only mean nobody mapped it, so check before reaching out.

## Scoring

`keywords.json` holds three lists: `intent` ("need", "hiring", "ищу", "нужен"…), `topic` ("telegram bot", "python", "landing page", "сайт"…) and `exclude` ("[for hire]", "available for hire", "резюме", "ищу заказы"…). Matching is case-insensitive and whole-word, and it works for Cyrillic.

score = 3 × min(intent hits, 2) + 2 × min(topic hits, 3), so the maximum is 12. A post needs at least one intent hit and one topic hit. Any exclude hit drops it.

Every checked post ID goes into a `seen` table, so nothing is scored or shown twice across restarts. A post is marked seen only after it has been stored or rejected, so a crash never loses a lead. Rejected posts are not re-scored if you later change `MIN_SCORE` or `keywords.json`; the new rules apply to new posts.

The same order posted again (another chat, another feed, a repost) is stored once. Texts are compared exactly after removing links, @mentions, hashtags, leading tags like `[Hiring]`, case and punctuation; messages under 40 characters only count as repeats from the same author. Leads you have given a status or note are never removed.

Telegram push notifications are sent only for posts younger than 24 h, so the first poll of a feed's backlog doesn't flood you.

## Security

- **Session:** HMAC-signed, `HttpOnly`, `SameSite=Strict` cookie with an expiry. Password comparison is constant-time. Login is throttled to 5 failures per 5 minutes per IP.
- **Revocation:** the signing key is bound to the password. To log out every device, change `LEADHUB_PASSWORD` or rotate `LEADHUB_SECRET_KEY`.
- **Exposure warning:** the app warns at startup if it listens beyond localhost over plain HTTP.
- **Auth coverage:** auth is a router-level dependency on every `/api` route except `session`/`login`/`logout`. `test_every_protected_api_route_rejects_anonymous_requests` enumerates all routes and asserts 401.
- **CSRF:** mutating requests must send an `X-LeadHub: 1` header, which a cross-site page can't send, on top of SameSite=Strict.
- **Hint cookie:** a separate non-secret `leadhub_hint` cookie only tells the page whether to show the login form straight away. It grants nothing (this is tested).
- **CSP:** strict, with a per-request nonce and no `unsafe-inline`/`unsafe-eval`. All libraries and fonts are self-hosted.
- **XSS:** untrusted text is rendered only with `textContent`, and only `http(s)` links get an `href`.
- **CSV export:** cells starting with `= + - @ \t \r | %` are prefixed with `'` (formula injection).
- **Overpass:** only whitelisted tag filters and numeric coordinates are interpolated into the query.
- **Logs:** httpx request logging is silenced in the monitor so the bot token, which is in the Bot API URL, never reaches the logs.
- **Binding:** the web server binds to `127.0.0.1` by default. Expose it only through an HTTPS reverse proxy with `LEADHUB_COOKIE_SECURE=1`.

## Frontend

The UI is a single self-contained `leadhub/static/index.html`: vanilla JS, no build step. It's over the usual 800-line guideline because the spec asks for one file. Sections are separated by banner comments.

- **Look ("onyx & champagne"):** deep warm black `#0A0908` is the default theme (light is an opt-in toggle, remembered per browser). Champagne-gold gradient on the wordmark, key numbers and primary buttons; hairline borders, soft lift shadows, fine film grain in the header. All text pairs still pass WCAG AA (gold on black 10:1).
- **Motion (GSAP 3.13):** staggered list reveal, status-change confirmation, count-up stats, a sliding gold tab indicator. All of it is off when the switch is off, and content then appears instantly.
- **Live list:** while the window is open the page checks for new leads every 30 s. At the top of the list they appear on their own; when you have scrolled down or are editing a card, a sticky «Новых заказов: N — показать» button offers them instead, so nothing jumps under you. New *chats* are still picked up only when Lead Hub starts.
- **Animation switch:** the header button «Анимация: вкл / выкл» turns all motion on or off; the choice is remembered per browser. Motion is **on by default** even when the OS asks for reduced motion (`prefers-reduced-motion`; on Windows "Animation effects"), because the owner asked for the animated backdrop explicitly. Storing `"system"` under `leadhub-motion` in localStorage hands the decision back to the OS. CSS reads it as `:root[data-motion="full" | "reduce"]`.
- **Starfield backdrop (three.js):** a fixed full-window tunnel of 4200 mint/jade/bone stars that drifts toward you, twinkles and slowly rolls.
  - Scrolling speeds it up and moves the camera forward; the cursor steers and pushes nearby stars aside.
  - Faint white/lilac flames sit in two corners.
  - It is drawn in one pass (background quad + additive points). The original recipe's extra bloom composers only rendered empty layers, so they added nothing.
  - It pauses when the window is hidden, is hidden in the light theme, and is one still frame when the switch is off.
- **3D (three.js r180):** a faceted obsidian gem inside three gold gyroscope rings with orbiting satellites and a dust field. Lighting is PBR from a procedural "photo studio" environment, so the metal has real reflections; desktop adds subtle pointer parallax.
  - It appears on the login screen and in the page header, never behind the data lists. On phones it has its own band above the headline.
  - It's lazy-imported in `requestIdleCallback` *after* the lists render. The login scene's WebGL context is released when you log in.
  - Device pixel ratio is capped at 1.5 (1.25 on phones). It pauses when the tab is hidden or the stage is off-screen.
  - With the switch off, the model is drawn as one motionless frame.
  - It's skipped with no WebGL, Save-Data or on low-memory devices. A static SVG of the same composition stays in its place.

### Verified in a real browser (Playwright, Chromium)

- **Screenshots** at 375 / 768 / 1440 px in dark and light: no console errors, no horizontal overflow.
- **Animation switch off:** cards are visible immediately, stats are final at once, and the 3D model is one still frame with no animation loop.
- **No WebGL:** three.js is never even requested; the static graphic is shown.
- **Throttled mobile** (4× CPU, 1.6 Mbps, 150 ms latency, cold cache):
  - FCP ≈ 1.2 s
  - first lead card ≈ 2.4 s
  - three.js download starts only after the first card renders
  - CLS ≈ 0.03
- **axe-core** (WCAG 2.1 AA + best practice): 0 violations on both tabs in both themes.
- **Keyboard:** skip link, arrow-key tabs, visible gold focus rings, every control labelled. Status is shown as a text label (select) plus a shape glyph plus a colour stripe, never colour alone.

## Tests

```bash
pytest --cov=leadhub          # 75 tests, 82% line coverage
```

The tests cover scoring, storage and dedup, auth on every route, CSRF, CSV escaping, OSM parsing with mocked HTTP (including retry), RSS parsing against a mocked transport, Reddit/Telegram mapping, notifications, and the read-only guard. Live Telegram and Reddit network loops aren't unit-tested; see the status table above.

## Licenses

| Asset | License |
|---|---|
| Geist 5.3 (variable, all text) | SIL Open Font License 1.1 — `leadhub/static/fonts/OFL-Geist.txt` |
| Instrument Serif Italic (the "hub." in the wordmark) | SIL Open Font License 1.1 — `leadhub/static/fonts/OFL-InstrumentSerif.txt` |
| three.js r180 | MIT — `leadhub/static/vendor/LICENSE-three.txt` |
| GSAP 3.13 | GreenSock "Standard No Charge" license (free for commercial use; not OSI open source) |
| Map data | © OpenStreetMap contributors, ODbL (attributed in the UI footer) |

The fonts come from the Fontsource packages `@fontsource-variable/geist` (latin + cyrillic, variable woff2) and `@fontsource/instrument-serif` (latin italic) and are self-hosted.

## Layout

```
leadhub/
  config.py        env/.env settings (secrets stay server-side)
  db.py            sqlite3 storage: leads, seen, businesses, monitor_state, geocode_cache
  scoring.py       keyword scoring (keywords.json)
  osm.py           Nominatim + Overpass search, no-website flagging
  csv_export.py    injection-safe CSV
  auth.py          password gate, signed session cookie, CSRF header check
  notify.py        Bot API push to the owner
  main.py          FastAPI app + API routes
  monitor.py       `python -m leadhub.monitor` entry point
  monitors/        common pipeline + rss / reddit / telegram (all read-only)
  static/          index.html, self-hosted fonts, vendored gsap + three
tests/             pytest suite
```
