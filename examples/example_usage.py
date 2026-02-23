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
from typing import Optional

from pyfreshr import ScraperClient


async def main(username: str, password: str, base_url: str = "https://dashboard.bw-log.com") -> None:
    client = ScraperClient(base_url=base_url)
    try:
        # Adjust login path/fields for your target if needed
        await client.login("/login", username, password)
    except Exception as exc:
        print("login failed:", exc)
        await client.close()
        return

    # Fetch devices for the account
    devices = await client.fetch_devices(tzoffset="60")
    print("Devices:", devices)

    # If at least one device exists, fetch its current data
    if devices:
        serial = devices[0].get("id")
        if serial:
            data = await client.fetch_device_current(serial, convert_flow=True)
            print(f"Current data for {serial}:", data)

    await client.close()


if __name__ == "__main__":
    # Replace with real credentials when running
    import os

    USER = os.environ.get("FRESHR_USER", "myuser")
    PASS = os.environ.get("FRESHR_PASS", "mypassword")
    asyncio.run(main(USER, PASS))
