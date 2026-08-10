"""PTZ commands for :class:`~.api.MeariApiClient`.

Kept out of ``api.py`` so that module stays focused on auth and discovery.
These are action codes (800-899): they are delivered straight to the camera,
which therefore has to be awake, and none of them report back a position.
"""

from __future__ import annotations

import json
from typing import Any

from .const import (
    IOT_CODE_PTZ_CALIBRATION,
    IOT_CODE_PTZ_PRESET,
    IOT_CODE_PTZ_START,
    IOT_CODE_PTZ_STOP,
    IOT_CODE_PTZ2_START,
    IOT_CODE_PTZ2_STOP,
    PTZ_DIRECTIONS,
)


class PtzApiMixin:
    """PTZ verbs layered on top of the client's device-action sender."""

    def _device_action(self, sn_num: str, code: str, value: str) -> bool:
        """Send an IoT action to the device; supplied by the API client."""
        raise NotImplementedError

    def ptz_start(
        self,
        sn_num: str,
        direction: str,
        *,
        use_ptz2: bool = True,
        speed: int | None = None,
    ) -> bool:
        """Start PTZ movement in *direction* until :meth:`ptz_stop` is called.

        Uses IoT code 841 (ptz2) when *use_ptz2* is True, else 807 (ptz).
        *speed* (1-100) overrides the per-axis default magnitude.
        """
        ps, ts = PTZ_DIRECTIONS.get(direction, (0, 0))
        if speed is not None:
            magnitude = max(1, min(100, int(speed)))
            ps = magnitude if ps > 0 else -magnitude if ps < 0 else 0
            ts = magnitude if ts > 0 else -magnitude if ts < 0 else 0
        code = IOT_CODE_PTZ2_START if use_ptz2 else IOT_CODE_PTZ_START
        value_str = json.dumps({"ps": ps, "ts": ts, "zs": 0}, separators=(",", ":"))
        return self._device_action(sn_num, code, value_str)

    def ptz_stop(self, sn_num: str, *, use_ptz2: bool = True) -> bool:
        """Send a PTZ stop command."""
        code = IOT_CODE_PTZ2_STOP if use_ptz2 else IOT_CODE_PTZ_STOP
        return self._device_action(sn_num, code, "{}")

    def ptz_preset(
        self,
        sn_num: str,
        preset_id: int,
        act: str,
        name: str | None = None,
    ) -> bool:
        """Store, recall or delete a PTZ preset (IoT code 848).

        Support is firmware-dependent; several battery models ignore this code
        without reporting an error, so a True result only means "accepted".
        """
        payload: dict[str, Any] = {"id": int(preset_id), "act": act}
        if name:
            payload["name"] = str(name)
        return self._device_action(
            sn_num, IOT_CODE_PTZ_PRESET, json.dumps(payload, separators=(",", ":"))
        )

    def ptz_calibrate(self, sn_num: str) -> bool:
        """Trigger PTZ self-calibration (IoT code 847)."""
        return self._device_action(sn_num, IOT_CODE_PTZ_CALIBRATION, "{}")
