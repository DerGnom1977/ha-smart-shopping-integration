"""Smart Shopping: Fachlogik (Port von smart_shopping.yaml und shopping_push_warenkorb.yaml)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant.core import Event, ServiceCall

from .const import STORES
from .framework import LogicBase

_LOGGER = logging.getLogger(__name__)
HERE = Path(__file__).parent
TOOL = HERE / "smart_shopping_tool.py"
STORE_NAMES = {k: n for k, n in STORES}


class Logic(LogicBase):
    def __init__(self, rt) -> None:
        super().__init__(rt)
        rt.mem.update(status={})
        rt.actions.update(aktualisieren=self.fetch, aufraeumen=self.rebuild, reset=self._reset)
        rt.register_service("angebote_aktualisieren", self._svc_fetch, vol.Schema({}))
        rt.register_service(
            "angebot_speichern",
            self._svc_angebot,
            vol.Schema({
                vol.Required("markt"): vol.In([n for _, n in STORES]),
                vol.Required("name"): str,
                vol.Required("preis"): vol.Coerce(float),
                vol.Optional("originalpreis", default=0): vol.Coerce(float),
                vol.Optional("gueltig_bis", default=""): str,
                vol.Optional("bild", default=""): str,
            }),
        )
        rt.register_service(
            "artikel_hinzufuegen",
            self._svc_artikel,
            vol.Schema({
                vol.Required("markt"): vol.In([n for _, n in STORES]),
                vol.Required("name"): str,
                vol.Optional("menge", default=1): vol.Coerce(int),
                vol.Optional("preis", default=0): vol.Coerce(float),
            }),
        )

    # -- Start -------------------------------------------------------------
    async def async_start(self) -> None:
        rt = self.rt
        await rt.hass.async_add_executor_job(self._init_files)
        rt.track_time(self._taeglich, hour=5, minute=45, second=0)
        rt.track_time(self._mitternacht, hour=0, minute=5, second=0)
        rt.track_interval(self._status_timer, timedelta(minutes=15))
        ids = [rt.cfg(f"liste_{k}") for k, _ in STORES]
        rt.track_state(ids, self._liste_changed)
        rt.later(120, self._nach_start)
        await self._status()

    def _init_files(self) -> None:
        base = Path(self.rt.hass.config.config_dir)
        data = base / "smart_shopping"
        data.mkdir(parents=True, exist_ok=True)
        (base / "www" / "smart_shopping").mkdir(parents=True, exist_ok=True)
        for name in ("sources.json", "manual.json"):
            if not (data / name).exists():
                shutil.copy(HERE / name, data / name)

    async def _reset(self) -> None:
        self.rt.reset_defaults()

    # -- Werkzeug-Aufruf ---------------------------------------------------
    async def _tool(self, *args: str) -> tuple[int, str]:
        env = {**os.environ, "SMART_SHOPPING_CONFIG": self.rt.hass.config.config_dir}
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(TOOL), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Angebots-Tool fehlgeschlagen: %s", err)
            return 1, str(err)
        text = out.decode("utf-8", "replace").strip()
        if proc.returncode:
            _LOGGER.warning("Angebots-Tool %s: %s", args[0], text)
        return proc.returncode or 0, text

    async def _status(self) -> None:
        code, text = await self._tool("status")
        if code == 0:
            try:
                self.rt.mem["status"] = json.loads(text)
            except ValueError:
                pass
        self.rt.refresh()

    async def fetch(self) -> None:
        await self._tool("fetch")
        await self._status()

    async def rebuild(self) -> None:
        await self._tool("rebuild")
        await self._status()

    # -- Zeitgesteuert -----------------------------------------------------
    async def _taeglich(self, _now) -> None:
        if self.rt.on("smart_shopping_taeglich_aktualisieren"):
            await self.fetch()

    async def _nach_start(self, _now=None) -> None:
        if self.rt.on("smart_shopping_taeglich_aktualisieren"):
            await self.fetch()

    async def _mitternacht(self, _now) -> None:
        await self.rebuild()

    async def _status_timer(self, _now) -> None:
        await self._status()

    # -- Dienste -----------------------------------------------------------
    async def _svc_fetch(self, call: ServiceCall) -> None:
        await self.fetch()

    async def _svc_angebot(self, call: ServiceCall) -> None:
        d = call.data
        args = ["add", "--store", d["markt"], "--name", d["name"].replace('"', ""),
                "--price", str(d["preis"]), "--original", str(d["originalpreis"]),
                "--valid-until", d["gueltig_bis"], "--image", d["bild"].replace('"', "")]
        await self._tool(*args)
        await self._status()

    def _liste_von(self, markt: str) -> str:
        key = next(k for k, n in STORES if n == markt)
        return self.rt.cfg(f"liste_{key}")

    async def _items(self, entity: str) -> list[dict]:
        try:
            resp = await self.rt.hass.services.async_call(
                "todo", "get_items", {"entity_id": entity, "status": "needs_action"},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("todo.get_items %s: %s", entity, err)
            return []
        return (resp or {}).get(entity, {}).get("items", [])

    async def _svc_artikel(self, call: ServiceCall) -> None:
        d = call.data
        ziel = self._liste_von(d["markt"])
        name = d["name"].strip()
        if name.lower() in [i.get("summary", "").lower() for i in await self._items(ziel)]:
            _LOGGER.info("Artikel steht bereits auf der Liste: %s", name)
            return
        desc = f"qty: {max(1, int(d['menge']))}"
        if d["preis"] and d["preis"] > 0:
            desc += f"\nprice: {d['preis']}"
        desc += "\nsource: manual"
        await self.rt.call("todo", "add_item", {"entity_id": ziel, "item": name, "description": desc})

    # -- Push bei neuem Artikel ---------------------------------------------
    async def _liste_changed(self, event: Event) -> None:
        rt = self.rt
        old, new = event.data.get("old_state"), event.data.get("new_state")
        rt.refresh()
        if not rt.on("smart_shopping_push_aktiv") or old is None or new is None:
            return
        try:
            alt, neu = int(float(old.state)), int(float(new.state))
        except ValueError:
            return
        if neu <= alt:
            return
        eid = event.data["entity_id"]
        markt = next((n for k, n in STORES if rt.cfg(f"liste_{k}") == eid), eid)
        key = next((k for k, n in STORES if n == markt), "liste")
        anzahl = neu - alt
        artikel = ""
        if anzahl == 1:
            items = await self._items(eid)
            artikel = items[-1].get("summary", "") if items else ""
        if artikel:
            text = f"{artikel} im {markt}-Warenkorb ({neu} offen)."
        elif anzahl == 1:
            text = f"Neuer Artikel im {markt}-Warenkorb ({neu} offen)."
        else:
            text = f"{anzahl} neue Artikel im {markt}-Warenkorb ({neu} offen)."
        await rt.notify(f"🛒 {markt}", text, {"url": rt.cfg("push_url"), "push": {"thread-id": f"smart-shopping-{key}"}})
