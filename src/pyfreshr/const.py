LOGIN_PAGE = "https://www.fresh-r.me/login/index.php?page=login"
LOGIN_API = "https://www.fresh-r.me/login/api/auth.php"
DEVICES_PAGE = "https://dashboard.bw-log.com/?page=devices&t="
DEVICES_API = "https://dashboard.bw-log.com/api.php?q="

# Per-device-type API request names
DEVICE_REQUEST_FRESH_R = "fresh-r-now"
DEVICE_REQUEST_FORWARD = "forward-now"
DEVICE_REQUEST_MONITOR = "monitor-now"

# Flow calibration constants (matches the JS calibrateFlow / processCurrentData)
FLOW_CALIBRATION_THRESHOLD: int = 200
FLOW_CALIBRATION_OFFSET: int = 700
FLOW_CALIBRATION_DIVISOR: int = 30
FLOW_CALIBRATION_BASE: int = 20
FORWARD_FLOW_DIVISOR: int = 3

# Default fields requested per device type
FIELDS_FRESH_R: list[str] = [
    "t1", "t2", "t3", "t4", "flow", "co2", "hum", "dp",
    "d5_25", "d4_25", "d4_03", "d5_03", "d5_1", "d4_1", "d1_25", "d1_03", "d1_1",
]
FIELDS_FORWARD: list[str] = ["date", "t1", "t2", "t3", "t4", "flow", "co2", "hum", "dp", "temp"]
FIELDS_MONITOR: list[str] = ["date", "co2", "hum", "dp", "temp", "d1_25"]
