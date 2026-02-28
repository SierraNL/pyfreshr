import json
import pytest
from pyfreshr import ScraperClient, Device, DeviceCurrent
from pyfreshr.exceptions import LoginError
from pyfreshr.const import LOGIN_PAGE, DEVICES_PAGE, DEVICES_API


class FakeHeaders(dict):
    """Dict subclass that adds ``getall()`` to mimic aiohttp's multidict headers."""

    def getall(self, key, default=None):
        val = self.get(key)
        if val is None:
            return [] if default is None else default
        return val if isinstance(val, list) else [val]


class DummyResponse:
    def __init__(self, status=200, text="", headers=None):
        self.status = status
        self._text = text
        self.headers = FakeHeaders(headers or {})

    async def text(self):
        return self._text

    async def json(self):
        # Mimic the real server: always raise ContentTypeError so login_post
        # falls through to its json.loads(text) fallback.
        import aiohttp
        raise aiohttp.ContentTypeError(None, None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyCookieJar:
    def __init__(self, cookies=None):
        self._cookies = cookies or {}

    def filter_cookies(self, url):
        return self._cookies

    def update_cookies(self, cookies):
        self._cookies.update(cookies)

    def __iter__(self):
        return iter(self._cookies.values())


class DummySession:
    def __init__(self, get_resp, post_resp, cookie_jar=None):
        self._get_resp = get_resp
        self._post_resp = post_resp
        self.cookie_jar = cookie_jar or DummyCookieJar()

    def get(self, url, **kwargs):
        return self._get_resp(url)

    def post(self, url, **kwargs):
        return self._post_resp(url)


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_sets_logged_in():
    def get_resp(url):
        if "page=devices" in url:  # finalize URL: bw-log.com/?page=devices&t=…
            return DummyResponse(
                status=302,
                headers={"Set-Cookie": "sess_token=tokval; Path=/; HttpOnly"},
            )
        return DummyResponse(status=200, text="login page")

    def post_resp(url):
        return DummyResponse(status=200, text=json.dumps({"auth_token": "sometoken"}))

    session = DummySession(get_resp=get_resp, post_resp=post_resp)
    client = ScraperClient("https://example.com", session=session)
    await client.login("user@example.com", "pass")
    assert client.logged_in
    assert client.sess_token == "tokval"


@pytest.mark.asyncio
async def test_login_fails_without_sess_token():
    def get_resp(url):
        return DummyResponse(status=302)  # no Set-Cookie header

    def post_resp(url):
        return DummyResponse(status=200, text=json.dumps({"auth_token": "sometoken"}))

    session = DummySession(get_resp=get_resp, post_resp=post_resp)
    client = ScraperClient("https://example.com", session=session)
    with pytest.raises(LoginError):
        await client.login("user@example.com", "pass")


# ---------------------------------------------------------------------------
# Fetch tests (session pre-seeded, no login triggered)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_devices_returns_units():
    devices_payload = {"user_units": {"units": [{"id": "e:1", "active_from": "0000-00-00"}]}}

    def get_resp(url):
        return DummyResponse(status=200, text=json.dumps(devices_payload))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar)
    client = ScraperClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    units = await client.fetch_devices()
    assert isinstance(units, list)
    assert isinstance(units[0], Device)
    assert units[0].id == "e:1"


@pytest.mark.asyncio
async def test_fetch_device_current():
    serial = "e:232208/170053"
    resp_payload = {
        f"{serial}_current": {
            "t1": "19.4", "t2": "8.0", "flow": "1040",
            "co2": "619", "hum": "38", "dp": "9.0",
        }
    }

    def get_resp(url):
        return DummyResponse(status=200, text=json.dumps(resp_payload))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar)
    client = ScraperClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    data = await client.fetch_device_current(serial, convert_flow=True)
    assert data.t1 == "19.4"
    assert data.co2 == "619"
    assert isinstance(data.flow, float)
    assert abs(data.flow - 41.6) < 1e-6


@pytest.mark.asyncio
async def test_fetch_device_current_missing_returns_default():
    serial = "e:missing/000"

    def get_resp(url):
        return DummyResponse(status=200, text=json.dumps({}))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar)
    client = ScraperClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    data = await client.fetch_device_current(serial)
    assert isinstance(data, DeviceCurrent)
    assert data.t1 is None
    assert data.flow is None


# ---------------------------------------------------------------------------
# Session management tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_reuse_skips_login():
    """fetch_devices should not trigger re-login when the session is still valid."""
    login_page_calls = 0

    def get_resp(url):
        nonlocal login_page_calls
        if "login" in url:
            login_page_calls += 1
        return DummyResponse(status=200, text=json.dumps({"user_units": {"units": []}}))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar)
    client = ScraperClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    await client.fetch_devices()
    await client.fetch_devices()

    assert login_page_calls == 0


@pytest.mark.asyncio
async def test_expired_session_triggers_relogin():
    """When the session is absent/expired, stored credentials are used to re-login."""
    get_calls = []

    def get_resp(url):
        get_calls.append(url)
        if "page=devices" in url:
            return DummyResponse(
                status=302,
                headers={"Set-Cookie": "sess_token=newtoken; Path=/"},
            )
        if "api.php" in url:
            return DummyResponse(status=200, text=json.dumps({"user_units": {"units": []}}))
        return DummyResponse(status=200, text="login page")

    def post_resp(url):
        return DummyResponse(status=200, text=json.dumps({"auth_token": "newtoken"}))

    session = DummySession(get_resp=get_resp, post_resp=post_resp)
    client = ScraperClient("https://example.com", session=session)
    client._credentials = ("user@example.com", "pass")
    client._login_params = {
        "username_field": "email",
        "password_field": "password",
        "headers": None,
        "login_page_path": None,
        "api_path": None,
        "devices_path": None,
        "debug": False,
    }
    # logged_in=False → _is_session_valid() → False → triggers re-login

    await client.fetch_devices()

    assert any("login" in url for url in get_calls)
    assert client.logged_in


@pytest.mark.asyncio
async def test_401_triggers_relogin_and_retry():
    """A 401 from the API should cause re-login and a transparent retry."""
    api_get_count = [0]

    def get_resp(url):
        if "api.php" in url:
            api_get_count[0] += 1
            if api_get_count[0] == 1:
                return DummyResponse(status=401)
            return DummyResponse(
                status=200,
                text=json.dumps({"user_units": {"units": [{"id": "e:99"}]}}),
            )
        if "page=devices" in url:
            return DummyResponse(
                status=302,
                headers={"Set-Cookie": "sess_token=newtoken; Path=/"},
            )
        return DummyResponse(status=200, text="login page")

    def post_resp(url):
        return DummyResponse(status=200, text=json.dumps({"auth_token": "newtoken"}))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(get_resp=get_resp, post_resp=post_resp, cookie_jar=cookie_jar)
    client = ScraperClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"
    client._credentials = ("user@example.com", "pass")
    client._login_params = {
        "username_field": "email",
        "password_field": "password",
        "headers": None,
        "login_page_path": None,
        "api_path": None,
        "devices_path": None,
        "debug": False,
    }

    devices = await client.fetch_devices()
    assert len(devices) == 1
    assert devices[0].id == "e:99"


# ---------------------------------------------------------------------------
# Session persistence tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_session_skips_login():
    """restore_session() should mark the client as logged-in without a network call."""
    login_page_calls = 0

    def get_resp(url):
        nonlocal login_page_calls
        if "login" in url:
            login_page_calls += 1
        return DummyResponse(status=200, text=json.dumps({"user_units": {"units": []}}))

    session = DummySession(get_resp=get_resp, post_resp=lambda url: DummyResponse())
    client = ScraperClient("https://example.com", session=session)
    client.restore_session("saved-token")

    assert client.logged_in
    assert client.sess_token == "saved-token"

    await client.fetch_devices()
    assert login_page_calls == 0  # no login GET was issued


@pytest.mark.asyncio
async def test_on_session_update_called_after_login():
    """on_session_update callback should receive the new token after login."""
    received_tokens = []

    def get_resp(url):
        if "page=devices" in url:
            return DummyResponse(
                status=302,
                headers={"Set-Cookie": "sess_token=fresh-token; Path=/"},
            )
        return DummyResponse(status=200, text="login page")

    def post_resp(url):
        return DummyResponse(status=200, text=json.dumps({"auth_token": "sometoken"}))

    session = DummySession(get_resp=get_resp, post_resp=post_resp)
    client = ScraperClient(
        "https://example.com",
        session=session,
        on_session_update=received_tokens.append,
    )
    await client.login("user@example.com", "pass")

    assert received_tokens == ["fresh-token"]
