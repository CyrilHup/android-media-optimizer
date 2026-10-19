"""Resolve and recover the previously verified phone's ADB transport."""

import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time

from parallel_resource_locks import file_mutex
from project_config import phone_serial


class PhoneUnavailableError(RuntimeError):
    pass


_MDNS_SERVICE = "_adb-tls-connect._tcp"
_DISCOVERY_COOLDOWN_SECONDS = 2.0
_MDNS_TIMEOUT_SECONDS = 4
_CONNECT_TIMEOUT_SECONDS = 5
_VERIFY_TIMEOUT_SECONDS = 5


def _decode(output):
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output or "")


def _parse_mdns_services(output, hardware_serial):
    """Return numeric IPv4 endpoints for this phone's TLS-connect service only."""
    expected_prefix = f"adb-{hardware_serial}-"
    endpoints = []
    for line in _decode(output).splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        instance, service, address = fields[0], fields[1], fields[2]
        if service != _MDNS_SERVICE or not instance.startswith(expected_prefix):
            continue
        # The serial must be followed by a non-empty instance suffix. This
        # also rejects serial-prefix collisions such as SERIAL-extra.
        suffix = instance[len(expected_prefix):]
        if not suffix or not re.fullmatch(r"[A-Za-z0-9_-]+", suffix):
            continue
        host, separator, port_text = address.rpartition(":")
        if not separator or not port_text.isascii() or not port_text.isdecimal():
            continue
        try:
            ip = ipaddress.IPv4Address(host)
            port = int(port_text)
        except (ipaddress.AddressValueError, ValueError):
            continue
        if not 1 <= port <= 65535:
            continue
        endpoint = f"{ip}:{port}"
        if endpoint not in endpoints:
            endpoints.append(endpoint)
    return endpoints


def _recent_discovery_sidecar(adb_path):
    adb_path = Path(adb_path)
    return adb_path.parent / ".codex-adb-mdns-discovery.json"


def _write_discovery_timestamp(path, timestamp):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"last_attempt": timestamp}), encoding="utf-8")
    os.replace(temporary, path)


def _discover_endpoints(adb_path, hardware_serial):
    """Run a throttled, cross-process mDNS query without restarting ADB."""
    adb_path = Path(adb_path)
    sidecar = _recent_discovery_sidecar(adb_path)
    lock = sidecar.with_suffix(".lock")
    try:
        with file_mutex(lock, timeout=_MDNS_TIMEOUT_SECONDS + 2, poll=0.05):
            now = time.time()
            try:
                last = json.loads(sidecar.read_text(encoding="utf-8")).get("last_attempt")
                last = float(last)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                last = None
            if last is not None and 0 <= now - last < _DISCOVERY_COOLDOWN_SECONDS:
                return []
            # Record before invoking ADB, so concurrent workers do not fan out
            # repeated service discovery after a timeout or daemon error.
            _write_discovery_timestamp(sidecar, now)
            try:
                result = subprocess.run(
                    [str(adb_path), "mdns", "services"],
                    capture_output=True,
                    check=True,
                    timeout=_MDNS_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return []
            return _parse_mdns_services(result.stdout, hardware_serial)
    except (OSError, TimeoutError):
        # If the shared throttle cannot be acquired or updated, fail closed:
        # don't launch an uncoordinated discovery storm.
        return []


def _verified_transport(adb_path, transport, hardware_serial, timeout=15):
    try:
        result = subprocess.run(
            [str(adb_path), "-s", transport, "shell", "getprop", "ro.serialno"],
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return _decode(result.stdout).strip() == hardware_serial


def _connect_discovered(adb_path, endpoints, hardware_serial):
    for endpoint in endpoints:
        try:
            # connect is intentionally limited to the freshly discovered,
            # numeric endpoint. It does not restart the ADB server.
            subprocess.run(
                [str(adb_path), "connect", endpoint],
                capture_output=True,
                check=False,
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
            devices = subprocess.run(
                [str(adb_path), "devices"],
                capture_output=True,
                check=True,
                timeout=_VERIFY_TIMEOUT_SECONDS,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        for line in _decode(devices.stdout).splitlines()[1:]:
            fields = line.split()
            if len(fields) < 2 or fields[1] != "device" or fields[0] != endpoint:
                continue
            if _verified_transport(adb_path, fields[0], hardware_serial, timeout=_VERIFY_TIMEOUT_SECONDS):
                return fields[0]
    return None


def phone_transport(adb_path, hardware_serial=None, reconnect=True):
    """Return a live transport only after verifying the phone's hardware serial.

    When no attached transport belongs to this phone, optionally rediscover its
    current wireless port through ADB mDNS. No stored IP or pairing endpoint is
    ever reused. ``reconnect=False`` is useful for quick, read-only liveness
    probes that should not initiate network discovery.
    """
    hardware_serial=hardware_serial or phone_serial()
    adb_path = Path(adb_path)
    result = subprocess.run(
        [str(adb_path), "devices"], capture_output=True, check=True, timeout=15
    )
    for line in _decode(result.stdout).splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            if _verified_transport(adb_path, fields[0], hardware_serial):
                return fields[0]

    if reconnect:
        endpoints = _discover_endpoints(adb_path, hardware_serial)
        transport = _connect_discovered(adb_path, endpoints, hardware_serial)
        if transport:
            return transport
    raise PhoneUnavailableError("The previously verified Nothing phone is not connected.")


def read_phone_shell(adb_path, transport, command, timeout=180, attempts=3):
    """Retry only a caller-declared read; never replay a phone mutation."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for number in range(attempts):
        try:
            result = subprocess.run(
                [str(adb_path), "-s", transport, "shell", command],
                capture_output=True,
                check=True,
                timeout=timeout,
            )
            return _decode(result.stdout).strip(), transport
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if number + 1 == attempts:
                raise
            time.sleep(0.3 * (number + 1))
            try:
                transport = phone_transport(adb_path)
            except (RuntimeError, subprocess.SubprocessError):
                pass  # Preserve the previous transport for the bounded retry.
