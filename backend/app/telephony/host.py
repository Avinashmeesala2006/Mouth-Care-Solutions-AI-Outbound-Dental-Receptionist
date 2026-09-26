"""Host inspection for the telephony preflight: WSL/Asterisk installation and GSM hardware.

Everything here reads real system state (``wsl.exe`` and Windows PnP device data) and
never assumes hardware exists. Results are cached briefly because the preflight may be
polled.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

# USB vendor IDs of common cellular modem makers (Huawei, Quectel, SIMCom, ZTE, Sierra, Telit, Qualcomm).
GSM_MODEM_VENDOR_IDS = ('12D1', '2C7C', '1E0E', '19D2', '1199', '1BC7', '05C6')


@dataclass
class HostReport:
    wsl_installed: bool = False
    wsl_distros: list[str] = field(default_factory=list)
    distro_present: bool = False
    asterisk_installed: bool = False
    asterisk_version: str | None = None
    gsm_modems: list[dict] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def _run(args: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f'{type(exc).__name__}'
    raw = completed.stdout + completed.stderr
    # wsl.exe writes UTF-16LE; everything else UTF-8.
    text = raw.decode('utf-16-le', 'replace') if raw[1:2] == b'\x00' else raw.decode('utf-8', 'replace')
    return completed.returncode, text.replace('\x00', '').strip()


def inspect_wsl(distro: str) -> HostReport:
    report = HostReport()
    if sys.platform != 'win32' or not shutil.which('wsl.exe'):
        report.details.append('wsl.exe not available on this host')
        return report
    code, text = _run(['wsl.exe', '--list', '--quiet'])
    if code != 0 or 'not installed' in text.lower():
        report.details.append('WSL is not installed (run: wsl --install -d Ubuntu, then reboot)')
        return report
    report.wsl_installed = True
    report.wsl_distros = [line.strip() for line in text.splitlines() if line.strip()]
    report.distro_present = distro in report.wsl_distros
    if not report.distro_present:
        report.details.append(f'WSL distro {distro!r} not found (have: {", ".join(report.wsl_distros) or "none"})')
        return report
    code, text = _run(['wsl.exe', '-d', distro, '--', 'sh', '-c', 'command -v asterisk >/dev/null && asterisk -V'], timeout=30)
    if code == 0 and text.lower().startswith('asterisk'):
        report.asterisk_installed = True
        report.asterisk_version = text.splitlines()[0].strip()
    else:
        report.details.append(f'Asterisk is not installed in WSL distro {distro!r} (run scripts/setup_asterisk.ps1)')
    return report


def detect_gsm_modems() -> tuple[list[dict], str | None]:
    """USB cellular modems attached to the Windows host (Modem class or known modem vendors)."""
    if sys.platform != 'win32':
        return [], 'gsm hardware scan implemented for Windows hosts only'
    script = ("Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -eq 'Modem' -or "
              "$_.DeviceID -match 'VID_(" + '|'.join(GSM_MODEM_VENDOR_IDS) + ")' } | "
              "Select-Object Name,PNPClass,Status,DeviceID | ConvertTo-Json -Compress")
    code, text = _run(['powershell', '-NoProfile', '-Command', script], timeout=30)
    if code != 0:
        return [], 'hardware scan failed'
    if not text:
        return [], None
    try:
        data = json.loads(text)
    except ValueError:
        return [], 'hardware scan returned unreadable output'
    items = data if isinstance(data, list) else [data]
    return [{'name': i.get('Name'), 'class': i.get('PNPClass'), 'status': i.get('Status')} for i in items], None


class CachedHostInspector:
    def __init__(self, ttl_seconds: float = 60.0, wsl_inspector=inspect_wsl, modem_detector=detect_gsm_modems):
        self.ttl_seconds = ttl_seconds
        self._wsl_inspector = wsl_inspector
        self._modem_detector = modem_detector
        self._cache: dict[str, tuple[float, HostReport]] = {}
        self._lock = threading.Lock()

    def __call__(self, distro: str, refresh: bool = False) -> HostReport:
        with self._lock:
            cached = self._cache.get(distro)
            if cached and not refresh and time.monotonic() - cached[0] < self.ttl_seconds:
                return cached[1]
        report = self._wsl_inspector(distro)
        modems, error = self._modem_detector()
        report.gsm_modems = modems
        if error:
            report.details.append(error)
        with self._lock:
            self._cache[distro] = (time.monotonic(), report)
        return report
