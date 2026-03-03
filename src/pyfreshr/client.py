from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from typing import Any, NamedTuple
from urllib.parse import quote, urljoin

import aiohttp

from .const import (
    DEVICE_REQUEST_FORWARD,
    DEVICE_REQUEST_FRESH_R,
    DEVICE_REQUEST_MONITOR,
    DEVICES_API,
    DEVICES_PAGE,
    FIELDS_FORWARD,
    FIELDS_FRESH_R,
    FIELDS_MONITOR,
    FLOW_CALIBRATION_BASE,
    FLOW_CALIBRATION_DIVISOR,
    FLOW_CALIBRATION_OFFSET,
    FLOW_CALIBRATION_THRESHOLD,
    FORWARD_FLOW_DIVISOR,
    LOGIN_API,
    LOGIN_PAGE,
)
from .exceptions import ApiResponseError, LoginError
from .models import DeviceReadings, DeviceSummary, DeviceType

_LOGGER = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _calibrate_flow(flow: float, device_type: DeviceType) -> float:
    """Apply vendor flow calibration (matches JS calibrateFlow + FORWARD pre-division)."""
    if device_type == DeviceType.FORWARD:
        flow = flow / FORWARD_FLOW_DIVISOR
    if flow > FLOW_CALIBRATION_THRESHOLD:
        flow = (flow - FLOW_CALIBRATION_OFFSET) / FLOW_CALIBRATION_DIVISOR + FLOW_CALIBRATION_BASE
    return round(flow * 10) / 10


def _adjusted_humidity(humidity: float, dew_point: float, target_temp: float) -> float:
    """Magnus-Tetens temperature-adjusted relative humidity.

    Matches JS calculateAdjustedHumidity.
    """
    try:
        log_rh = math.log(humidity / 100)
        a = (17.625 * dew_point) / (243.04 + dew_point)
        T_sh = 243.04 * (a - log_rh) / (17.625 + log_rh - a)
        adjusted = humidity * math.exp(
            4283.78 * (T_sh - target_temp) / (243.12 + T_sh) / (243.12 + target_temp)
        )
        result = round(adjusted * 10) / 10
        return result if not math.isnan(result) else round(humidity * 10) / 10
    except (ValueError, ZeroDivisionError, OverflowError):
        return round(humidity * 10) / 10


def _process_readings(raw: dict[str, Any], device_type: DeviceType) -> dict[str, Any]:
    """Normalise and calibrate raw API values (matches JS processCurrentData).

    Returns a new dict with calibrated float values for ``flow`` and ``hum``.
    All other fields are passed through unchanged.
    """
    result = dict(raw)

    # Flow: device-type pre-scaling then vendor calibration curve
    raw_flow = result.get("flow")
    if raw_flow is not None:
        try:
            result["flow"] = _calibrate_flow(float(raw_flow), device_type)
        except (TypeError, ValueError):
            result.pop("flow", None)

    # Humidity: temperature-adjusted RH via Magnus-Tetens formula
    raw_hum = result.get("hum")
    raw_dp = result.get("dp")
    if raw_hum is not None and raw_dp is not None:
        try:
            hum = float(raw_hum)
            dp = float(raw_dp)
            if device_type == DeviceType.FORWARD:
                target_raw = result.get("temp")
            elif device_type == DeviceType.FRESH_R:
                target_raw = result.get("t1")
            else:
                target_raw = None
            if target_raw is not None:
                result["hum"] = _adjusted_humidity(hum, dp, float(target_raw))
            else:
                result["hum"] = round(hum * 10) / 10
        except (TypeError, ValueError):
            pass

    return result


class _LoginPostResult(NamedTuple):
    url: str
    status: int
    text: str
    parsed: dict[str, Any] | None
    location: str | None


class FreshrClient:
    """Async client for the Fresh-R / bw-log.com API using `aiohttp`."""

    def __init__(
        self,
        base_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
        request_interval: float = 0.0,
        timeout: float = 10.0,
        on_session_update: Callable[[str], None] | None = None,
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
            timeout: Total timeout in seconds for each HTTP request. Defaults
                to 10 s. Raises ``asyncio.TimeoutError`` when exceeded.
            on_session_update: Optional callback invoked with the new token
                string whenever a fresh ``sess_token`` is obtained. Use this
                to persist the token (e.g. via HA's ``Store``) so that
                ``restore_session()`` can skip the login round-trip on the
                next startup.
        """
        self.base_url = base_url or ""
        self._external_session = session is not None
        self.session = session or aiohttp.ClientSession()
        self.sess_token: str | None = None
        self.logged_in = False
        # Stored so _ensure_session() can re-authenticate automatically.
        self._credentials: tuple[str, str] | None = None  # (username, password)
        self._login_params: dict[str, Any] = {}  # keyword args forwarded to login()
        self._request_interval = request_interval
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._last_request: float = 0.0
        self._on_session_update = on_session_update

    def _make_url(self, path: str | None) -> str:
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

    async def __aenter__(self) -> FreshrClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

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
        headers: dict[str, str] | None = None,
        login_page_path: str | None = None,
        api_path: str | None = None,
        devices_path: str | None = None,
    ) -> None:
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
        }

        login_page_path = login_page_path or LOGIN_PAGE
        api_path = api_path or LOGIN_API
        devices_path = devices_path or DEVICES_PAGE

        await self._login_get(login_page_path)

        post_result = await self._login_post(
            username,
            password,
            username_field=username_field,
            password_field=password_field,
            headers=headers,
            api_path=api_path,
        )

        await self._login_finalize(
            devices_path=devices_path,
            resp_text=post_result.text,
            resp_json=post_result.parsed,
            redirect_location=post_result.location,
        )

    # ------------------------------------------------------------------
    # Private login steps
    # ------------------------------------------------------------------

    async def _login_get(self, login_page_path: str | None = None) -> None:
        """Step 1: GET the login page to establish session cookies."""
        page_url = self._make_url(login_page_path or LOGIN_PAGE)
        _LOGGER.debug("login GET -> url: %s", page_url)
        _headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with self.session.get(page_url, headers=_headers, timeout=self._timeout) as resp:
            if not (200 <= resp.status < 300):
                raise LoginError(f"failed to GET login page: {resp.status}")

    async def _login_post(
        self,
        username: str,
        password: str,
        username_field: str = "email",
        password_field: str = "password",
        headers: dict[str, str] | None = None,
        api_path: str | None = None,
    ) -> _LoginPostResult:
        """Step 2: POST credentials to the login API.

        Returns a ``_LoginPostResult`` with url, status, text, parsed JSON (or None), and location.
        Redirects are never followed so the Location header is available for
        ``_login_finalize``.  Raises ``LoginError`` on non-2xx/3xx responses
        or when the response body indicates authentication failure.
        """
        url = self._make_url(api_path or LOGIN_API)
        _LOGGER.debug("login POST -> url: %s", url)
        payload: dict[str, str] = {
            username_field: username,
            password_field: password,
            "keep_logged_in": "on",
        }
        _headers: dict[str, str] = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": LOGIN_PAGE,
        }
        if headers:
            _headers.update(headers)
        async with self.session.post(
            url, data=payload, headers=_headers, allow_redirects=False, timeout=self._timeout
        ) as resp:
            status = resp.status
            location = resp.headers.get("Location")
            text = await resp.text()
            _LOGGER.debug("login POST -> status: %s, location: %s", status, location)
            if not (200 <= status < 400):
                raise LoginError(f"login failed: {status}")

            # Server returns JSON as text/html; parse manually to avoid
            # ContentTypeError (same workaround used by the browser's jQuery).
            parsed: dict[str, Any] | None = None
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

            return _LoginPostResult(url, status, text, parsed, location)

    async def _login_finalize(
        self,
        devices_path: str | None = None,
        resp_text: str | None = None,
        resp_json: dict[str, Any] | None = None,
        redirect_location: str | None = None,
    ) -> None:
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
        _LOGGER.debug("login finalize -> visiting devices URL: %s", devices_url)

        _headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": LOGIN_PAGE,
        }
        # allow_redirects=False lets us read Set-Cookie directly from the 302.
        async with self.session.get(
            devices_url, headers=_headers, allow_redirects=False, timeout=self._timeout
        ) as resp:
            await resp.text()
            _LOGGER.debug("login finalize -> devices status: %s", resp.status)

            # Two Set-Cookie headers are typical: one that clears any old token
            # (Max-Age=0) followed by one with the real value. Process all so
            # the last valid value wins.
            sess_token_value: str | None = None
            for raw_cookie in resp.headers.getall("Set-Cookie", []):
                parts = [p.strip() for p in raw_cookie.split(";")]
                directives = {p.lower() for p in parts[1:]}
                if "max-age=0" in directives:
                    continue
                if parts[0].lower().startswith("sess_token="):
                    sess_token_value = parts[0].split("=", 1)[1]

        _LOGGER.debug("login finalize -> sess_token present: %s", sess_token_value is not None)

        if sess_token_value is None:
            raise LoginError("login did not produce sess_token cookie")

        self.sess_token = sess_token_value
        self.session.cookie_jar.update_cookies({"sess_token": self.sess_token})
        self.logged_in = True
        _LOGGER.info("login successful")

        if self._on_session_update is not None:
            self._on_session_update(self.sess_token)

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
        _LOGGER.debug("session expired or missing, re-authenticating")
        username, password = self._credentials
        await self.login(username, password, **self._login_params)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def fetch_devices(
        self,
        tzoffset: str = "0",
        headers: dict[str, str] | None = None,
        api_path: str | None = None,
    ) -> list[DeviceSummary]:
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

            payload: dict[str, Any] = {
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
            async with self.session.get(
                url + quote(json.dumps(payload)), headers=req_headers, timeout=self._timeout
            ) as resp:
                status = resp.status
                _LOGGER.debug("fetch_devices -> status: %s", status)
                if status in (401, 403) and attempt == 0:
                    reauth_needed = True
                elif not (200 <= status < 300):
                    raise ApiResponseError(f"failed to fetch devices: {status}")
                else:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except (ValueError, TypeError):
                        raise ApiResponseError("devices response is not JSON")

            if reauth_needed:
                _LOGGER.warning(
                    "fetch_devices -> session rejected (status %s), re-authenticating", status
                )
                self.logged_in = False
                self.sess_token = None
                await self._ensure_session()
            else:
                break

        units: list[dict[str, Any]] = []
        if isinstance(data, dict):
            units = data.get("user_units", {}).get("units", []) or []

        return [DeviceSummary.from_dict(u) for u in units]

    async def fetch_device_current(
        self,
        device: DeviceSummary | str,
        fields: list[str] | None = None,
        tzoffset: str = "0",
        headers: dict[str, str] | None = None,
        api_path: str | None = None,
    ) -> DeviceReadings:
        """Fetch current data for a single device.

        ``device`` may be a :class:`DeviceSummary` (recommended — the correct
        API request name and default fields are derived automatically from
        :attr:`DeviceSummary.device_type`) or a plain serial string (falls back
        to the :attr:`DeviceType.FRESH_R` defaults).

        Sends a GET request to ``DEVICES_API`` (or ``api_path``) with the
        payload JSON-encoded as the ``q=`` query parameter. Returns the
        inner mapping for the ``<serial>_current`` key with flow calibrated
        and humidity temperature-adjusted per the vendor JS logic.
        """
        if isinstance(device, DeviceSummary):
            serial = device.id or ""
            device_type = device.device_type
        else:
            serial = device
            device_type = DeviceType.FRESH_R

        if device_type == DeviceType.FORWARD:
            request_name = DEVICE_REQUEST_FORWARD
            default_fields = FIELDS_FORWARD
        elif device_type == DeviceType.MONITOR:
            request_name = DEVICE_REQUEST_MONITOR
            default_fields = FIELDS_MONITOR
        else:
            request_name = DEVICE_REQUEST_FRESH_R
            default_fields = FIELDS_FRESH_R

        if fields is None:
            fields = default_fields

        await self._ensure_session()

        api_path = api_path or DEVICES_API
        url = self._make_url(api_path)

        data: Any = None
        for attempt in range(2):
            await self._throttle()

            payload: dict[str, Any] = {
                "tzoffset": tzoffset,
                "token": self.sess_token or "",
                "requests": {
                    f"{serial}_current": {
                        "request": request_name,
                        "serial": serial,
                        "fields": fields,
                    }
                },
            }

            req_headers = headers.copy() if headers else {}
            if self.sess_token and "Cookie" not in req_headers:
                req_headers["Cookie"] = f"sess_token={self.sess_token}"

            reauth_needed = False
            async with self.session.get(
                url + quote(json.dumps(payload)), headers=req_headers, timeout=self._timeout
            ) as resp:
                status = resp.status
                _LOGGER.debug("fetch_device_current(%s) -> status: %s", serial, status)
                if status in (401, 403) and attempt == 0:
                    reauth_needed = True
                elif not (200 <= status < 300):
                    raise ApiResponseError(f"failed to fetch device {serial}: {status}")
                else:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except (ValueError, TypeError):
                        raise ApiResponseError("device response is not JSON")

            if reauth_needed:
                _LOGGER.warning(
                    "fetch_device_current(%s) -> session rejected (status %s), re-authenticating",
                    serial,
                    status,
                )
                self.logged_in = False
                self.sess_token = None
                await self._ensure_session()
            else:
                break

        key = f"{serial}_current"
        if not isinstance(data, dict) or key not in data:
            return DeviceReadings()

        result = _process_readings(data.get(key, {}) or {}, device_type)
        return DeviceReadings.from_dict(result)
