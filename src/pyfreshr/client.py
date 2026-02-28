from typing import Callable, Dict, List, Optional, Any
import asyncio
import json
from urllib.parse import urljoin, quote

import aiohttp

from .models import Device, DeviceCurrent
from .exceptions import LoginError, ScrapeError
from .const import LOGIN_PAGE, LOGIN_API, DEVICES_PAGE
from .const import DEVICES_API


class FreshrClient:
    """Async client for the Fresh-R / bw-log.com API using `aiohttp`."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        request_interval: float = 0.0,
        on_session_update: Optional[Callable[[str], None]] = None,
    ):
        """Create a client.

        Args:
            base_url: Optional prefix joined with relative paths. Full
                absolute URLs from ``const.py`` are always used as-is.
            session: Optional external ``aiohttp.ClientSession``. When
                provided the caller is responsible for closing it.
            request_interval: Minimum seconds between consecutive API fetch
                requests. Defaults to 0 (no throttling). Set to e.g. ``1.0``
                to avoid hammering the vendor's server when the HA polling
                interval is very short.
            on_session_update: Optional callback invoked with the new token
                string whenever a fresh ``sess_token`` is obtained. Use this
                to persist the token (e.g. via HA's ``Store``) so that
                ``restore_session()`` can skip the login round-trip on the
                next startup.
        """
        self.base_url = base_url or ""
        self._external_session = session is not None
        self.session = session or aiohttp.ClientSession()
        self.sess_token: Optional[str] = None
        self.logged_in = False
        # Stored so _ensure_session() can re-authenticate automatically.
        self._credentials: Optional[tuple] = None  # (username, password)
        self._login_params: dict = {}  # keyword args forwarded to login()
        self._request_interval = request_interval
        self._last_request: float = 0.0
        self._on_session_update = on_session_update

    def _make_url(self, path: Optional[str]) -> str:
        """Return an absolute URL for `path`.

        If `path` is already an absolute URL (starts with http/https), it's
        returned unchanged. Otherwise it's joined with `self.base_url`.
        """
        if not path:
            return self.base_url
        low = path.lower()
        if low.startswith("http://") or low.startswith("https://"):
            return path
        return urljoin(self.base_url, path)

    async def close(self) -> None:
        if not self._external_session:
            await self.session.close()

    def restore_session(self, token: str) -> None:
        """Restore a previously saved session token without performing a login.

        Call this on startup with a token saved from a previous session to
        avoid an unnecessary login round-trip. The client will re-authenticate
        automatically if the server rejects the restored token.
        """
        self.sess_token = token
        self.session.cookie_jar.update_cookies({"sess_token": token})
        self.logged_in = True

    async def _throttle(self) -> None:
        """Enforce the minimum interval between consecutive API requests."""
        if self._request_interval <= 0:
            return
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last_request
        if elapsed < self._request_interval:
            await asyncio.sleep(self._request_interval - elapsed)
        self._last_request = asyncio.get_running_loop().time()

    # ------------------------------------------------------------------
    # Public login entry point
    # ------------------------------------------------------------------

    async def login(
        self,
        username: str,
        password: str,
        username_field: str = "email",
        password_field: str = "password",
        headers: Optional[Dict[str, str]] = None,
        login_page_path: Optional[str] = None,
        api_path: Optional[str] = None,
        devices_path: Optional[str] = None,
        debug: bool = False,
    ) -> Any:
        """Log in and establish a session.

        Performs three steps internally: GET the login page, POST credentials,
        then exchange the one-time auth token for a ``sess_token`` cookie.
        Credentials are stored so the client can re-authenticate automatically
        when the session expires.
        """
        self._credentials = (username, password)
        self._login_params = {
            "username_field": username_field,
            "password_field": password_field,
            "headers": headers,
            "login_page_path": login_page_path,
            "api_path": api_path,
            "devices_path": devices_path,
            "debug": debug,
        }

        login_page_path = login_page_path or LOGIN_PAGE
        api_path = api_path or LOGIN_API
        devices_path = devices_path or DEVICES_PAGE

        await self._login_get(login_page_path, debug=debug)

        post_url, status, text, parsed, location = await self._login_post(
            username,
            password,
            username_field=username_field,
            password_field=password_field,
            headers=headers,
            api_path=api_path,
            debug=debug,
        )

        return await self._login_finalize(
            devices_path=devices_path,
            resp_text=text,
            resp_json=parsed,
            redirect_location=location,
            debug=debug,
        )

    # ------------------------------------------------------------------
    # Private login steps
    # ------------------------------------------------------------------

    async def _login_get(self, login_page_path: Optional[str] = None, debug: bool = False) -> None:
        """Step 1: GET the login page to establish session cookies."""
        page_url = self._make_url(login_page_path or LOGIN_PAGE)
        if debug:
            print("[debug] login GET -> url:", page_url)
        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with self.session.get(page_url, headers=_headers) as resp:
            if not (200 <= resp.status < 300):
                raise LoginError(f"failed to GET login page: {resp.status}")

    async def _login_post(
        self,
        username: str,
        password: str,
        username_field: str = "email",
        password_field: str = "password",
        headers: Optional[Dict[str, str]] = None,
        api_path: Optional[str] = None,
        debug: bool = False,
    ) -> Any:
        """Step 2: POST credentials to the login API.

        Returns ``(post_url, status, text, parsed_json_or_None, location)``.
        Redirects are never followed so the Location header is available for
        ``_login_finalize``.  Raises ``LoginError`` on non-2xx/3xx responses
        or when the response body indicates authentication failure.
        """
        url = self._make_url(api_path or LOGIN_API)
        if debug:
            print("[debug] login POST -> url:", url)
        payload: Dict[str, str] = {
            username_field: username,
            password_field: password,
            "keep_logged_in": "on",
        }
        _headers: Dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": LOGIN_PAGE,
        }
        if headers:
            _headers.update(headers)
        async with self.session.post(url, data=payload, headers=_headers, allow_redirects=False) as resp:
            status = resp.status
            location = resp.headers.get("Location")
            text = await resp.text()
            if debug:
                print("[debug] login POST -> status:", status)
                print("[debug] login POST -> location:", location)
                try:
                    print("[debug] login POST -> headers:")
                    for k, v in dict(resp.headers).items():
                        print(f"  {k}: {v}")
                except Exception:
                    print("[debug] login POST -> headers: <unavailable>")
                print("[debug] login POST -> body:")
                try:
                    print(text)
                except Exception:
                    print(repr(text))
            if not (200 <= status < 400):
                raise LoginError(f"login failed: {status}")

            # Server returns JSON as text/html; parse manually to avoid
            # ContentTypeError (same workaround used by the browser's jQuery).
            parsed = None
            try:
                parsed = await resp.json()
            except aiohttp.ContentTypeError:
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    parsed = None

            if isinstance(parsed, dict) and "authenticated" in parsed:
                if not parsed["authenticated"]:
                    raise LoginError(parsed.get("message", "login response indicates failure"))

            return url, status, text, parsed, location

    async def _login_finalize(
        self,
        devices_path: Optional[str] = None,
        resp_text: Optional[str] = None,
        resp_json: Optional[Any] = None,
        redirect_location: Optional[str] = None,
        debug: bool = False,
    ) -> Any:
        """Step 3: exchange the one-time auth token for a ``sess_token`` cookie.

        GETs the devices page URL with ``allow_redirects=False``. The PHP server
        responds with a 302 that carries ``Set-Cookie: sess_token=…``, which is
        extracted directly from the response headers (aiohttp's safe CookieJar
        would otherwise drop the cross-domain cookie).

        Raises ``LoginError`` when the response carries no usable token/redirect
        or when ``sess_token`` is absent from the Set-Cookie headers.
        """
        devices_url = redirect_location
        if devices_url is None and isinstance(resp_json, dict):
            auth_token = resp_json.get("auth_token")
            if auth_token:
                devices_url = self._make_url(devices_path or DEVICES_PAGE) + auth_token

        if devices_url is None:
            raise LoginError("login response did not contain a token or redirect")
        if debug:
            print("[debug] login finalize -> visiting devices URL:", devices_url)

        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": LOGIN_PAGE,
        }
        # allow_redirects=False lets us read Set-Cookie directly from the 302.
        async with self.session.get(devices_url, headers=_headers, allow_redirects=False) as resp:
            devices_body = await resp.text()
            if debug:
                print("[debug] login finalize -> devices status:", resp.status)
                try:
                    print("[debug] login finalize -> request headers:")
                    for k, v in resp.request_info.headers.items():
                        print(f"  {k}: {v}")
                except Exception:
                    print("[debug] login finalize -> request headers: <unavailable>")
                try:
                    print("[debug] login finalize -> response headers:")
                    for k, v in resp.headers.items():
                        print(f"  {k}: {v}")
                except Exception:
                    print("[debug] login finalize -> response headers: <unavailable>")
                print("[debug] login finalize -> response body:")
                print(devices_body)

            # Two Set-Cookie headers are typical: one that clears any old token
            # (Max-Age=0) followed by one with the real value. Process all so
            # the last valid value wins.
            sess_token_value: Optional[str] = None
            for raw_cookie in resp.headers.getall("Set-Cookie", []):
                parts = [p.strip() for p in raw_cookie.split(";")]
                directives = {p.lower() for p in parts[1:]}
                if "max-age=0" in directives:
                    continue
                if parts[0].lower().startswith("sess_token="):
                    sess_token_value = parts[0].split("=", 1)[1]

        if debug:
            print("[debug] sess_token from Set-Cookie header:", sess_token_value)

        if sess_token_value is None:
            raise LoginError("login did not produce sess_token cookie")

        self.sess_token = sess_token_value
        self.session.cookie_jar.update_cookies({"sess_token": self.sess_token})
        self.logged_in = True

        if self._on_session_update is not None:
            self._on_session_update(self.sess_token)

        if resp_json is not None:
            return resp_json
        return resp_text

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _is_session_valid(self) -> bool:
        """Return True when a sess_token cookie is present and not yet expired.

        aiohttp's cookie jar automatically drops cookies whose Max-Age/Expires
        has passed, so we simply ask it whether the cookie is still there.
        """
        if not self.logged_in or not self.sess_token:
            return False
        # The sess_token cookie is scoped to the dashboard domain (not base_url).
        cookies = self.session.cookie_jar.filter_cookies(self._make_url(DEVICES_API))
        return cookies.get("sess_token") is not None

    async def _ensure_session(self) -> None:
        """Re-authenticate if the current session is absent or expired.

        Raises ``LoginError`` when the session is invalid and no credentials
        have been stored (i.e. ``login()`` was never called on this client).
        """
        if self._is_session_valid():
            return
        if self._credentials is None:
            raise LoginError("not logged in and no credentials available for automatic re-login")
        username, password = self._credentials
        await self.login(username, password, **self._login_params)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def fetch_devices(
        self,
        tzoffset: str = "0",
        headers: Optional[Dict[str, str]] = None,
        api_path: Optional[str] = None,
    ) -> List[Device]:
        """Fetch available devices for the logged-in account.

        Sends a GET request to ``DEVICES_API`` (or ``api_path``) with the
        payload JSON-encoded as the ``q=`` query parameter. Returns the list
        of devices (possibly empty).
        """
        await self._ensure_session()

        api_path = api_path or DEVICES_API
        url = self._make_url(api_path)

        data: Any = None
        for attempt in range(2):
            await self._throttle()

            payload: Dict[str, Any] = {
                "tzoffset": tzoffset,
                "token": self.sess_token or "",
                "requests": {
                    "user_units": {"request": "syssearch", "role": "user", "fields": ["units"]}
                },
            }

            req_headers = headers.copy() if headers else {}
            if self.sess_token and "Cookie" not in req_headers:
                req_headers["Cookie"] = f"sess_token={self.sess_token}"

            reauth_needed = False
            async with self.session.get(url + quote(json.dumps(payload)), headers=req_headers) as resp:
                status = resp.status
                if status in (401, 403) and attempt == 0:
                    reauth_needed = True
                elif not (200 <= status < 300):
                    raise ScrapeError(f"failed to fetch devices: {status}")
                else:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except (ValueError, TypeError):
                        raise ScrapeError("devices response is not JSON")

            if reauth_needed:
                self.logged_in = False
                self.sess_token = None
                await self._ensure_session()
            else:
                break

        units: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            units = data.get("user_units", {}).get("units", []) or []

        return [Device.from_dict(u) for u in units]

    async def fetch_device_current(
        self,
        serial: str,
        fields: Optional[List[str]] = None,
        tzoffset: str = "0",
        headers: Optional[Dict[str, str]] = None,
        api_path: Optional[str] = None,
        convert_flow: bool = False,
    ) -> DeviceCurrent:
        """Fetch current data for a single device identified by ``serial``.

        Sends a GET request to ``DEVICES_API`` (or ``api_path``) with the
        payload JSON-encoded as the ``q=`` query parameter, and returns the
        inner mapping for the ``<serial>_current`` key. If ``convert_flow`` is
        True the ``flow`` field (if present) is converted to a float in m³ by
        multiplying by 0.04.
        """
        await self._ensure_session()

        api_path = api_path or DEVICES_API
        url = self._make_url(api_path)

        if fields is None:
            fields = [
                "t1", "t2", "t3", "t4",
                "flow", "co2", "hum", "dp",
                "d5_25", "d4_25", "d4_03", "d5_03",
                "d5_1", "d4_1", "d1_25", "d1_03", "d1_1",
            ]

        data: Any = None
        for attempt in range(2):
            await self._throttle()

            payload: Dict[str, Any] = {
                "tzoffset": tzoffset,
                "token": self.sess_token or "",
                "requests": {
                    f"{serial}_current": {"request": "fresh-r-now", "serial": serial, "fields": fields}
                },
            }

            req_headers = headers.copy() if headers else {}
            if self.sess_token and "Cookie" not in req_headers:
                req_headers["Cookie"] = f"sess_token={self.sess_token}"

            reauth_needed = False
            async with self.session.get(url + quote(json.dumps(payload)), headers=req_headers) as resp:
                status = resp.status
                if status in (401, 403) and attempt == 0:
                    reauth_needed = True
                elif not (200 <= status < 300):
                    raise ScrapeError(f"failed to fetch device {serial}: {status}")
                else:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except (ValueError, TypeError):
                        raise ScrapeError("device response is not JSON")

            if reauth_needed:
                self.logged_in = False
                self.sess_token = None
                await self._ensure_session()
            else:
                break

        key = f"{serial}_current"
        if not isinstance(data, dict) or key not in data:
            return DeviceCurrent()

        result = data.get(key, {}) or {}
        if convert_flow and "flow" in result:
            try:
                result["flow"] = float(result.get("flow", 0)) * 0.04
            except (TypeError, ValueError):
                result["flow"] = None

        return DeviceCurrent.from_dict(result)
