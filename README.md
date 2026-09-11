# Neo Browser Stream

> **READ THIS FIRST — LEGAL & ETHICAL NOTICE**

This project is a remote browser-streaming platform. It is intended **only for
legitimate, authorized use**, including:

- **Support and troubleshooting** of computers you own or are explicitly
  authorized to administer.
- **Remote assistance** for family, friends, or end users who have given
  informed consent and asked for help.
- **Internal IT operations** within an organization, on systems and networks
  you are authorized to manage.
- **Security research, education, and penetration testing** that is
  performed under a written scope of work against systems you own or have
  written permission to test.
- **Personal use** of the operator's own devices and accounts.

### You must NOT use this project to

- Access computers, accounts, or data you do not own or have explicit
  permission to control.
- Bypass authentication, encryption, access controls, or any other
  security mechanism without documented authorization.
- Conduct surveillance, stalking, harassment, or unauthorized monitoring
  of any person.
- Infringe copyright, scrape content in violation of a site's terms of
  service, or otherwise break the law.

In most jurisdictions, using software like this against systems you do not
own or have permission to control is a **criminal offense** (e.g. the U.S.
Computer Fraud and Abuse Act, the EU Directive 2013/40/EU, the U.K. Computer
Misuse Act, and similar laws worldwide). The maintainers of this project do
not condone illegal use and accept no liability for misuse.

**By installing, running, or modifying this software you confirm that you
understand and accept the above.**

---

## What is this?

**Neo Browser Stream** is a FastAPI service that streams a remote browser
session to an admin over WebSocket. The codebase contains three components:

| File         | Role                                                                 |
|--------------|----------------------------------------------------------------------|
| `api.py`     | FastAPI server, WebSocket endpoints, admin auth, file/session APIs.  |
| `client.html`| Page served to the *remote* browser; captures stream + telemetry.    |
| `Admin.html` | Admin console UI: live viewer, session manager, file browser, etc.   |

The admin can watch a single live client stream at a time, navigate the
remote browser, send URLs / refresh commands, and browse downloaded files
and profiles from the server.

> **Note.** The upstream version of this project shipped a Google reCAPTCHA
> step on the admin login flow. The current build disables that step
> (`verify_recaptcha` is a no-op) and the default credentials are
> `admin` / `admin`. Change the password before exposing the server to
> any untrusted network.

---

## Requirements

- **Python** 3.10+ (uses `Optional`, `match`, and modern type hints)
- **pip**
- A modern browser on the operator's machine (Chrome / Edge / Firefox)
- Outbound HTTPS for outbound links the streamed browser is asked to load

### Python dependencies

The server imports, among others:

```
fastapi
uvicorn[standard]
websockets
httpx
bcrypt
slowapi
python-multipart
```

Install them with:

```bash
pip install -r requirements.txt
```

> If you don't have a `requirements.txt`, the minimum command is:
> `pip install fastapi uvicorn websockets httpx bcrypt slowapi python-multipart`

---

## Install

```bash
git clone <your-fork-or-repo-url> neo-browser-stream
cd neo-browser-stream
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The server stores sessions, files, and profiles on disk under the working
directory. Make sure the process has read/write permission there.

---

## Run

```bash
python api.py
```

The default bind address is `127.0.0.1`. Edit the bottom of `api.py` if you
want to expose it on `0.0.0.0` (don't do that on a public network without
auth + TLS in front of it).

Open:

- `http://127.0.0.1:8000/client.html` — the *client* page (loads in the
  remote browser)
- `http://127.0.0.1:8000/admin`     — the admin console
- `http://127.0.0.1:8000/health`    — JSON health check

Log in to the admin console with `admin` / `admin` and change the password
immediately.

### Environment variables

| Variable               | Default                | Purpose                                          |
|------------------------|------------------------|--------------------------------------------------|
| `ADMIN_USERNAME`       | `admin`                | Admin login username                              |
| `ADMIN_PASSWORD_HASH`  | bcrypt hash of `admin` | bcrypt `$2b$…` hash; generate with `python -c`  |
| `JWT_SECRET_KEY`       | placeholder            | **MUST** be set to a long random secret in prod  |
| `RECAPTCHA_SECRET_KEY` | unused                 | Legacy; ignored since reCAPTCHA was removed      |

Rotate `JWT_SECRET_KEY` if you ever suspect it leaked.

---

## Test

There is no formal test suite in this repo yet. The minimum things to
verify before considering a change "done":

### 1. Static checks

```bash
python -m py_compile api.py
```

### 2. Server boots

```bash
python api.py &
sleep 2
curl -fsS http://127.0.0.1:8000/health
```

Expect a JSON response with `{"status": "ok", ...}`.

### 3. Admin login works

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}'
```

Expect `{"success": true, "token": "..."}`. A `401` means the password
hash drifted from the default — re-bake it with bcrypt.

### 4. End-to-end (manual)

1. Start the server.
2. Open `http://127.0.0.1:8000/client.html` in **one** browser window.
3. Open `http://127.0.0.1:8000/admin`, log in, in **another** window.
4. The admin console should show the client session, allow you to view
   the live stream, and let you push a URL / refresh.

If the live stream doesn't show, check the browser devtools console on
both sides for WebSocket errors against `/ws/manager` or `/ws/...`.

---

## Working on the project

### Code map

- **`api.py`** is the entry point. It's a single-file FastAPI app with
  ~5,000 lines. Key sections, in order:
  - imports + config (top)
  - lifespan / app setup
  - WebSocket manager(s) (admin subscriptions, single-stream enforcement)
  - session, profile, file APIs
  - `verify_recaptcha` (currently a no-op — see Legal note above)
  - `/api/auth/login` (JWT issuance)
  - archive / LPV (live-pushed-viewer) routes
  - `if __name__ == "__main__"` runner at the bottom

- **`client.html`** is the page loaded in the *remote* browser. It
  handles WebSocket telemetry, frame streaming, and the in-iframe
  `resizeRecaptchaLogos` visual fixup for pushed pages that embed
  Google widgets.

- **`Admin.html`** is the operator console. Big single-file SPA — search
  by function name (e.g. `connect`, `handleStream`, `loadProfiles`)
  rather than line number.

### Conventions

- **Single file per surface** — please keep `api.py` and the two HTML
  files self-contained. Don't introduce a build step unless you also
  add a CI path that runs the resulting bundle.
- **Match the existing style** — async/await, `logger = logging.getLogger(__name__)`,
  type hints where the surrounding code uses them, FastAPI `Depends`
  for auth.
- **No secrets in source.** Use env vars; never commit a real
  `JWT_SECRET_KEY` or `ADMIN_PASSWORD_HASH`.
- **No new external services without documenting them** in this README.

### Before you open a PR

1. `python -m py_compile api.py`
2. Boot the server and run the manual smoke test (login + live stream).
3. If you touched the WebSocket protocol, confirm both `client.html` and
  `Admin.html` still connect (`/ws/manager` or whichever route you
  changed).
4. If you changed auth, regenerate `ADMIN_PASSWORD_HASH` and update
  this README.

### Reporting security issues

Please don't file public issues for vulnerabilities. Contact the
maintainers privately (see your fork's `SECURITY.md` if present).

---

## License

See `LICENSE` if present in your checkout. If there is no license file,
**all rights are reserved by the original author** — treat the code as
"source-available, no granted rights" until you add a license. Adding a
license without the author's permission is itself a license violation.
