"""Smart Shopping."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, FIELDS, PREFIX, SPECS, TITLE
from .framework import Runtime
from .logic import Logic

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ['button', 'sensor', 'switch']


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    rt = Runtime(hass, entry, DOMAIN, TITLE, PREFIX, SPECS, FIELDS)
    rt.logic = Logic(rt)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = rt
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await rt.logic.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        rt = hass.data[DOMAIN].pop(entry.entry_id)
        await rt.logic.async_stop()
        await rt.shutdown()
    return ok


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
