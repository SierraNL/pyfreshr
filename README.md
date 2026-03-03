# pyfreshr
Python library around the Fresh-r web interface for use in a Home Assistant integration.

## Installation

```bash
pip install pyfreshr
```

## Quick start

```python
import asyncio
from pyfreshr import FreshrClient

async def main():
    async with FreshrClient() as client:
        await client.login("user@example.com", "password")

        devices = await client.fetch_devices()
        for device in devices:
            readings = await client.fetch_device_current(device)
            print(device.id, readings.t1, readings.flow, readings.co2)

asyncio.run(main())
```

## Session persistence

The login sequence performs three HTTP round-trips. To avoid repeating this on every Home Assistant restart, save the session token after login and restore it on the next startup. The client re-authenticates automatically if the restored token has expired.

```python
from pyfreshr import FreshrClient

async def setup(hass, stored_token):
    client = FreshrClient(on_session_update=lambda token: hass.store.save(token))

    if stored_token:
        client.restore_session(stored_token)   # skip login if token is still valid
    else:
        await client.login("user@example.com", "password")

    return client
```

## Logging

The library uses Python's standard `logging` module under the logger name `pyfreshr.client`. To see debug output (HTTP requests, response statuses, session events) enable it in your application:

```python
import logging
logging.getLogger("pyfreshr.client").setLevel(logging.DEBUG)
```

In Home Assistant, add this to your `configuration.yaml`:

```yaml
logger:
  logs:
    pyfreshr.client: debug
```

## Example script

Run the example script (without installing the package) from the repository root:

```bash
PYTHONPATH=src python examples/example_usage.py
```

You can also set credentials via environment variables `FRESHR_USER` and `FRESHR_PASS` before running the script.

## Models

### `DeviceSummary`

Returned by `fetch_devices()`. Contains the device identifier and metadata.

| Field | Type | Description |
|---|---|---|
| `id` | `str \| None` | Device serial number |
| `type` | `str` | Raw type string from the API (e.g. `"fresh-r-itw"`) |
| `active_from` | `str \| None` | Activation date |
| `device_type` | `DeviceType` | Categorised type derived from `type` (property) |
| `extras` | `dict` | Any additional fields returned by the API |

### `DeviceReadings`

Returned by `fetch_device_current()`. All numeric fields are `None` when the API does not return a value for the device type. See [Value processing](#value-processing) for fields that are calibrated before being returned.

| Field | Type | Unit | Description |
|---|---|---|---|
| `t1` | `float \| None` | °C | Supply air temperature |
| `t2` | `float \| None` | °C | Extract air temperature |
| `t3` | `float \| None` | °C | Temperature sensor 3 |
| `t4` | `float \| None` | °C | Temperature sensor 4 |
| `flow` | `float \| None` | m³/h | Ventilation flow (calibrated) |
| `co2` | `int \| None` | ppm | CO₂ concentration |
| `hum` | `float \| None` | %RH | Relative humidity (temperature-adjusted) |
| `dp` | `float \| None` | °C | Dew point |
| `temp` | `float \| None` | °C | Temperature sensor (Forward and Monitor only) |
| `extras` | `dict` | — | All other fields from the API, including particle measurements (`d5_25`, `d1_25`, etc. in µg/m³) |

## Supported devices

The dashboard exposes four device types. Three are supported; the fourth (Extract) uses a separate external API that is not covered by this library.

| Device type | `DeviceType` | Supported |
|---|---|---|
| Fresh-R | `DeviceType.FRESH_R` | Yes |
| Forward | `DeviceType.FORWARD` | Yes (untested) |
| Monitor | `DeviceType.MONITOR` | Yes (untested) |
| Fresh-R Extract | — | No |

The device type is detected automatically from the `type` string returned by the API (substring match, mirroring the dashboard JS). `fetch_device_current` accepts a `DeviceSummary` directly and uses its type to select the correct API request name and default field list.

## Value processing

Raw API values are calibrated before being returned, matching the processing performed by the dashboard JavaScript (`processCurrentData`). The processed values are reflected in the `DeviceReadings` fields.

### Flow (`DeviceReadings.flow`)

Flow is calibrated through a piecewise curve. For **Forward** devices the raw sensor value is first divided by 3 before the curve is applied.

```
if raw_flow > 200:
    flow = (raw_flow − 700) / 30 + 20
else:
    flow = raw_flow

flow = round(flow, 1)  # m³/h
```

### Humidity (`DeviceReadings.hum`)

Humidity is adjusted for the supply-air temperature using the Magnus-Tetens formula. The reference temperature used depends on device type:

| Device type | Reference temperature |
|---|---|
| Fresh-R | `t1` (supply air temperature) |
| Forward | `temp` (temperature sensor) |
| Monitor | None — raw humidity is rounded to 1 dp |

```
T_sh = 243.04 × (a − ln(hum/100)) / (17.625 + ln(hum/100) − a)
       where a = 17.625 × dp / (243.04 + dp)

hum_adj = hum × exp(4283.78 × (T_sh − T_ref) / (243.12 + T_sh) / (243.12 + T_ref))
hum_adj = round(hum_adj, 1)  # %RH
```

If any input value is missing or the formula produces NaN the raw humidity (rounded to 1 dp) is returned instead.
