# Stage 1 — API Foundation Implementation Plan

> **For agentic workers:** Implement this plan task by task, in order, as `docs/implementing-plan-tasks.md` says. The plan merges through one PR for the whole branch; a task gets an early PR only when its issue says so (`AGENTS.md` rule 5). Each task lists its files, the interfaces it consumes and produces, its tests (the task's stop condition) and the checks that prove it works.

**Goal:** The FastAPI application every router plugs into: the app factory and its services, router discovery, one error format, sign-in for both run modes with Host/Origin/CSRF checks, the built UI served from the same port, the `mlops-agents` CLI, and the committed OpenAPI contract.

**Branch:** `plan/stage-1-api-foundation`. **Consumes:** merged code only. **Produces:** I3 (task 1) and I4 (task 3) of the stage overview.

**Read first:** `docs/superpowers/plans/2026-10-16-stage-1-overview.md` (global constraints, review focus, "Interfaces between plans" I3 and I4), spec sections 1 ("Access and security", "Packaging"), 6 (Routing, API) and 7, and `docs/roadmap.md` ("Run modes").

## Tasks

| Task | Title | Consumes |
|---|---|---|
| 1 | App skeleton: services, router discovery, errors, session guard, OpenAPI contract | merged code |
| 2 | One HTTP client library: GitHub moves to `httpx2` | task 1 |
| 3 | Sign-in for both run modes, Host and Origin checks | task 1 |
| 4 | Built UI serving and the CLI | task 3 |

Tasks 2 and 3 are independent of each other; do them in either order.

---

### Task 1: App skeleton

**Files:**
- Create in `backend/src/mlops_agents/api/`: `__init__.py` (empty), `app.py`, `services.py`, `deps.py`, `errors.py`, `security.py`, `routers/__init__.py` (empty), `openapi_export.py`
- Create: `frontend/openapi.json` (exported, see below)
- Modify: `.github/workflows/backend.yml` (one step: the contract check)
- Test: `backend/tests/api/__init__.py` (empty), `conftest.py`, `test_app.py`, `test_errors.py`, `test_guard.py`, `test_openapi.py`

**Interfaces:**
- Consumes: `Settings`, `RunMode`, `load_settings`, `APP_NAME` (config), `DataPaths`, `Databases`, `open_keystore`, `KeyStore`, `KeyStoreError`, `DomainError`, `error_text`, `ProvidersService`, `GitHubAuth`, `GhCli`, `GhLike`, `GitRunner`, `WorkspaceService`, `AgentCatalog`, `default_registry`, `ToolRegistry`; test helper `MemoryKeyStore` (`tests/conftest.py`).
- Produces (I3, exactly as the overview states), plus:
  - `create_app(settings: Settings, *, keystore: KeyStore | None = None, gh: GhLike | None = None) -> FastAPI`. The CLI passes the key store it opened (so a wrong master key stops startup with a plain message, task 4); tests pass a memory key store and a `gh` that is not installed. Without `keystore` the lifespan opens it. `create_app` does no I/O: everything that touches the data folder, the key store or the network happens in the lifespan.
  - `security.py`: `SessionSigner(secret: bytes)` with `issue() -> tuple[str, str]` (session cookie value, CSRF token), `check(cookie: str | None) -> str | None` (the session nonce, or None when missing, forged or older than `SESSION_DAYS = 30`), `csrf_for(nonce: str) -> str`; `load_secret(paths: DataPaths) -> bytes`; cookie names `SESSION_COOKIE = "session"`, `CSRF_COOKIE = "csrf_token"`, header `CSRF_HEADER = "X-CSRF-Token"`.

**Implementation notes:**
- Lifespan: build everything inside one `contextlib.AsyncExitStack`, so each service registers its own cleanup where it is built (`stack.push_async_callback(http.aclose)`, `stack.push_async_callback(databases.dispose)`) and shutdown undoes them in reverse order. A later plan adding a service (api-sessions' `SessionManager`, with its startup recovery and its shutdown) adds one field and one builder block, never a shutdown step elsewhere. Steps: `paths.ensure()`; `Databases(paths)`; the key store (`keystore` argument or `open_keystore(settings, paths)`); one `httpx2.AsyncClient` for providers and one `httpx.AsyncClient` for GitHub (task 2 makes them one); `GitHubAuth(databases, keystore, github_http, gh or GhCli())`; `GitRunner(paths.bin, token_provider=github.token)`; `WorkspaceService(settings, paths, databases, git, github, github_http)`; `ProvidersService(databases, keystore, provider_http)`; `tools = default_registry()`; `AgentCatalog.load(tools.names())`; `load_secret(paths)`. Store `Services` and the `SessionSigner` on `app.state`.
- `FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)`: the contract is exported, not served.
- Router discovery: iterate `pkgutil.iter_modules` over `mlops_agents.api.routers`, import each module, include its `router` with `prefix="/api"`. Sort by module name so the OpenAPI output is stable. A module without `router` is an error at startup naming the module.
- Error handlers (`errors.py`, registered by `create_app`): `DomainError` → its status and `{"code", "message", "detail"}`; `RequestValidationError` → 422, `invalid_request`, message "Some fields are missing or invalid.", `detail = {"errors": [{"field": "body.name", "message": "..."}]}` (never the submitted values: they may hold a key), where `message` is the validator's own sentence: pydantic v2 prefixes errors raised in validators with "Value error, ", which is removed; `KeyStoreError` → 503, `keystore_unavailable`, its message; Starlette `HTTPException` → its status, code `not_found` for 404 and `http_<status>` otherwise; any other exception → 500, `internal_error`, "Something went wrong on the server. The server log has the details." This last case is not an `Exception` handler: Starlette's `ServerErrorMiddleware` re-raises after calling one, and uvicorn then logs the raw traceback with the exception's text. Write it as the innermost pure ASGI middleware around the routers: it catches the exception, logs one error line with the exception's type name, the method, the path and the stack frames (`"".join(traceback.format_tb(exc.__traceback__))`: files, lines and source lines), never the exception's text (its message can hold a key that `error_text` does not mask) and without `exc_info` or `logger.exception` (either prints the text through the formatter), sends the JSON 500 and does not re-raise; when the response has already started (a stream), it only logs and ends the response.
- Session guard: one pure ASGI middleware (a class with `async def __call__(self, scope, receive, send)`), never `@app.middleware("http")` or `BaseHTTPMiddleware`, which break streaming responses and miss client disconnects; the SSE stream (I7) passes through it. Task 3's host check is written the same way. For every path under `/api/`: no valid session cookie → 401 `not_signed_in` ("Sign in again."), except `GET /api/auth/status`, `POST /api/auth/token` and `POST /api/auth/login`; for POST, PUT, PATCH and DELETE, a missing or wrong `X-CSRF-Token` → 403 `csrf_failed` ("Reload the page and try again."), except `POST /api/auth/token` and `POST /api/auth/login` (there is no session yet; the Origin check and `SameSite=Strict` protect them). `POST /api/auth/logout` needs both. Compare tokens with `hmac.compare_digest` on `.encode()`d bytes: on `str` it raises `TypeError` for non-ASCII input, which would turn a forged header into a 500. Paths outside `/api/` are not guarded (the UI must load to show the sign-in page).
- `deps.py`: `get_services(request)` returns `request.app.state.services`. `ProjectDep` reads the `project_id` path parameter: an id that does not match `DataPaths`' project-id pattern → 404 `project_not_found` (never let `DataPaths.project_dir`'s `ValueError` become a 500); an unknown id → 404 `project_not_found`, "This project no longer exists." (if `WorkspaceService.get` already raises a 404 with another code or message, keep the merged one and say so in the PR).
- `openapi_export.py`: `python -m mlops_agents.api.openapi_export` writes `create_app(load_settings(RunMode.LOCAL, env={})).openapi()` to `frontend/openapi.json` (path from the repository root, found from the module's location) as JSON with `indent=2`, `sort_keys=True`, a trailing newline, written with `newline="\n"`. `--check` compares in text mode with universal newlines (a Windows checkout may have CRLF) and exits 1 with "frontend/openapi.json is out of date. Run: uv run python -m mlops_agents.api.openapi_export" when the file differs. CI step in `backend.yml`, after pytest: `uv run python -m mlops_agents.api.openapi_export --check`.

Code for `security.py`'s signer (security-sensitive; implement as written):

```python
SESSION_DAYS = 30
SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"


def load_secret(paths: DataPaths) -> bytes:
    """32 random bytes kept in the data folder, so sign-ins survive restarts."""
    path = paths.cookie_secret
    if path.exists():
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    secret = secrets.token_bytes(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(secret.hex(), encoding="ascii")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)
    return secret


class SessionSigner:
    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def _mac(self, text: str) -> str:
        digest = hmac.new(self._secret, text.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def issue(self) -> tuple[str, str]:
        nonce = secrets.token_urlsafe(16)
        body = f"{int(time.time())}.{nonce}"
        return f"{body}.{self._mac('session:' + body)}", self.csrf_for(nonce)

    def check(self, cookie: str | None) -> str | None:
        if not cookie or cookie.count(".") != 2:
            return None
        issued, nonce, mac = cookie.split(".")
        if not hmac.compare_digest(mac.encode(), self._mac(f"session:{issued}.{nonce}").encode()):
            return None
        if not issued.isdigit() or time.time() - int(issued) > SESSION_DAYS * 86400:
            return None
        return nonce

    def csrf_for(self, nonce: str) -> str:
        return self._mac("csrf:" + nonce)
```

The `client` fixture (`tests/api/conftest.py`, I3), written as code because every router plan depends on it:

```python
@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    settings = load_settings(RunMode.LOCAL, env={"MLOPS_AGENTS_HOME": str(tmp_path / "data")})
    app = create_app(settings, keystore=MemoryKeyStore(), gh=NoGh())
    async with app.router.lifespan_context(app):
        cookie, csrf = app.state.signer.issue()
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:8765",
            cookies={SESSION_COOKIE: cookie, CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
        ) as http:
            yield http
```
`httpx2.ASGITransport` collects the whole response before returning it (checked on 2026-10-21: an endless `StreamingResponse` never yields its first chunk), so this client cannot read a stream. For streams the file also provides `live_client`: the same app served by uvicorn on a free `127.0.0.1` port inside the test (`uvicorn.Server(uvicorn.Config(app, timeout_graceful_shutdown=5)).serve(sockets=[sock])` in a task, waiting for `server.started`, stopped with `should_exit` at teardown), and an `httpx2.AsyncClient` with the same cookies and header and `base_url` set to that port. The host check (task 3) accepts it because it binds `settings.port` (pick the free port first, then build the settings with it).

`NoGh` is a `GhLike` whose `available` is False and whose `token()` raises exactly what `GhCli.token()` raises when `gh` is missing, `DomainError("gh_not_installed", ..., status=409)` with the same message; it lives in `tests/api/conftest.py` (`GhCli(path=None)` would look `gh` up on PATH). The file also provides `app` (the same app, for tests that need `app.state`), `anonymous` (a client with no cookies), `server_client` and the factory `make_client(mode, **settings_env)` that all of them use (I3): `server_client` runs server mode with a password, a master key, `base_url="https://app.example.com"` with `app.example.com` in `allowed_hosts`, and the memory key store (`https`: server-mode cookies are `Secure`, so on `http` the cookies set by login and logout would never be sent back, and the Origin check expects `https`; `ASGITransport` passes the scheme through). Task 3's Host/Origin checks accept this client's `127.0.0.1:8765` host.

**Tests (behaviors to verify):**
- `test_app.py`: the lifespan starts and stops on a temporary data folder (app.db is created, the HTTP clients are closed after shutdown); a router module added under a temporary package path is included under `/api` without editing any list (test the discovery function with a package built in `tmp_path`); a router module without `router` fails with its name in the message; `create_app` alone touches no file (call it with a data folder that does not exist and check it still does not exist).
- `test_errors.py`, with routes registered on the app inside the test: `DomainError("x", "msg", status=409, detail={"a": 1})` → 409 and exactly `{"code": "x", "message": "msg", "detail": {"a": 1}}`; a pydantic validation failure → 422 `invalid_request` with the field path and without the submitted value (post a body holding a fake key and check it is absent from the response); a validator raising `ValueError("Use https.")` comes back as the message "Use https.", without "Value error, "; `KeyStoreError("Unlock your keyring.")` → 503 `keystore_unavailable` with that message; an unknown `/api/nope` → 404 `not_found`; a `RuntimeError("secret sk-test-123")` → 500 `internal_error` whose body does not contain the text, the test client receives the response instead of the exception (keep `ASGITransport`'s default `raise_app_exceptions=True`: a re-raise would fail the test), `caplog.text` does not contain `sk-test-123`, and it contains `RuntimeError`, the request path and the name of the test route function that raised (the stack frames are logged).
- `test_guard.py`: a streaming route registered in the test sends its first chunk to `live_client` before the route finishes (the pure ASGI guard does not buffer); `anonymous` GET of a guarded route → 401 `not_signed_in`; a session cookie or `X-CSRF-Token` with non-ASCII characters → 401 or 403, never 500; a forged, expired (patch `time.time`) or malformed session cookie → 401; `client` POST without the header → 403 `csrf_failed`; with another session's token → 403; `client` GET and POST with the right token pass; `GET /api/auth/status`, `POST /api/auth/token`, `POST /api/auth/login` and `/` pass without a session; `POST /api/auth/logout` needs a session and the CSRF header; `load_secret` returns the same bytes on a second call and creates the file with mode 0600 on POSIX.
- `ProjectDep` (a route using it, registered in the test): `prj_unknown` → 404 `project_not_found`; `NOTANID` and `a..b` → 404, not 500.
- `test_openapi.py`: no component name in the export contains `__` (unique model names, I3); the committed `frontend/openapi.json` equals a fresh export (the same check CI runs); the export is byte-identical when run twice.

**Checks:** `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run python -m mlops_agents.api.openapi_export --check`.

---

### Task 2: One HTTP client library

The decision recorded in `drafts/README.md`: providers use `httpx2`, the openai SDK's client; GitHub still uses `httpx`. Move GitHub to `httpx2` so the app owns one client library and one client.

**Files:**
- Modify: `backend/src/mlops_agents/integrations/github/api.py`, `auth.py`, `backend/src/mlops_agents/workspace/service.py`, `backend/src/mlops_agents/credentials/scrub.py`, `backend/src/mlops_agents/api/app.py`, `backend/pyproject.toml`, `backend/uv.lock`
- Modify tests: `backend/tests/integrations/test_github_api.py`, `test_github_auth.py`, `backend/tests/workspace/test_github.py`, `test_local.py`, `backend/tests/credentials/test_scrub.py`

**Implementation notes:**
- Replace `httpx` with `httpx2` in every file above (`AsyncClient`, `Response`, `Headers`, `HTTPError`, `MockTransport`, `Request`; httpx2 2.13.1 has them all). Behavior and messages do not change.
- `scrub.error_text` keeps only the `httpx2.InvalidURL` case.
- The lifespan creates one `httpx2.AsyncClient` and passes it to providers, GitHub and the workspace.
- Remove `httpx` from `[project] dependencies` (`uv remove httpx`). If `uv run pyright` or the tests then find an `import httpx` left in `src/` or `tests/`, replace it; no module of ours imports `httpx` afterwards (check with `git grep -nE "^\s*(import|from) httpx\b"`, which must print nothing).

**Tests:** the existing GitHub, workspace and scrub tests pass unchanged apart from the import; `test_app.py` checks the lifespan built one client shared by `services.providers`, `services.github` and `services.workspace` (compare the objects by identity through a small test-only accessor, or assert on `app.state`).

**Checks:** as task 1, plus the `git grep` above prints nothing.

---

### Task 3: Sign-in for both run modes

**Files:**
- Create: `backend/src/mlops_agents/api/routers/auth.py`, `backend/src/mlops_agents/api/hosts.py`
- Modify: `backend/src/mlops_agents/api/app.py` (install the host middleware), `frontend/openapi.json` (re-export)
- Test: `backend/tests/api/test_auth.py`, `test_hosts.py`

**Interfaces:**
- Consumes: task 1's `SessionSigner`, cookie and header names; `Settings` (`mode`, `port`, `password`, `access_token`, `allowed_hosts`, `dev_origins`, `trusted_proxies`).
- Produces: I4 exactly. Models: `AuthStatus(mode: Literal["local", "server"], signed_in: bool)`, `TokenSignIn(token: str)`, `PasswordSignIn(password: str)`.

**Implementation notes:**
- `GET /api/auth/status`: `signed_in` is whether the request carries a valid session.
- `POST /api/auth/token` (local mode; 404 `not_available` in server mode): compares the body's token with the app's access token using `hmac.compare_digest` on encoded bytes. The access token is `settings.access_token`, or, when it is None, one generated with `secrets.token_urlsafe(32)` by `create_app` and kept on `app.state.access_token` (task 4's CLI reads it there to print the URL). Wrong token → 401 `bad_token`, "This link is out of date. Open the address printed in the terminal where the app runs."
- `POST /api/auth/login` (server mode; 404 `not_available` in local mode): at startup the password is hashed in memory with `hashlib.scrypt` and a random salt; a login hashes the attempt the same way (both in `asyncio.to_thread`: scrypt is slow on purpose) and compares the digests with `hmac.compare_digest`. The plain password is never stored anywhere, the hash only in memory. Wrong password → 401 `bad_password`, "Wrong password." Rate limit: at most 5 failed attempts per client address per 60 seconds; the 6th → 429 `too_many_attempts`, "Too many attempts. Wait a minute and try again." (in memory; the client address is `request.client.host`, or the first `X-Forwarded-For` hop when the peer is in `trusted_proxies`).
- Success (both): `signer.issue()`; set `session` (HttpOnly, `SameSite=Strict`, `Path=/`, `Max-Age` 30 days, `Secure` in server mode) and `csrf_token` (same, but readable by scripts: not HttpOnly). Return `AuthStatus`.
- `POST /api/auth/logout`: guarded like any other route (session and CSRF header, see task 1); deletes both cookies. Sessions are stateless signed cookies, so logout ends the session in that browser only; this is stated in the PR description.
- `hosts.py`, a middleware on every request (`/` and the UI included):
  - Allowed hosts: local mode `127.0.0.1:<port>` and `localhost:<port>`; server mode each entry of `allowed_hosts` (with or without a port). Compare the host case-insensitively. When the peer address is in `trusted_proxies`, the host and scheme come from `X-Forwarded-Host` and `X-Forwarded-Proto`. Any other host → 400 `bad_host`, "This address is not allowed. Open the app at the address it printed."
  - When an `Origin` header is present it must be one of the allowed hosts with the expected scheme (local `http`, server `https`), or one of `dev_origins`; otherwise 403 `bad_origin`.

**Tests (behaviors to verify):**
- Local: status says `local`, not signed in; a wrong token → 401 `bad_token` and no cookie; the right token → 200, both cookies with the flags above, and a following guarded GET passes; `login` → 404 `not_available`; a fresh app generates a token when `settings.access_token` is None and two apps generate different ones.
- Server (settings with password, master key and allowed hosts; `MemoryKeyStore`): wrong password → 401; five wrong then the right one inside 60 s → the 6th attempt is 429 even with the right password; after 60 s (patched clock) the right password works; `Secure` is set; `token` → 404.
- Logout without the CSRF header → 403; with it → both cookies cleared and the next guarded request is 401.
- No secret leaks: the password and the token appear in no response body, header (except the token-free cookies) or log record (capture logs with `caplog`).
- Hosts: `Host: evil.example` → 400; `Host: LOCALHOST:8765` passes; `Origin: http://evil.example` → 403; a dev origin from settings passes; with `trusted_proxies = ("10.0.0.1",)` a request from 10.0.0.1 with `X-Forwarded-Host: app.example.com` passes in server mode, the same header from another peer is ignored (and the request fails on its real host).

**Checks:** as task 1 (re-export `frontend/openapi.json`).

---

### Task 4: Built UI serving and the CLI

**Files:**
- Create: `backend/src/mlops_agents/api/static.py`, `backend/src/mlops_agents/cli.py`
- Modify: `backend/src/mlops_agents/api/app.py` (mount the UI last, after the routers), `frontend/vite.config.ts` (the `/api` proxy becomes `{ target: "http://127.0.0.1:8765", changeOrigin: true }`, so the backend sees its own host; the browser's `Origin` stays the Vite one, which `--dev` allows), `AGENTS.md` (Frontend commands: the dev server line says to start the backend first with `cd backend && uv run mlops-agents start --dev`)
- Test: `backend/tests/api/test_static.py`, `backend/tests/test_cli.py`

**Interfaces:**
- Consumes: task 3's `app.state.access_token`; `snapshot_all` (storage), `load_settings`, `ConfigError`, `DEFAULT_PORT`.
- Produces: the `mlops-agents` command already declared in `pyproject.toml` (`mlops_agents.cli:main`): `mlops-agents start [--server] [--port N]` and `mlops-agents backup`.

**Implementation notes:**
- `static.py`: `mount_ui(app, web_dir: Path)`. For a GET or HEAD whose path does not start with `/api`: a file that exists under `web_dir` is served (resolve the path and check it stays inside `web_dir`; otherwise 404); any other path gets `index.html`, so deep links such as `/projects/prj_x/agents` reload. When `web_dir/index.html` does not exist, every such path gets a small HTML page: "The UI is not built. Run `npm run build` in `frontend/`, or `npm run dev` for development." The web folder is `files("mlops_agents") / "web"` resolved to a path.
- `cli.py` with `argparse`:
  - `start`: `load_settings(mode, port)`; a `ConfigError` prints its message to stderr and exits 2. Bind the socket once, to `(settings.host, port)`, and hand it to uvicorn (`server.serve(sockets=[sock])`), so there is no gap between checking the port and using it; binding fails → "Port 8765 is in use. Start with --port and another number, for example --port 8766." and exit 2. Open the key store before starting anything: `keystore = open_keystore(settings, DataPaths(settings.data_dir))`; a `ConfigError` or `KeyStoreError` (a wrong `MLOPS_AGENTS_MASTER_KEY` fails `EncryptedFileStore.check()`) prints its message to stderr and exits 2, with no traceback. Build the app with `create_app(settings, keystore=keystore)`, then run `uvicorn.Server(uvicorn.Config(app, log_level="info", timeout_graceful_shutdown=5))` on that socket (the timeout keeps an open browser tab from holding a stopping server). Local mode: once the server has started (poll `server.started` from a task), print `Open http://127.0.0.1:<port>/#token=<token>` and open it (the token is in the fragment, which browsers never send to the server, so it never reaches an access log) with `webbrowser.open`. Server mode: print `Listening on <host>:<port>`; never print the password.
  - `start --no-browser`: prints the link and opens no browser (CI and headless machines; the packaging plan's install check uses it).
  - `start --dev` (local mode only): adds `http://localhost:5173` and `http://127.0.0.1:5173` to `dev_origins` (`dataclasses.replace`), and prints and opens `http://localhost:5173/#token=<token>` instead, for Vite's dev server.
  - `backup`: `load_settings(RunMode.LOCAL)` (the data folder is the same in both modes), `snapshot_all(DataPaths(settings.data_dir))`, print the backup folder.
- The token in the URL fragment is exchanged by the UI (`POST /api/auth/token`), which then removes it from the address bar (frontend-foundation plan, I4).

**Tests (behaviors to verify):**
- Static (a `web_dir` in `tmp_path` with `index.html` and `assets/app.js`): `/assets/app.js` is served with a JavaScript content type; `/projects/prj_x/agents` returns `index.html`; `/../app.db` and `/assets/../../secret.txt` never return a file outside `web_dir`; `/api/unknown` stays a JSON 404; without `index.html` the "not built" page is returned; UI paths need no session.
- CLI (call `main(argv)` directly; patch `uvicorn.Server.serve`, `webbrowser.open`): a port held by a test socket → exit 2 with the message naming the port and `--port`; `start --server` without the environment variables → exit 2 with the `ConfigError` text; local `start` prints a URL carrying the app's token after `#token=` and opens the browser once; no log record captured during start contains the token; `start --server` with the variables set opens no browser and prints no password; `start --dev` allows the two Vite origins and prints the :5173 URL; `start --no-browser` prints the link and never calls `webbrowser.open`; `start --server --dev` exits 2 ("--dev is for local mode only."); `start --server` with an existing `keys.enc` written under another master key exits 2 with the plain message and no traceback on stderr; `backup` writes `backups/<timestamp>/app.db` under `MLOPS_AGENTS_HOME`.

**Checks:** as task 1. Also run it once by hand: `uv run mlops-agents start`, open the printed URL, check `/api/auth/status` in the browser, stop with Ctrl+C; report the output in the PR.

---

## Not in this plan

- Every resource router (api-resources), the session routes (api-sessions), the sign-in pages (frontend-foundation), `scripts/build.py` and the wheel check (packaging).
