"""PTZ services for the CloudEdge / Meari integration.

These cameras expose PTZ as fire-and-forget start/stop actions (IoT codes
807/808 or 841/842). There is no position feedback: IoT 1034 goes unanswered
on the battery models, and most of them ignore presets (848) and the built-in
patrol (822) too. So everything above a raw motor pulse is orchestrated here:

* timed moves — start, wait, stop, always stopping even if the call is cancelled;
* travel tracking — signed seconds of motor time per axis, the only way back
  to where a sweep started;
* sweeps — step across the scene and return home, firing events so an
  automation can record while it happens.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    EVENT_PTZ_SWEEP_FINISHED,
    EVENT_PTZ_SWEEP_STARTED,
    EVENT_PTZ_SWEEP_STEP,
    PTZ_DEFAULT_VARIANT,
    PTZ_DIRECTIONS,
    PTZ_MAX_MOVE_DURATION,
    PTZ_OPPOSITE,
    PTZ_SWEEP_DEFAULT_PAUSE,
    PTZ_SWEEP_DEFAULT_STEP_DURATION,
    PTZ_SWEEP_DEFAULT_STEPS,
    PTZ_SWEEP_DEFAULT_WAKE_TIMEOUT,
    PTZ_SWEEP_MAX_STEPS,
    PTZ_VARIANTS,
)
from .coordinator import CloudEdgeMeariCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_PTZ = "ptz"
SERVICE_PTZ_SWEEP = "ptz_sweep"
SERVICE_PTZ_HOME = "ptz_home"
SERVICE_PTZ_SET_HOME = "ptz_set_home"
SERVICE_PTZ_PRESET = "ptz_preset"
SERVICE_PTZ_CALIBRATE = "ptz_calibrate"

PTZ_LOCKS = "ptz_locks"

_SPEED = vol.All(vol.Coerce(int), vol.Range(min=1, max=100))
_VARIANT = vol.In(list(PTZ_VARIANTS))
_DURATION = vol.All(vol.Coerce(float), vol.Range(min=0.1, max=PTZ_MAX_MOVE_DURATION))

SERVICE_PTZ_SCHEMA = vol.Schema(
    {
        vol.Required("action"): vol.In(["move", "stop"]),
        vol.Optional("argument"): vol.In(list(PTZ_DIRECTIONS)),
        vol.Optional("duration"): _DURATION,
        vol.Optional("speed"): _SPEED,
        vol.Optional("variant", default=PTZ_DEFAULT_VARIANT): _VARIANT,
    },
    extra=vol.ALLOW_EXTRA,
)

SERVICE_PTZ_SWEEP_SCHEMA = vol.Schema(
    {
        vol.Optional("direction", default="right"): vol.In(list(PTZ_DIRECTIONS)),
        vol.Optional("steps", default=PTZ_SWEEP_DEFAULT_STEPS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=PTZ_SWEEP_MAX_STEPS)
        ),
        vol.Optional(
            "step_duration", default=PTZ_SWEEP_DEFAULT_STEP_DURATION
        ): _DURATION,
        vol.Optional("pause", default=PTZ_SWEEP_DEFAULT_PAUSE): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=120)
        ),
        vol.Optional("speed"): _SPEED,
        vol.Optional("return_home", default=True): vol.Coerce(bool),
        vol.Optional("wake", default=True): vol.Coerce(bool),
        vol.Optional(
            "wake_timeout", default=PTZ_SWEEP_DEFAULT_WAKE_TIMEOUT
        ): vol.All(vol.Coerce(float), vol.Range(min=0, max=120)),
        vol.Optional("variant", default=PTZ_DEFAULT_VARIANT): _VARIANT,
    },
    extra=vol.ALLOW_EXTRA,
)

SERVICE_PTZ_HOME_SCHEMA = vol.Schema(
    {
        vol.Optional("speed"): _SPEED,
        vol.Optional("variant", default=PTZ_DEFAULT_VARIANT): _VARIANT,
    },
    extra=vol.ALLOW_EXTRA,
)

SERVICE_PTZ_SET_HOME_SCHEMA = vol.Schema({}, extra=vol.ALLOW_EXTRA)

SERVICE_PTZ_PRESET_SCHEMA = vol.Schema(
    {
        vol.Required("action"): vol.In(["set", "goto", "delete"]),
        vol.Required("preset"): vol.All(vol.Coerce(int), vol.Range(min=1, max=32)),
        vol.Optional("name"): vol.All(str, vol.Length(max=32)),
    },
    extra=vol.ALLOW_EXTRA,
)

SERVICE_PTZ_CALIBRATE_SCHEMA = vol.Schema({}, extra=vol.ALLOW_EXTRA)

# The device accepts "del", the service exposes the clearer "delete".
_PRESET_ACTS = {"set": "set", "goto": "goto", "delete": "del"}


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _all_coordinators(hass: HomeAssistant) -> list[CloudEdgeMeariCoordinator]:
    return [
        value
        for value in hass.data.get(DOMAIN, {}).values()
        if isinstance(value, CloudEdgeMeariCoordinator)
    ]


def _device_uuids(device: Any) -> set[str]:
    if device is None:
        return set()
    return {ident[1] for ident in device.identifiers if ident[0] == DOMAIN}


def _resolve_targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[CloudEdgeMeariCoordinator]:
    """Map the call's entity/device targets onto camera coordinators."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    uuids: set[str] = set()
    unique_ids: set[str] = set()

    for entity_id in _as_list(call.data.get("entity_id")):
        entry = ent_reg.async_get(entity_id)
        if entry is None:
            _LOGGER.warning("PTZ: unknown entity %s", entity_id)
            continue
        if entry.device_id:
            uuids |= _device_uuids(dev_reg.async_get(entry.device_id))
        elif entry.unique_id:
            unique_ids.add(entry.unique_id)

    for device_id in _as_list(call.data.get("device_id")):
        uuids |= _device_uuids(dev_reg.async_get(device_id))

    targets: list[CloudEdgeMeariCoordinator] = []
    for coord in _all_coordinators(hass):
        if coord.device_uuid in uuids or f"{coord.device_uuid}_camera" in unique_ids:
            targets.append(coord)

    if not targets:
        _LOGGER.warning("PTZ: no CloudEdge / Meari camera matched %s", call.data)
    return targets


def _ptz_lock(hass: HomeAssistant, coord: CloudEdgeMeariCoordinator) -> asyncio.Lock:
    """Serialise motor commands per camera; two sweeps must not interleave."""
    locks: dict[str, asyncio.Lock] = hass.data[DOMAIN].setdefault(PTZ_LOCKS, {})
    return locks.setdefault(coord.device_uuid, asyncio.Lock())


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


async def _async_ensure_awake(
    hass: HomeAssistant,
    coord: CloudEdgeMeariCoordinator,
    timeout: float,
) -> bool:
    """Wake a dormant battery camera and wait until it reports being awake."""
    if not coord.is_battery_camera or coord.camera_awake:
        return True

    await hass.async_add_executor_job(coord.wake_camera)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if coord.camera_awake:
            return True
        await asyncio.sleep(0.5)

    if not coord.camera_awake:
        _LOGGER.warning(
            "PTZ: %s did not wake within %.0fs; sending commands anyway",
            coord.device_name,
            timeout,
        )
    return coord.camera_awake


async def _async_timed_move(
    hass: HomeAssistant,
    coord: CloudEdgeMeariCoordinator,
    direction: str,
    duration: float,
    *,
    variant: str | None = None,
    speed: int | None = None,
) -> None:
    """Move for *duration* seconds, then stop and record how far it travelled.

    The stop is shielded: a cancelled service call must never leave the motor
    running.
    """
    started = time.monotonic()
    await hass.async_add_executor_job(
        partial(coord.ptz_move, direction, variant=variant, speed=speed)
    )
    try:
        await asyncio.sleep(duration)
    finally:
        travelled = min(duration, time.monotonic() - started)
        await asyncio.shield(
            hass.async_add_executor_job(partial(coord.ptz_stop, variant=variant))
        )
        coord.record_ptz_travel(direction, travelled)


async def _async_go_home(
    hass: HomeAssistant,
    coord: CloudEdgeMeariCoordinator,
    *,
    variant: str | None = None,
    speed: int | None = None,
) -> None:
    """Undo the tracked travel, one axis at a time."""
    offset = coord.ptz_offset
    for axis, positive in (("pan", "right"), ("tilt", "up")):
        value = float(offset.get(axis, 0.0))
        # Below ~0.1s the motor barely twitches; not worth a command.
        if abs(value) < 0.1:
            continue
        direction = PTZ_OPPOSITE[positive] if value > 0 else positive
        await _async_timed_move(
            hass,
            coord,
            direction,
            min(abs(value), PTZ_MAX_MOVE_DURATION),
            variant=variant,
            speed=speed,
        )
    coord.reset_ptz_offset()


async def _async_sweep(
    hass: HomeAssistant,
    coord: CloudEdgeMeariCoordinator,
    data: dict[str, Any],
) -> None:
    """Step across the scene and come back to the starting position."""
    direction: str = data["direction"]
    steps: int = data["steps"]
    step_duration: float = data["step_duration"]
    pause: float = data["pause"]
    speed: int | None = data.get("speed")
    variant: str | None = data.get("variant")

    base_event = {
        "device_uuid": coord.device_uuid,
        "device_name": coord.device_name,
        "direction": direction,
        "steps": steps,
    }

    if data["wake"]:
        await _async_ensure_awake(hass, coord, data["wake_timeout"])

    hass.bus.async_fire(EVENT_PTZ_SWEEP_STARTED, dict(base_event))
    _LOGGER.info(
        "PTZ sweep on %s: %d steps of %.1fs %s, pause %.1fs",
        coord.device_name,
        steps,
        step_duration,
        direction,
        pause,
    )

    completed = 0
    try:
        for step in range(1, steps + 1):
            # Keep the live session from expiring mid-sweep.
            coord.wake_camera()
            await _async_timed_move(
                hass,
                coord,
                direction,
                step_duration,
                variant=variant,
                speed=speed,
            )
            completed = step
            hass.bus.async_fire(
                EVENT_PTZ_SWEEP_STEP, {**base_event, "step": step}
            )
            if pause and step < steps:
                await asyncio.sleep(pause)
    finally:
        # Runs on cancellation too, so an aborted sweep still comes home.
        if data["return_home"]:
            await _async_go_home(hass, coord, variant=variant, speed=speed)
        hass.bus.async_fire(
            EVENT_PTZ_SWEEP_FINISHED, {**base_event, "completed_steps": completed}
        )


# ---------------------------------------------------------------------------
# Service registration
# ---------------------------------------------------------------------------


def async_register_ptz_services(hass: HomeAssistant) -> None:
    """Register the PTZ services once for the whole integration."""
    if hass.services.has_service(DOMAIN, SERVICE_PTZ):
        return

    def _ptz_targets(call: ServiceCall) -> list[CloudEdgeMeariCoordinator]:
        targets = []
        for coord in _resolve_targets(hass, call):
            if not coord.has_ptz:
                _LOGGER.warning("PTZ: %s does not support PTZ", coord.device_name)
                continue
            targets.append(coord)
        return targets

    async def _handle_ptz(call: ServiceCall) -> None:
        action = call.data["action"]
        direction = call.data.get("argument")
        duration = call.data.get("duration")
        speed = call.data.get("speed")
        variant = call.data.get("variant")

        if action == "move" and not direction:
            raise HomeAssistantError("cloudplus.ptz: 'move' requires an argument")

        for coord in _ptz_targets(call):
            if action == "stop":
                await hass.async_add_executor_job(
                    partial(coord.ptz_stop, variant=variant)
                )
                continue
            if duration is None:
                # Legacy behaviour: run until an explicit stop call.
                await hass.async_add_executor_job(
                    partial(coord.ptz_move, direction, variant=variant, speed=speed)
                )
                continue
            async with _ptz_lock(hass, coord):
                await _async_timed_move(
                    hass, coord, direction, duration, variant=variant, speed=speed
                )

    async def _handle_sweep(call: ServiceCall) -> None:
        data = dict(call.data)
        for coord in _ptz_targets(call):
            lock = _ptz_lock(hass, coord)
            if lock.locked():
                _LOGGER.warning(
                    "PTZ: a sweep is already running on %s; skipping",
                    coord.device_name,
                )
                continue
            async with lock:
                await _async_sweep(hass, coord, data)

    async def _handle_home(call: ServiceCall) -> None:
        for coord in _ptz_targets(call):
            async with _ptz_lock(hass, coord):
                await _async_go_home(
                    hass,
                    coord,
                    variant=call.data.get("variant"),
                    speed=call.data.get("speed"),
                )

    async def _handle_set_home(call: ServiceCall) -> None:
        for coord in _ptz_targets(call):
            coord.reset_ptz_offset()

    async def _handle_preset(call: ServiceCall) -> None:
        act = _PRESET_ACTS[call.data["action"]]
        preset = call.data["preset"]
        name = call.data.get("name")
        for coord in _ptz_targets(call):
            await hass.async_add_executor_job(
                partial(coord.ptz_preset, preset, act, name)
            )

    async def _handle_calibrate(call: ServiceCall) -> None:
        for coord in _ptz_targets(call):
            async with _ptz_lock(hass, coord):
                await hass.async_add_executor_job(coord.ptz_calibrate)

    for name, handler, schema in (
        (SERVICE_PTZ, _handle_ptz, SERVICE_PTZ_SCHEMA),
        (SERVICE_PTZ_SWEEP, _handle_sweep, SERVICE_PTZ_SWEEP_SCHEMA),
        (SERVICE_PTZ_HOME, _handle_home, SERVICE_PTZ_HOME_SCHEMA),
        (SERVICE_PTZ_SET_HOME, _handle_set_home, SERVICE_PTZ_SET_HOME_SCHEMA),
        (SERVICE_PTZ_PRESET, _handle_preset, SERVICE_PTZ_PRESET_SCHEMA),
        (SERVICE_PTZ_CALIBRATE, _handle_calibrate, SERVICE_PTZ_CALIBRATE_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema)
