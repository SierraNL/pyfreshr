"""Example usage of `pyfreshr`.

This script demonstrates the two-step flow used by Home Assistant integrations:
1. `login()` to establish a session and retrieve the session token
2. `fetch_devices()` to list available devices for the account
3. `fetch_device_current()` to retrieve current measurements for a device

Notes:
- The client is asynchronous; this example uses `asyncio.run`.
- Run locally without installation with: `PYTHONPATH=src python examples/example_usage.py`
"""
import asyncio
import os
import argparse
import getpass
from typing import Optional

from pyfreshr import ScraperClient, Device, DeviceCurrent
from pyfreshr.const import LOGIN_PAGE, LOGIN_API, DEVICES_PAGE


async def main(username: str, password: str, debug: bool = False) -> None:
    client = ScraperClient()
    try:
        # Use constant URLs from `const.py` so full endpoints are used.
        await client.login(
            username,
            password,
            login_page_path=LOGIN_PAGE,
            api_path=LOGIN_API,
            devices_path=DEVICES_PAGE,
            debug=debug,
        )
    except Exception as exc:
        print("login failed:", exc)
        await client.close()
        return

    # Fetch devices for the account
    devices = await client.fetch_devices(tzoffset="60")
    print("Devices:")
    for d in devices:
        if isinstance(d, Device):
            print(" -", d.id, "(active_from=", d.active_from, ")")
        else:
            print(" -", d)

    # If at least one device exists, fetch its current data
    if devices:
        serial = devices[0].id if isinstance(devices[0], Device) else None
        if serial:
            data: DeviceCurrent = await client.fetch_device_current(serial, convert_flow=True)
            print(f"Current data for {serial}:")
            print(" t1:", data.t1)
            print(" co2:", data.co2)
            print(" flow:", data.flow)

    await client.close()


def _get_credentials(cli_user: Optional[str], cli_pass: Optional[str]) -> tuple[str, str]:
    user = cli_user or os.environ.get("FRESHR_USER")
    pwd = cli_pass or os.environ.get("FRESHR_PASS")
    if not user:
        user = input("FRESHR username: ")
    if not pwd:
        pwd = getpass.getpass("FRESHR password: ")
    return user, pwd


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", help="Freshr username")
    parser.add_argument("--pass", dest="password", help="Freshr password")
    parser.add_argument("--debug", action="store_true", help="Print debug HTTP responses during login")
    args = parser.parse_args()

    USER, PASS = _get_credentials(args.user, args.password)
    asyncio.run(main(USER, PASS, debug=args.debug))
    # If you want debug output for the login flow, call login with `debug=True`.
