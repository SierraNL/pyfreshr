from typing import Dict, Optional, Any, List
from urllib.parse import urljoin

import aiohttp

from .models import ScrapeResult
from .exceptions import LoginError, ScrapeError
from .const import LOGIN_PAGE, LOGIN_API, DEVICES_PAGE
from .const import DEVICES_API


class ScraperClient:
    """Async client that logs in and fetches JSON endpoints using `aiohttp`.

    This client assumes requests and responses are JSON; selectors for
    `scrape` are dotted paths into the JSON object (e.g. "user.name").
    """

    def __init__(self, base_url: str, session: Optional[aiohttp.ClientSession] = None):
        self.base_url = base_url
        self._external_session = session is not None
        self.session = session or aiohttp.ClientSession()
        self.sess_token: Optional[str] = None
        self.logged_in = False

    async def close(self) -> None:
        if not self._external_session:
            await self.session.close()

    async def login(
        self,
        login_path: str,
        username: str,
        password: str,
        username_field: str = "username",
        password_field: str = "password",
        extra_fields: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        allow_redirects: bool = True,
        login_page_path: Optional[str] = None,
        api_path: Optional[str] = None,
        devices_path: Optional[str] = None,
    ) -> Any:
        """POST JSON to the login endpoint and return the parsed JSON.

        Raises `LoginError` on non-2xx responses or when the response indicates failure.
        """
        # Use constants from const.py when explicit paths aren't provided
        login_page_path = login_page_path or login_path or LOGIN_PAGE
        api_path = api_path or login_path or LOGIN_API
        devices_path = devices_path or DEVICES_PAGE

        # Step 1: GET the login page to establish any session cookies
        page_path = login_page_path
        page_url = urljoin(self.base_url, page_path)
        async with self.session.get(page_url) as _page_resp:
            page_status = _page_resp.status
            if not (200 <= page_status < 300):
                raise LoginError(f"failed to GET login page: {page_status}")

        # Step 2: POST credentials to the login API
        post_path = api_path
        url = urljoin(self.base_url, post_path)
        payload: Dict[str, str] = {username_field: username, password_field: password}
        if extra_fields:
            payload.update(extra_fields)
        async with self.session.post(url, json=payload, headers=headers or {}, allow_redirects=allow_redirects) as resp:
            status = resp.status
            text = await resp.text()
            if not (200 <= status < 300):
                raise LoginError(f"login failed: {status}")
            low = text.lower()
            if "invalid" in low or "incorrect" in low or "error" in low:
                raise LoginError("login response indicates failure")

            # Step 3: after successful login the server should set a cookie named
            # `sess_token` (often via a redirect to the devices page). Try to
            # retrieve that cookie from the session cookie jar.
            cookie_url = urljoin(self.base_url, devices_path or url)
            cookies = self.session.cookie_jar.filter_cookies(cookie_url)
            sess = cookies.get("sess_token")
            if sess is None:
                raise LoginError("login did not produce sess_token cookie")
            self.sess_token = sess.value
            # ensure cookie jar has it explicitly
            self.session.cookie_jar.update_cookies({"sess_token": self.sess_token})

            self.logged_in = True
            try:
                return await resp.json()
            except aiohttp.ContentTypeError:
                return text

    async def fetch_devices(
        self,
        tzoffset: str = "0",
        headers: Optional[Dict[str, str]] = None,
        api_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch available devices for the logged-in account.

        Posts a JSON request to `DEVICES_PAGE` (or `api_path`) including the
        `token` (taken from `self.sess_token`) and a requests block asking
        for `user_units` (the account's units/devices). Returns the list of
        device dicts (possibly empty).
        """
        if not self.logged_in:
            raise LoginError("must be logged in to fetch devices")

        api_path = api_path or DEVICES_PAGE
        url = urljoin(self.base_url, api_path)

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

        async with self.session.post(url, json=payload, headers=req_headers) as resp:
            status = resp.status
            if not (200 <= status < 300):
                raise ScrapeError(f"failed to fetch devices: {status}")
            try:
                data = await resp.json()
            except aiohttp.ContentTypeError:
                raise ScrapeError("devices response is not JSON")

        units: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            units = data.get("user_units", {}).get("units", []) or []
        return units

    async def fetch_device_current(
        self,
        serial: str,
        fields: Optional[List[str]] = None,
        tzoffset: str = "0",
        headers: Optional[Dict[str, str]] = None,
        api_path: Optional[str] = None,
        convert_flow: bool = False,
    ) -> Dict[str, Optional[Any]]:
        """Fetch current data for a single device identified by `serial`.

        The method posts a JSON request similar to the example and returns the
        inner mapping for the `<serial>_current` key. If `convert_flow` is
        True the `flow` field (if present) is converted to a float in m3 by
        multiplying the raw value by 0.04.
        """
        if not self.logged_in:
            raise LoginError("must be logged in to fetch device data")

        api_path = api_path or DEVICES_PAGE
        url = urljoin(self.base_url, api_path)

        if fields is None:
            fields = [
                "t1",
                "t2",
                "t3",
                "t4",
                "flow",
                "co2",
                "hum",
                "dp",
                "d5_25",
                "d4_25",
                "d4_03",
                "d5_03",
                "d5_1",
                "d4_1",
                "d1_25",
                "d1_03",
                "d1_1",
            ]

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

        async with self.session.post(url, json=payload, headers=req_headers) as resp:
            status = resp.status
            if not (200 <= status < 300):
                raise ScrapeError(f"failed to fetch device {serial}: {status}")
            try:
                data = await resp.json()
            except aiohttp.ContentTypeError:
                raise ScrapeError("device response is not JSON")

        key = f"{serial}_current"
        if not isinstance(data, dict) or key not in data:
            return {}

        result = data.get(key, {}) or {}
        if convert_flow and "flow" in result:
            try:
                raw = float(result.get("flow", 0))
                result["flow"] = raw * 0.04
            except (TypeError, ValueError):
                pass

        return result
