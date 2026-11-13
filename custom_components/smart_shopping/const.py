"""Smart Shopping: Spezifikation (Angebote, Einkaufslisten, Push)."""
from __future__ import annotations

from .framework import Field, Spec

DOMAIN = "smart_shopping"
TITLE = "Smart Shopping"
PREFIX = "smart_shopping"
VERSION = "1.0.0"
DESCRIPTION = "Angebote einsammeln, Einkaufslisten je Markt zählen und bei neuen Artikeln per Push melden."
DASH = "dashboard-einkaufsliste"
STORES = [("lidl", "Lidl"), ("netto", "Netto"), ("penny", "Penny"), ("kaufland", "Kaufland"), ("rewe", "REWE")]

FIELDS = [
    Field("notify_person1", "mobile_app_mein_handy", "text", "Notify-Dienst Person 1"),
    Field("notify_person2", "mobile_app_partner_handy", "text", "Notify-Dienst Person 2"),
    Field("push_url", "/dashboard-einkaufsliste/einkauf", "text", "Seite, die ein Tipp auf die Push-Meldung öffnet"),
] + [Field(f"liste_{k}", f"todo.{k}", "entity", f"To-do-Liste {n}", ("todo",)) for k, n in STORES]


def offen(rt, k: str) -> int:
    try:
        return int(float(rt.state(rt.cfg(f"liste_{k}"))))
    except (TypeError, ValueError):
        return 0


def gesamt(rt) -> int:
    return sum(offen(rt, k) for k, _ in STORES)


def angebote(rt):
    return int((rt.mem.get("status") or {}).get("count", 0))


def angebote_attrs(rt) -> dict:
    s = rt.mem.get("status") or {}
    return {"updated": s.get("updated", ""), "errors": s.get("errors", []), "per_store": s.get("per_store", {})}


SPECS = [
    Spec("switch", "smart_shopping_push_aktiv", "Push bei neuem Artikel", "mdi:bell-ring", True),
    Spec("switch", "smart_shopping_ziel_person1", "Push an Person 1", "mdi:cellphone-message", True),
    Spec("switch", "smart_shopping_ziel_person2", "Push an Person 2", "mdi:cellphone-message", True),
    Spec("switch", "smart_shopping_taeglich_aktualisieren", "Angebote täglich aktualisieren", "mdi:calendar-sync", True),
    Spec("sensor", "smart_shopping_angebote", "Smart Shopping Angebote", "mdi:tag-multiple", unit="Angebote", fn=angebote, attrs=angebote_attrs),
]
for _k, _n in STORES:
    SPECS.append(Spec("sensor", f"smart_shopping_{_k}_offen", f"Smart Shopping {_n} offen", "mdi:cart", unit="Artikel", fn=lambda rt, k=_k: offen(rt, k)))
SPECS += [
    Spec("sensor", "smart_shopping_gesamt_offen", "Smart Shopping Gesamt offen", "mdi:cart-variant", unit="Artikel", fn=gesamt),
    Spec("button", "smart_shopping_angebote_aktualisieren", "Angebote aktualisieren", "mdi:refresh", press="aktualisieren"),
    Spec("button", "smart_shopping_abgelaufene_entfernen", "Abgelaufene Angebote entfernen", "mdi:broom", press="aufraeumen"),
    Spec("button", "smart_shopping_standardwerte", "Standardwerte setzen", "mdi:restore", press="reset"),
]

SECTIONS = [
    ("Angebote", ["smart_shopping_angebote", "smart_shopping_angebote_aktualisieren", "smart_shopping_abgelaufene_entfernen", "smart_shopping_taeglich_aktualisieren"]),
    ("Offene Einkäufe", [f"smart_shopping_{k}_offen" for k, _ in STORES] + ["smart_shopping_gesamt_offen"]),
    ("Push", ["smart_shopping_push_aktiv", "smart_shopping_ziel_person1", "smart_shopping_ziel_person2", "smart_shopping_standardwerte"]),
]
