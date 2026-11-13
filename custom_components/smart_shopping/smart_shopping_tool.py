#!/usr/bin/env python3
"""Smart Shopping – Angebots-Tool (nur Python-Standardbibliothek).

Befehle:
  fetch    Quellen aus sources.json abrufen, mit manual.json zusammenführen,
           abgelaufene Angebote entfernen, www/smart_shopping/offers.json schreiben
  rebuild  wie fetch, aber ohne Netzwerkzugriff (nutzt den Zwischenspeicher)
  status   JSON für den Home-Assistant-Sensor ausgeben
  add      manuelles Angebot in manual.json eintragen

Aufbau (modular): jede Quelle ist ein "Adapter" (Funktion in ADAPTERS).
Fällt eine Quelle aus, bleiben ihre zuletzt bekannten Angebote erhalten und der
Fehler wird nur im Status vermerkt – andere Quellen sind nicht betroffen.

Verzeichnis: Umgebungsvariable SMART_SHOPPING_CONFIG (Standard: /config).
Es werden keine Zugangsdaten gespeichert; Tokens werden aus einer Datei gelesen
(token_file in sources.json).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.request

BASE = os.environ.get("SMART_SHOPPING_CONFIG", "/config")
DATA = os.path.join(BASE, "smart_shopping")
WWW = os.path.join(BASE, "www", "smart_shopping")
SOURCES = os.path.join(DATA, "sources.json")
MANUAL = os.path.join(DATA, "manual.json")
CACHE = os.path.join(DATA, "cache.json")
OUT = os.path.join(WWW, "offers.json")
STORES = ["Lidl", "Netto", "Penny", "Kaufland", "REWE"]
USER_AGENT = "HomeAssistant-SmartShopping/1.0"
MAX_BYTES = 5 * 1024 * 1024


# ----------------------------------------------------------------- Helfer
def today():
    return dt.date.today().isoformat()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_path(obj, path):
    """Punkt-Pfad ('a.b.0.c') in verschachtelten Daten auflösen."""
    cur = obj
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def to_float(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^\d,.\-]", "", str(value))
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def to_date(value):
    """Datum als YYYY-MM-DD. Zeitangaben mit Zeitzone (z. B. ...T23:00:00Z) werden
    nach Europe/Berlin umgerechnet, sonst verschiebt sich der Tag um eins."""
    if not value:
        return ""
    text = str(value).strip()
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return ""
    if re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                from zoneinfo import ZoneInfo
                parsed = parsed.astimezone(ZoneInfo("Europe/Berlin"))
            return parsed.date().isoformat()
        except Exception:
            pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else ""


def normalize(raw, store):
    """Rohdaten -> einheitliches Angebotsformat. Gibt None bei unbrauchbaren Daten."""
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    price = to_float(raw.get("price"))
    original = to_float(raw.get("original_price"))
    discount = to_float(raw.get("discount_percent"))
    if discount is None and price and original and original > price:
        discount = round((1 - price / original) * 100)
    image = str(raw.get("image") or "").strip()
    if image and not re.match(r"^(https?://|/)", image):
        image = ""
    offer_store = str(raw.get("store") or store).strip() or store
    offer = {
        "name": name[:120],
        "store": offer_store,
        "price": price,
        "original_price": original,
        "discount_percent": int(discount) if discount else None,
        "image": image,
        "valid_from": to_date(raw.get("valid_from")),
        "valid_until": to_date(raw.get("valid_until")),
        "special": bool(raw.get("special")),
        "unit": str(raw.get("unit") or "")[:40],
    }
    key = "|".join([offer_store, name.lower(), str(price), offer["valid_until"]])
    offer["id"] = str(raw.get("id") or hashlib.sha1(key.encode()).hexdigest()[:12])
    return offer


# --------------------------------------------------------------- Adapter
def adapter_json(source):
    """Generischer JSON-Adapter: 'url' (http/https) oder 'file' (lokale Datei).

    mapping: Zielfeld -> Punkt-Pfad im Quell-JSON (z. B. {"name": "title"}).
    items_path: Pfad zur Liste der Angebote (leer = Wurzel ist die Liste).
    """
    if source.get("url"):
        url = source["url"]
        if not re.match(r"^https?://", url):
            raise ValueError("Nur http(s)-URLs erlaubt")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        token_file = source.get("token_file")
        if token_file:
            with open(token_file, "r", encoding="utf-8") as fh:
                headers["Authorization"] = "Bearer " + fh.read().strip()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("Antwort zu groß")
        payload = json.loads(body.decode("utf-8"))
    elif source.get("file"):
        with open(source["file"], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        raise ValueError("url oder file fehlt")

    items = get_path(payload, source["items_path"]) if source.get("items_path") else payload
    if not isinstance(items, list):
        raise ValueError("items_path liefert keine Liste")
    mapping = source.get("mapping") or {}
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = {}
        for target in ("id", "name", "price", "original_price", "discount_percent",
                      "image", "valid_from", "valid_until", "special", "unit", "store"):
            src = mapping.get(target, target)
            raw[target] = get_path(item, src)
        result.append(raw)
    return result


def adapter_marktguru(source):
    """marktguru.de (inoffizielle Web-API, KEIN offizielles Produkt von marktguru).

    Die API liefert nur Treffer zu Suchbegriffen (kein "alle Angebote").
    Konfiguration: zip_code, queries (Liste), retailers (Zuordnung Händlername
    -> Markt), keys_file (JSON mit apikey/clientkey, nicht im Klartext in
    sources.json), limit, pause_seconds. Wenige Abfragen, höchstens 1x täglich.
    """
    import time
    import urllib.parse

    keys = load_json(source.get("keys_file", os.path.join(DATA, "marktguru_keys.json")), {})
    if not keys.get("apikey") or not keys.get("clientkey"):
        raise ValueError("marktguru_keys.json mit apikey und clientkey fehlt")
    zip_code = str(source.get("zip_code", "")).strip()
    if not re.fullmatch(r"\d{5}", zip_code):
        raise ValueError("zip_code (5 Ziffern) fehlt")
    queries = [q for q in source.get("queries", []) if str(q).strip()][:30]
    if not queries:
        raise ValueError("queries ist leer")
    retailers = source.get("retailers") or {"lidl": "Lidl", "netto marken-discount": "Netto",
                                      "penny": "Penny", "kaufland": "Kaufland", "rewe": "REWE"}
    base = source.get("base_url", "https://api.marktguru.de/api/v1/offers/search")
    img_tpl = source.get("image_url", "https://mg2de.b-cdn.net/api/v1/offers/{id}/images/default/0/medium.webp")
    limit = min(int(source.get("limit", 24)), 100)
    pause = float(source.get("pause_seconds", 1.5))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
               "x-apikey": keys["apikey"], "x-clientkey": keys["clientkey"]}

    def match_store(offer):
        names = [str(a.get("name", "")) for a in (offer.get("advertisers") or []) if isinstance(a, dict)]
        for name in names:
            low = name.lower()
            for prefix, store in retailers.items():
                if low.startswith(prefix.lower()):
                    return store
        return None

    result, errors = [], 0
    for idx, query in enumerate(queries):
        if idx:
            time.sleep(pause)
        params = urllib.parse.urlencode({"as": "web", "limit": limit, "offset": 0,
                                         "q": str(query).strip(), "zipCode": zip_code})
        try:
            req = urllib.request.Request(f"{base}?{params}", headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Antwort zu groß")
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            errors += 1
            continue
        for item in payload.get("results") or []:
            store = match_store(item)
            if not store:
                continue
            validity = (item.get("validityDates") or [{}])[0] or {}
            product = item.get("product") or {}
            brand = (item.get("brand") or {}).get("name") or ""
            title = product.get("name") or item.get("description") or str(query)
            if brand and brand.lower() not in title.lower():
                title = f"{brand} {title}"
            result.append({
                "id": f"mg-{item.get('id')}",
                "store": store,
                "name": title,
                "price": item.get("price"),
                "original_price": item.get("oldPrice"),
                "valid_from": validity.get("from"),
                "valid_until": validity.get("to"),
                "image": img_tpl.format(id=item.get("id")) if item.get("id") else "",
                "unit": item.get("description") or "",
            })
    if errors == len(queries):
        raise RuntimeError("alle marktguru-Abfragen fehlgeschlagen")
    return result


ADAPTERS = {"json": adapter_json, "marktguru": adapter_marktguru}  # weitere hier registrieren


# ----------------------------------------------------------------- Kern
def build(fetch):
    sources = load_json(SOURCES, {}).get("sources", [])
    cache = load_json(CACHE, {})
    previous = load_json(OUT, {}).get("sources", {})
    status = {}
    collected = []

    for source in sources:
        sid = source.get("id") or source.get("store", "?")
        if not source.get("enabled", False):
            continue
        store = source.get("store", "")
        if not fetch:  # rebuild: nur Zwischenspeicher, vorherigen Status behalten
            offers = cache.get(sid, [])
            status[sid] = previous.get(sid, {"ok": True, "count": len(offers)})
            collected.extend(offers)
            continue
        try:
            adapter = ADAPTERS[source.get("type", "json")]
            raws = adapter(source)
            offers = [o for o in (normalize(r, store) for r in raws) if o]
            cache[sid] = offers
            status[sid] = {"ok": True, "count": len(offers)}
        except Exception as exc:  # eine Quelle darf nie das Ganze stoppen
            offers = cache.get(sid, [])
            status[sid] = {"ok": False, "error": str(exc)[:200], "cached": len(offers)}
        collected.extend(offers)

    manual = load_json(MANUAL, {}).get("offers", [])
    for raw in manual:
        offer = normalize(raw, raw.get("store", ""))
        if offer and offer["store"]:
            collected.append(offer)

    now = today()
    seen, final = set(), []
    for offer in collected:
        if offer["valid_until"] and offer["valid_until"] < now:
            continue  # abgelaufen
        if offer["store"] not in STORES:
            continue  # Markt nicht (mehr) konfiguriert, z. B. alter Zwischenspeicher
        if offer["id"] in seen:
            continue
        seen.add(offer["id"])
        final.append(offer)
    final.sort(key=lambda o: (o["store"], o["name"].lower()))

    save_json(CACHE, cache)
    save_json(OUT, {
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
        "sources": status,
        "offers": final,
    })
    return final, status


def cmd_status(_args):
    data = load_json(OUT, {})
    now = today()
    offers = [o for o in data.get("offers", [])
              if not o.get("valid_until") or o["valid_until"] >= now]
    per_store = {}
    for o in offers:
        per_store[o["store"]] = per_store.get(o["store"], 0) + 1
    errors = [f"{k}: {v.get('error')}" for k, v in (data.get("sources") or {}).items()
              if not v.get("ok")]
    print(json.dumps({"count": len(offers), "updated": data.get("updated", ""),
                      "errors": errors, "per_store": per_store}, ensure_ascii=False))


def cmd_add(args):
    if args.store not in STORES:
        sys.exit(f"Unbekannter Markt: {args.store}")
    price = to_float(args.price)
    original = to_float(args.original)
    if not args.name.strip() or price is None or price <= 0:
        sys.exit("Name und Preis > 0 erforderlich")
    data = load_json(MANUAL, {"offers": []})
    data.setdefault("offers", []).append({
        "store": args.store,
        "name": args.name.strip(),
        "price": price,
        "original_price": original if original and original > 0 else None,
        "valid_until": to_date(args.valid_until) or "",
        "image": args.image or "",
    })
    save_json(MANUAL, data)
    build(fetch=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    sub.add_parser("rebuild")
    sub.add_parser("status")
    add = sub.add_parser("add")
    for opt in ("store", "name", "price", "original", "valid-until", "image"):
        add.add_argument("--" + opt, default="")
    args = parser.parse_args()
    if args.cmd == "fetch":
        offers, status = build(fetch=True)
        print(f"{len(offers)} Angebote, Quellen: {json.dumps(status, ensure_ascii=False)}")
    elif args.cmd == "rebuild":
        offers, _ = build(fetch=False)
        print(f"{len(offers)} Angebote")
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "add":
        cmd_add(args)


if __name__ == "__main__":
    main()
