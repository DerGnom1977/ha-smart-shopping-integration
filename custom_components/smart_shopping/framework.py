"""Gemeinsames Mini-Framework: Entitäten aus einer Spezifikation, Laufzeit-Helfer, Konfig-Dialog."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

VALUE_KINDS = ("switch", "number", "select", "time")


# --------------------------------------------------------------------------- Spezifikation
@dataclass
class Spec:
    """Beschreibung einer Entität."""

    kind: str  # switch|number|select|time|sensor|binary_sensor|button
    key: str  # Teil der entity_id, z. B. blink_push_flur
    name: str
    icon: str | None = None
    default: Any = None
    min: float = 0
    max: float = 100
    step: float = 1
    unit: str | None = None
    mode: str = "slider"
    options: list[str] | None = None
    fn: Callable[["Runtime"], Any] | None = None  # sensor/binary_sensor
    attrs: Callable[["Runtime"], dict] | None = None
    device_class: str | None = None
    state_class: str | None = None
    press: str | None = None  # button: Name der Aktion in Runtime.actions


@dataclass
class Field:
    """Eintrag im Konfigurationsdialog."""

    key: str
    default: str
    kind: str  # text | entity
    label: str
    domains: tuple[str, ...] = ()


def build_schema(fields: list[Field], current: dict) -> vol.Schema:
    """Formular für Config- und Options-Flow."""
    schema: dict = {}
    for f in fields:
        value = current.get(f.key, f.default)
        if f.kind == "entity":
            sel: Any = selector.EntitySelector(
                selector.EntitySelectorConfig(domain=list(f.domains) or None)
            )
        else:
            sel = selector.TextSelector()
        schema[vol.Optional(f.key, description={"suggested_value": value})] = sel
    return vol.Schema(schema)


# --------------------------------------------------------------------------- Laufzeit
class Runtime:
    """Hält Werte, Konfiguration und Listener einer Integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        domain: str,
        title: str,
        prefix: str,
        specs: list[Spec],
        fields: list[Field],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.domain = domain
        self.title = title
        self.prefix = prefix
        self.specs = specs
        self.defaults = {f.key: f.default for f in fields}
        self.values: dict[str, Any] = {}
        for s in specs:
            if s.kind in VALUE_KINDS:
                self.values[s.key] = self._initial(s)
        self.mem: dict[str, Any] = {}
        self.actions: dict[str, Callable] = {}
        self.logic: Any = None
        self._unsub: list[Callable[[], None]] = []
        self._tasks: set = set()
        self._services: list[str] = []

    # -- Werte -------------------------------------------------------------
    @staticmethod
    def _initial(s: Spec) -> Any:
        if s.kind == "time" and isinstance(s.default, str):
            return dtime.fromisoformat(s.default)
        if s.kind == "select" and s.default is None and s.options:
            return s.options[0]
        return s.default

    @property
    def signal(self) -> str:
        return f"{self.domain}_{self.entry.entry_id}_update"

    def get(self, key: str) -> Any:
        return self.values.get(key)

    def on(self, key: str) -> bool:
        return bool(self.values.get(key))

    def flt(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.values.get(key))
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.refresh()

    def reset_defaults(self) -> None:
        for s in self.specs:
            if s.kind in VALUE_KINDS:
                self.values[s.key] = self._initial(s)
        self.refresh()

    @callback
    def refresh(self, *_: Any) -> None:
        async_dispatcher_send(self.hass, self.signal)

    # -- Konfiguration -----------------------------------------------------
    def cfg(self, key: str) -> str:
        merged = {**self.entry.data, **self.entry.options}
        value = merged.get(key)
        if value is None:
            value = self.defaults.get(key, "")
        return value

    # -- Zustände ----------------------------------------------------------
    def state(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        return st.state if st else None

    def is_state(self, entity_id: str | None, value: str) -> bool:
        return self.state(entity_id) == value

    def attr(self, entity_id: str | None, name: str) -> Any:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        return st.attributes.get(name) if st else None

    def num(self, entity_id: str | None, default: float | None = None) -> float | None:
        try:
            return float(self.state(entity_id))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    # -- Aktionen ----------------------------------------------------------
    async def call(
        self, domain: str, service: str, data: dict | None = None, blocking: bool = True
    ) -> bool:
        try:
            await self.hass.services.async_call(
                domain, service, data or {}, blocking=blocking
            )
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Dienst %s.%s fehlgeschlagen: %s", domain, service, err)
            return False

    async def notify(
        self,
        title: str,
        message: str,
        data: dict | None = None,
        who: tuple[str, ...] = ("person1", "person2"),
    ) -> None:
        """Push an die Geräte, deren Schalter <prefix>_ziel_<name> an ist."""
        for w in who:
            if not self.on(f"{self.prefix}_ziel_{w}"):
                continue
            service = str(self.cfg(f"notify_{w}") or "").removeprefix("notify.")
            if not service:
                continue
            payload: dict = {"title": title, "message": message}
            if data:
                payload["data"] = data
            await self.call("notify", service, payload)

    async def pn_create(self, title: str, message: str, notification_id: str) -> None:
        await self.call(
            "persistent_notification",
            "create",
            {"title": title, "message": message, "notification_id": notification_id},
        )

    async def pn_dismiss(self, notification_id: str) -> None:
        await self.call(
            "persistent_notification", "dismiss", {"notification_id": notification_id}
        )

    # -- Listener ----------------------------------------------------------
    def track_state(self, entity_ids: list[str | None], cb: Callable) -> None:
        ids = [e for e in entity_ids if e]
        if ids:
            self._unsub.append(async_track_state_change_event(self.hass, ids, cb))

    def track_time(self, cb: Callable, **kwargs: Any) -> None:
        self._unsub.append(async_track_time_change(self.hass, cb, **kwargs))

    def track_interval(self, cb: Callable, interval) -> None:
        self._unsub.append(async_track_time_interval(self.hass, cb, interval))

    def track_event(self, event_type: str, cb: Callable) -> None:
        self._unsub.append(self.hass.bus.async_listen(event_type, cb))

    def later(self, seconds: float, cb: Callable) -> Callable[[], None]:
        unsub = async_call_later(self.hass, seconds, cb)
        self._unsub.append(unsub)
        return unsub

    def spawn(self, coro) -> None:
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def register_service(self, name: str, handler: Callable, schema=None) -> None:
        self.hass.services.async_register(self.domain, name, handler, schema=schema)
        self._services.append(name)

    async def shutdown(self) -> None:
        for unsub in self._unsub:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsub.clear()
        for task in list(self._tasks):
            task.cancel()
        for name in self._services:
            self.hass.services.async_remove(self.domain, name)
        self._services.clear()


class LogicBase:
    """Basisklasse für die Fachlogik."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    async def async_start(self) -> None:  # pragma: no cover
        return None

    async def async_stop(self) -> None:  # pragma: no cover
        return None


# --------------------------------------------------------------------------- Zeit-Helfer
def now_local() -> datetime:
    return dt_util.now()


def in_window(now_t: dtime, start: dtime, end: dtime) -> bool:
    """True, wenn now_t zwischen start und end liegt (auch über Mitternacht)."""
    if start <= end:
        return start <= now_t <= end
    return now_t >= start or now_t <= end


# --------------------------------------------------------------------------- Entitäten
class RTBase(RestoreEntity):
    _attr_should_poll = False
    _platform = ""

    def __init__(self, rt: Runtime, spec: Spec) -> None:
        self.rt = rt
        self.spec = spec
        self._attr_unique_id = f"{rt.domain}_{spec.key}"
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self.entity_id = f"{self._platform}.{spec.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(rt.domain, rt.entry.entry_id)},
            name=rt.title,
            manufacturer="HA Custom",
            model=rt.title,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.rt.signal, self._on_signal)
        )

    @callback
    def _on_signal(self) -> None:
        self.async_write_ha_state()


class RTSwitch(RTBase, SwitchEntity):
    _platform = "switch"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.rt.values[self.spec.key] = last.state == "on"

    @property
    def is_on(self) -> bool:
        return self.rt.on(self.spec.key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.rt.set(self.spec.key, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.rt.set(self.spec.key, False)


class RTNumber(RTBase, NumberEntity):
    _platform = "number"

    def __init__(self, rt: Runtime, spec: Spec) -> None:
        super().__init__(rt, spec)
        self._attr_native_min_value = spec.min
        self._attr_native_max_value = spec.max
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_mode = NumberMode.SLIDER if spec.mode == "slider" else NumberMode.BOX

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                self.rt.values[self.spec.key] = float(last.state)
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float | None:
        v = self.rt.values.get(self.spec.key)
        return None if v is None else float(v)

    async def async_set_native_value(self, value: float) -> None:
        self.rt.set(self.spec.key, float(value))


class RTSelect(RTBase, SelectEntity):
    _platform = "select"

    def __init__(self, rt: Runtime, spec: Spec) -> None:
        super().__init__(rt, spec)
        self._attr_options = list(spec.options or [])

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self.rt.values[self.spec.key] = last.state

    @property
    def current_option(self) -> str | None:
        return self.rt.values.get(self.spec.key)

    async def async_select_option(self, option: str) -> None:
        self.rt.set(self.spec.key, option)


class RTTime(RTBase, TimeEntity):
    _platform = "time"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            try:
                self.rt.values[self.spec.key] = dtime.fromisoformat(last.state)
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> dtime | None:
        return self.rt.values.get(self.spec.key)

    async def async_set_value(self, value: dtime) -> None:
        self.rt.set(self.spec.key, value)


class RTButton(RTBase, ButtonEntity):
    _platform = "button"

    async def async_press(self) -> None:
        action = self.rt.actions.get(self.spec.press or "")
        if action:
            await action()


class RTSensor(RTBase, SensorEntity):
    _platform = "sensor"

    def __init__(self, rt: Runtime, spec: Spec) -> None:
        super().__init__(rt, spec)
        self._attr_native_unit_of_measurement = spec.unit
        if spec.device_class:
            self._attr_device_class = SensorDeviceClass(spec.device_class)
        if spec.state_class:
            self._attr_state_class = SensorStateClass(spec.state_class)

    @property
    def native_value(self) -> Any:
        try:
            return self.spec.fn(self.rt) if self.spec.fn else None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Sensor %s: %s", self.spec.key, err)
            return None

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self.spec.attrs:
            return None
        try:
            return self.spec.attrs(self.rt)
        except Exception:  # noqa: BLE001
            return None


class RTBinary(RTBase, BinarySensorEntity):
    _platform = "binary_sensor"

    @property
    def is_on(self) -> bool | None:
        try:
            return bool(self.spec.fn(self.rt)) if self.spec.fn else None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Binärsensor %s: %s", self.spec.key, err)
            return None


_CLASSES = {
    "switch": RTSwitch,
    "number": RTNumber,
    "select": RTSelect,
    "time": RTTime,
    "button": RTButton,
    "sensor": RTSensor,
    "binary_sensor": RTBinary,
}


def platform_setup(kind: str, domain: str):
    """Liefert async_setup_entry für eine Plattform-Datei."""

    async def _setup(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
        rt: Runtime = hass.data[domain][entry.entry_id]
        async_add_entities(
            [_CLASSES[kind](rt, s) for s in rt.specs if s.kind == kind]
        )

    return _setup
