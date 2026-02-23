import types
import pytest
from pyfreshr import ScraperClient


class DummyResponse:
    def __init__(self, status=200, text="", json_data=None):
        self.status = status
        self._text = text
        self._json = json_data

    async def text(self):
        return self._text

    async def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyCookieJar:
    def __init__(self, cookies=None):
        # cookies should be a mapping-like object returned by filter_cookies
        self._cookies = cookies or {}

    def filter_cookies(self, url):
        return self._cookies

    def update_cookies(self, cookies):
        self._cookies.update(cookies)


class DummySession:
    def __init__(self, get_resp, post_resp, cookie_jar=None):
        self._get_resp = get_resp
        self._post_resp = post_resp
        self.cookie_jar = cookie_jar or DummyCookieJar()

    def get(self, url):
        return self._get_resp(url)
    def get(self, url, **kwargs):
        return self._get_resp(url)

    def post(self, url, json=None, headers=None, allow_redirects=True, **kwargs):
        return self._post_resp(url)


@pytest.mark.asyncio
async def test_login_sets_logged_in():
    def get_resp(url):
        return DummyResponse(status=200, text="login page")

    def post_resp(url):
        return DummyResponse(status=200, text="Welcome", json_data={"ok": True})

    cookie_jar = DummyCookieJar({"sess_token": types.SimpleNamespace(value="tokval")})
    session = DummySession(get_resp=get_resp, post_resp=post_resp, cookie_jar=cookie_jar)
    client = ScraperClient("https://example.com", session=session)
    await client.login("/login", "u", "p")
    assert client.logged_in


@pytest.mark.asyncio
async def test_scrape_parses_fields():
    # test fetch_devices returns units list
    def post_devices(url):
        return DummyResponse(status=200, json_data={"user_units": {"units": [{"id": "e:1", "active_from": "0000-00-00"}]}})

    session = DummySession(get_resp=lambda url: DummyResponse(), post_resp=post_devices)
    client = ScraperClient("https://example.com", session=session)
    # simulate logged in state
    client.logged_in = True
    client.sess_token = "tok"
    units = await client.fetch_devices()
    assert isinstance(units, list)
    assert units[0]["id"] == "e:1"


@pytest.mark.asyncio
async def test_fetch_device_current():
    serial = "e:232208/170053"

    resp_payload = {f"{serial}_current": {"t1": "19.4", "t2": "8.0", "flow": "1040", "co2": "619", "hum": "38", "dp": "9.0"}}

    def post_device(url):
        return DummyResponse(status=200, json_data=resp_payload)

    session = DummySession(get_resp=lambda url: DummyResponse(), post_resp=post_device)
    client = ScraperClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"
    data = await client.fetch_device_current(serial, convert_flow=True)
    assert data.get("t1") == "19.4"
    assert data.get("co2") == "619"
    assert isinstance(data.get("flow"), float)
    assert abs(data.get("flow") - 41.6) < 1e-6
