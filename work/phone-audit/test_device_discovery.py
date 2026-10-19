"""Mocked wireless rediscovery tests; no real ADB or phone access."""

from contextlib import nullcontext
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import device


SERIAL = "TESTPHONE123"


def completed(stdout=b"", returncode=0):
    return SimpleNamespace(stdout=stdout, returncode=returncode)


class MdnsParseTests(unittest.TestCase):
    def test_only_exact_phone_tls_connect_numeric_ipv4_is_accepted(self):
        output = (
            "List of discovered mdns services\n"
            f"adb-{SERIAL}-serviceA _adb-tls-connect._tcp 192.168.1.9:41001\n"
            f"adb-{SERIAL}-serviceB _adb-tls-pairing._tcp 192.168.1.9:41002\n"
            f"adb-{SERIAL}x-serviceC _adb-tls-connect._tcp 192.168.1.9:41003\n"
            "adb-OTHER-serviceD _adb-tls-connect._tcp 192.168.1.9:41004\n"
            f"adb-{SERIAL}-serviceE _adb-tls-connect._tcp phone.local:41005\n"
            f"adb-{SERIAL}-serviceF _adb-tls-connect._tcp 192.168.1.9:99999\n"
            f"adb-{SERIAL}-serviceG _adb-tls-connect._tcp 192.168.1.9:41001\n"
        )
        self.assertEqual(
            device._parse_mdns_services(output, SERIAL), ["192.168.1.9:41001"]
        )


class PhoneRediscoveryTests(unittest.TestCase):
    def setup_transport_mocks(self):
        fake_mutex = patch.object(
            device, "file_mutex", side_effect=lambda *args, **kwargs: nullcontext()
        )
        fake_mutex.start()
        self.addCleanup(fake_mutex.stop)

    def test_connects_new_port_then_verifies_hardware_serial(self):
        self.setup_transport_mocks()
        adb_devices_empty = completed(b"List of devices attached\n\n")
        mdns = completed(
            f"adb-{SERIAL}-instance _adb-tls-connect._tcp 192.0.2.38:37059\n".encode()
        )
        adb_devices_connected = completed(
            b"List of devices attached\n192.0.2.38:37059\tdevice\n"
        )
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[1:] == ["devices"]:
                return adb_devices_empty if len(calls) == 1 else adb_devices_connected
            if args[1:] == ["mdns", "services"]:
                return mdns
            if args[1:] == ["connect", "192.0.2.38:37059"]:
                return completed(b"connected to 192.0.2.38:37059\n")
            if args[1:] == ["-s", "192.0.2.38:37059", "shell", "getprop", "ro.serialno"]:
                return completed((SERIAL + "\n").encode())
            raise AssertionError(f"unexpected adb command: {args}")

        with tempfile.TemporaryDirectory() as folder, patch.object(
            device.subprocess, "run", side_effect=run
        ):
            adb = Path(folder) / "adb.exe"
            self.assertEqual(device.phone_transport(adb, hardware_serial=SERIAL), "192.0.2.38:37059")

        commands = [call[0][1:] for call in calls]
        self.assertIn(["connect", "192.0.2.38:37059"], commands)
        self.assertNotIn(["kill-server"], commands)
        self.assertEqual(commands.count(["mdns", "services"]), 1)

    def test_rejects_other_device_and_pairing_service_without_connecting(self):
        self.setup_transport_mocks()
        output = (
            f"adb-{SERIAL}x-instance _adb-tls-connect._tcp 192.0.2.38:37059\n"
            f"adb-{SERIAL}-pair _adb-tls-pairing._tcp 192.0.2.38:37060\n"
            "adb-OTHER-instance _adb-tls-connect._tcp 192.168.2.40:37061\n"
        ).encode()
        with tempfile.TemporaryDirectory() as folder, patch.object(
            device.subprocess,
            "run",
            side_effect=[
                completed(b"List of devices attached\n"),
                completed(output),
            ],
        ) as run:
            with self.assertRaises(device.PhoneUnavailableError):
                device.phone_transport(Path(folder) / "adb.exe", hardware_serial=SERIAL)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ["devices", "mdns"])

    def test_rejects_discovered_endpoint_when_serial_verification_fails(self):
        self.setup_transport_mocks()
        with tempfile.TemporaryDirectory() as folder, patch.object(
            device.subprocess,
            "run",
            side_effect=[
                completed(b"List of devices attached\n"),
                completed(f"adb-{SERIAL}-x _adb-tls-connect._tcp 192.0.2.38:37059\n".encode()),
                completed(b"connected\n"),
                completed(b"List of devices attached\n192.0.2.38:37059\tdevice\n"),
                completed(b"DIFFERENT_PHONE\n"),
            ],
        ):
            with self.assertRaises(device.PhoneUnavailableError):
                device.phone_transport(Path(folder) / "adb.exe", hardware_serial=SERIAL)

    def test_discovery_is_throttled_and_can_be_disabled_for_probes(self):
        self.setup_transport_mocks()
        empty = completed(b"List of devices attached\n")
        with tempfile.TemporaryDirectory() as folder, patch.object(
            device.subprocess, "run", return_value=empty
        ) as run, patch.object(device.time, "time", return_value=1000.0):
            adb = Path(folder) / "adb.exe"
            with self.assertRaises(device.PhoneUnavailableError):
                device.phone_transport(adb, hardware_serial=SERIAL)
            self.assertEqual(run.call_args_list[-1].args[0][1:3], ["mdns", "services"])

            calls_after_discovery = run.call_count
            with self.assertRaises(device.PhoneUnavailableError):
                device.phone_transport(adb, hardware_serial=SERIAL)
            self.assertEqual(run.call_count, calls_after_discovery + 1)  # devices only

            calls_before_probe = run.call_count
            with self.assertRaises(device.PhoneUnavailableError):
                device.phone_transport(adb, hardware_serial=SERIAL, reconnect=False)
            self.assertEqual(run.call_count, calls_before_probe + 1)  # devices only


if __name__ == "__main__":
    unittest.main()
