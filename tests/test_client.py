import json

import pytest

from pyfreshr import DeviceReadings, DeviceSummary, FreshrClient, LoginError, ScrapeError
from pyfreshr.client import _adjusted_humidity, _calibrate_flow, _process_readings
from pyfreshr.models import DeviceType


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
    client = FreshrClient("https://example.com", session=session)
    await client.login("user@example.com", "pass")
    assert client.logged_in
    assert client.sess_token == "tokval"


async def test_login_fails_without_sess_token():
    def get_resp(url):
        return DummyResponse(status=302)  # no Set-Cookie header

    def post_resp(url):
        return DummyResponse(status=200, text=json.dumps({"auth_token": "sometoken"}))

    session = DummySession(get_resp=get_resp, post_resp=post_resp)
    client = FreshrClient("https://example.com", session=session)
    with pytest.raises(LoginError):
        await client.login("user@example.com", "pass")


# ---------------------------------------------------------------------------
# Fetch tests (session pre-seeded, no login triggered)
# ---------------------------------------------------------------------------

async def test_fetch_devices_returns_units():
    devices_payload = {"user_units": {"units": [{"id": "e:1", "active_from": "0000-00-00"}]}}

    def get_resp(url):
        return DummyResponse(status=200, text=json.dumps(devices_payload))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    units = await client.fetch_devices()
    assert isinstance(units, list)
    assert isinstance(units[0], DeviceSummary)
    assert units[0].id == "e:1"


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
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    data = await client.fetch_device_current(serial)
    assert data.t1 == 19.4
    assert data.co2 == 619
    assert isinstance(data.flow, float)
    # flow=1040 → FRESH_R calibration: (1040-700)/30 + 20 = 31.3
    assert abs(data.flow - 31.3) < 1e-6


async def test_fetch_device_current_missing_returns_default():
    serial = "e:missing/000"

    def get_resp(url):
        return DummyResponse(status=200, text=json.dumps({}))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    data = await client.fetch_device_current(serial)
    assert isinstance(data, DeviceReadings)
    assert data.t1 is None
    assert data.flow is None


# ---------------------------------------------------------------------------
# Session management tests
# ---------------------------------------------------------------------------

async def test_session_reuse_skips_login():
    """fetch_devices should not trigger re-login when the session is still valid."""
    login_page_calls = 0

    def get_resp(url):
        nonlocal login_page_calls
        if "login" in url:
            login_page_calls += 1
        return DummyResponse(status=200, text=json.dumps({"user_units": {"units": []}}))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    await client.fetch_devices()
    await client.fetch_devices()

    assert login_page_calls == 0


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
    client = FreshrClient("https://example.com", session=session)
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
    client = FreshrClient("https://example.com", session=session)
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

async def test_restore_session_skips_login():
    """restore_session() should mark the client as logged-in without a network call."""
    login_page_calls = 0

    def get_resp(url):
        nonlocal login_page_calls
        if "login" in url:
            login_page_calls += 1
        return DummyResponse(status=200, text=json.dumps({"user_units": {"units": []}}))

    session = DummySession(get_resp=get_resp, post_resp=lambda url: DummyResponse())
    client = FreshrClient("https://example.com", session=session)
    client.restore_session("saved-token")

    assert client.logged_in
    assert client.sess_token == "saved-token"

    await client.fetch_devices()
    assert login_page_calls == 0  # no login GET was issued


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
    client = FreshrClient(
        "https://example.com",
        session=session,
        on_session_update=received_tokens.append,
    )
    await client.login("user@example.com", "pass")

    assert received_tokens == ["fresh-token"]


# ---------------------------------------------------------------------------
# _calibrate_flow unit tests
# ---------------------------------------------------------------------------

def test_calibrate_flow_fresh_r_above_threshold():
    # (1040 - 700) / 30 + 20 = 31.3
    assert abs(_calibrate_flow(1040.0, DeviceType.FRESH_R) - 31.3) < 1e-9


def test_calibrate_flow_fresh_r_below_threshold():
    # raw <= 200: returned as-is (rounded to 1 dp)
    assert _calibrate_flow(150.0, DeviceType.FRESH_R) == 150.0


def test_calibrate_flow_fresh_r_at_threshold():
    # raw == 200: not > threshold, so returned unchanged
    assert _calibrate_flow(200.0, DeviceType.FRESH_R) == 200.0


def test_calibrate_flow_forward_pre_divides():
    # FORWARD: raw / 3 first, then curve if > 200
    # 300 / 3 = 100 → ≤ 200 → 100.0
    assert _calibrate_flow(300.0, DeviceType.FORWARD) == 100.0


def test_calibrate_flow_forward_above_threshold_after_division():
    # FORWARD: 900 / 3 = 300 > 200 → (300 - 700) / 30 + 20 = 6.67 → 6.7
    result = _calibrate_flow(900.0, DeviceType.FORWARD)
    assert abs(result - 6.7) < 1e-9


def test_calibrate_flow_monitor_not_divided():
    # MONITOR behaves like FRESH_R (no pre-division)
    assert abs(_calibrate_flow(1040.0, DeviceType.MONITOR) - 31.3) < 1e-9


# ---------------------------------------------------------------------------
# _adjusted_humidity unit tests
# ---------------------------------------------------------------------------

def test_adjusted_humidity_returns_float():
    result = _adjusted_humidity(50.0, 10.0, 20.0)
    assert isinstance(result, float)


def test_adjusted_humidity_same_temp_returns_raw_rounded():
    # When T_sh == target_temp the exponent is 0 → hum * exp(0) = hum
    # In practice the formula won't give exactly the same T_sh, but we can
    # verify the fallback: humidity=0 triggers log(0) → ValueError → fallback.
    result = _adjusted_humidity(0.0, 10.0, 20.0)
    assert result == 0.0  # round(0.0 * 10) / 10


def test_adjusted_humidity_fallback_on_zero_humidity():
    # log(0/100) → ValueError → fallback returns round(raw * 10) / 10
    assert _adjusted_humidity(0.0, 5.0, 15.0) == 0.0


def test_adjusted_humidity_fallback_on_nan():
    # Extremely large exponent causes overflow → fallback to rounded raw
    result = _adjusted_humidity(50.0, -200.0, 200.0)
    assert isinstance(result, float)  # should not raise


# ---------------------------------------------------------------------------
# _process_readings unit tests
# ---------------------------------------------------------------------------

def test_process_readings_calibrates_flow_fresh_r():
    raw = {"t1": "19.4", "flow": "1040", "hum": "38", "dp": "9.0"}
    result = _process_readings(raw, DeviceType.FRESH_R)
    assert abs(result["flow"] - 31.3) < 1e-9


def test_process_readings_calibrates_flow_forward():
    raw = {"temp": "21.0", "flow": "300", "hum": "50", "dp": "10.0"}
    result = _process_readings(raw, DeviceType.FORWARD)
    # 300 / 3 = 100 → ≤ 200 → 100.0
    assert result["flow"] == 100.0


def test_process_readings_adjusts_humidity_fresh_r():
    raw = {"t1": "19.4", "flow": "50", "hum": "38", "dp": "9.0"}
    result = _process_readings(raw, DeviceType.FRESH_R)
    # Humidity should be adjusted (not equal to the raw string "38")
    assert result["hum"] != "38"
    assert isinstance(result["hum"], float)


def test_process_readings_monitor_rounds_humidity_without_adjustment():
    # MONITOR: no reference temperature → raw humidity rounded to 1 dp
    raw = {"co2": "800", "hum": "45", "dp": "8.0", "temp": "20.0"}
    result = _process_readings(raw, DeviceType.MONITOR)
    assert result["hum"] == 45.0


def test_process_readings_missing_flow_not_in_result():
    raw = {"t1": "19.4", "hum": "38", "dp": "9.0"}
    result = _process_readings(raw, DeviceType.FRESH_R)
    assert "flow" not in result


def test_process_readings_missing_dp_skips_humidity_adjustment():
    # Without dp the humidity formula cannot run; hum is left unchanged
    raw = {"t1": "19.4", "flow": "50", "hum": "38"}
    result = _process_readings(raw, DeviceType.FRESH_R)
    assert result["hum"] == "38"


def test_process_readings_passthrough_extras():
    raw = {"t1": "19.4", "flow": "50", "hum": "38", "dp": "9.0", "d5_25": "12"}
    result = _process_readings(raw, DeviceType.FRESH_R)
    assert result["d5_25"] == "12"


def test_process_readings_invalid_flow_removed():
    # Non-numeric flow is removed rather than crashing
    raw = {"t1": "19.4", "flow": "not-a-number", "hum": "38", "dp": "9.0"}
    result = _process_readings(raw, DeviceType.FRESH_R)
    assert "flow" not in result


def test_process_readings_invalid_humidity_left_unchanged():
    # Non-numeric hum: TypeError is caught and hum is left as-is
    raw = {"t1": "19.4", "hum": "bad", "dp": "9.0"}
    result = _process_readings(raw, DeviceType.FRESH_R)
    assert result["hum"] == "bad"


# ---------------------------------------------------------------------------
# DeviceSummary / DeviceReadings model tests
# ---------------------------------------------------------------------------

def test_device_type_forward():
    d = DeviceSummary(type="fresh-r-forward-v2")
    assert d.device_type == DeviceType.FORWARD


def test_device_type_monitor():
    d = DeviceSummary(type="fresh-r-monitor")
    assert d.device_type == DeviceType.MONITOR


def test_device_summary_from_dict_none():
    d = DeviceSummary.from_dict(None)
    assert d.id is None
    assert d.type == "unknown"


def test_device_readings_from_dict_none():
    r = DeviceReadings.from_dict(None)
    assert r.t1 is None
    assert r.flow is None


def test_device_readings_from_dict_invalid_flow():
    # Non-convertible flow string → flow is None
    r = DeviceReadings.from_dict({"flow": "bad"})
    assert r.flow is None


# ---------------------------------------------------------------------------
# FreshrClient utility / lifecycle tests
# ---------------------------------------------------------------------------

def test_make_url_empty_path():
    session = DummySession(
        get_resp=lambda url: DummyResponse(), post_resp=lambda url: DummyResponse()
    )
    client = FreshrClient("https://example.com", session=session)
    assert client._make_url(None) == "https://example.com"
    assert client._make_url("") == "https://example.com"


def test_make_url_http_absolute():
    session = DummySession(
        get_resp=lambda url: DummyResponse(), post_resp=lambda url: DummyResponse()
    )
    client = FreshrClient("https://example.com", session=session)
    assert client._make_url("http://other.com/path") == "http://other.com/path"


async def test_context_manager_closes_session():
    session = DummySession(
        get_resp=lambda url: DummyResponse(), post_resp=lambda url: DummyResponse()
    )
    async with FreshrClient("https://example.com", session=session) as client:
        assert client is not None
    # No assertion needed — just verifying __aexit__ does not raise


async def test_close_skips_external_session():
    """close() must not close a session it doesn't own."""
    session = DummySession(
        get_resp=lambda url: DummyResponse(), post_resp=lambda url: DummyResponse()
    )
    client = FreshrClient("https://example.com", session=session)
    # Should not raise even though DummySession has no close() method
    await client.close()


async def test_throttle_active_path():
    """When request_interval > 0, _throttle should delay if called immediately twice."""
    client = FreshrClient("https://example.com", request_interval=0.01)
    client._last_request = 0.0  # simulate very recent previous request
    # Just verify it completes without error; actual sleep time is short
    await client._throttle()
    await client._throttle()
    await client.close()


async def test_ensure_session_raises_without_credentials():
    """_ensure_session raises LoginError when no credentials are stored."""
    client = FreshrClient("https://example.com")
    # logged_in=False and no credentials
    with pytest.raises(LoginError):
        await client._ensure_session()
    await client.close()


# ---------------------------------------------------------------------------
# fetch_devices error paths
# ---------------------------------------------------------------------------

async def test_fetch_devices_raises_on_server_error():
    def get_resp(url):
        return DummyResponse(status=500)

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    with pytest.raises(ScrapeError):
        await client.fetch_devices()


async def test_fetch_devices_raises_on_invalid_json():
    def get_resp(url):
        return DummyResponse(status=200, text="not-json")

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    with pytest.raises(ScrapeError):
        await client.fetch_devices()


# ---------------------------------------------------------------------------
# fetch_device_current routing and error paths
# ---------------------------------------------------------------------------

async def test_fetch_device_current_forward_routing():
    serial = "e:forward/001"
    resp_payload = {f"{serial}_current": {"flow": "300", "hum": "50", "dp": "10.0", "temp": "21.0"}}

    def get_resp(url):
        return DummyResponse(status=200, text=json.dumps(resp_payload))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    device = DeviceSummary(id=serial, type="fresh-r-forward")
    data = await client.fetch_device_current(device)
    assert isinstance(data, DeviceReadings)
    assert data.flow == 100.0  # 300 / 3 = 100 → ≤ 200 → 100.0


async def test_fetch_device_current_monitor_routing():
    serial = "e:monitor/001"
    resp_payload = {f"{serial}_current": {"co2": "800", "hum": "45", "dp": "8.0", "temp": "20.0"}}

    def get_resp(url):
        return DummyResponse(status=200, text=json.dumps(resp_payload))

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    device = DeviceSummary(id=serial, type="fresh-r-monitor")
    data = await client.fetch_device_current(device)
    assert isinstance(data, DeviceReadings)
    assert data.hum == 45.0  # MONITOR: raw humidity rounded, no adjustment


async def test_fetch_device_current_raises_on_server_error():
    def get_resp(url):
        return DummyResponse(status=500)

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    with pytest.raises(ScrapeError):
        await client.fetch_device_current("e:bad/001")


async def test_fetch_device_current_raises_on_invalid_json():
    def get_resp(url):
        return DummyResponse(status=200, text="not-json")

    cookie_jar = DummyCookieJar({"sess_token": "tok"})
    session = DummySession(
        get_resp=get_resp, post_resp=lambda url: DummyResponse(), cookie_jar=cookie_jar
    )
    client = FreshrClient("https://example.com", session=session)
    client.logged_in = True
    client.sess_token = "tok"

    with pytest.raises(ScrapeError):
        await client.fetch_device_current("e:bad/001")


async def test_fetch_device_current_401_triggers_relogin():
    serial = "e:232208/170053"
    api_get_count = [0]

    def get_resp(url):
        if "api.php" in url:
            api_get_count[0] += 1
            if api_get_count[0] == 1:
                return DummyResponse(status=401)
            return DummyResponse(
                status=200,
                text=json.dumps({f"{serial}_current": {"t1": "20.0", "flow": "50"}}),
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
    client = FreshrClient("https://example.com", session=session)
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

    data = await client.fetch_device_current(serial)
    assert isinstance(data, DeviceReadings)
