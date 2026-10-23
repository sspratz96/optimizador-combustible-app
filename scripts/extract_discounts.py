
#!/usr/bin/env python3
"""Extrae descuentos de bencina (Chile) desde fuentes publicas con Claude
y actualiza discounts.json si hay cambios. Pensado para GitHub Actions.

Salida:
  - exit 0 sin tocar archivos  -> sin cambios
  - exit 0 con discounts.json y pr_body.md reescritos -> hay cambios (el workflow abre PR)
  - exit 1 -> error o extraccion sospechosa; NO toca discounts.json
"""
import base64
import json
import os
import re
import sys
import unicodedata
import urllib.request
from calendar import monthrange
from datetime import date
from html.parser import HTMLParser

CHOCALE_TAG_URL = "https://chocale.cl/temas/descuentos-bencinas/"
MODEL = "claude-haiku-4-5-20251001"
MAX_IMAGES = 2
VALID_BRANDS = {"Copec", "Shell", "Aramco"}
VALID_DAYS = set(range(7))
MIN_ENTRIES = 8          # tipicamente hay ~15; menos que esto = extraccion degenerada
MAX_ENTRIES = 60
MAX_CHARS_PER_PAGE = 40000
DAY_ABBR = ["dom", "lun", "mar", "mie", "jue", "vie", "sab"]
UA = "BencinApp-bot/1.0 (actualizacion mensual de descuentos; contacto en el repo)"


class TextExtractor(HTMLParser):
    """HTML -> texto plano, ignorando script/style."""
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip > 0:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip == 0 and data.strip():
            self.parts.append(data.strip())


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def html_to_text(html):
    p = TextExtractor()
    p.feed(html)
    return "\n".join(p.parts)[:MAX_CHARS_PER_PAGE]


def fetch_image_b64(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    if len(data) > 4_000_000:
        raise ValueError(f"imagen muy grande ({len(data)} bytes)")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mt = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mt = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mt = "image/webp"
    else:
        raise ValueError("la respuesta no es una imagen reconocible")
    return base64.b64encode(data).decode(), mt


def gather_sources():
    """Hasta 2 articulos recientes de Chocale, con sus tablas-imagen (Datawrapper)."""
    sources, images = [], []
    tag_html = fetch(CHOCALE_TAG_URL)
    links = re.findall(r'href="(https://chocale\.cl/\d{4}/[^"]*bencina[^"]*)"', tag_html)
    seen = []
    for u in links:
        if u not in seen:
            seen.append(u)
        if len(seen) == 2:
            break
    for url in seen:
        try:
            html = fetch(url)
            sources.append((url, html_to_text(html)))
            print(f"OK fuente: {url}")
            for chart_id in dict.fromkeys(re.findall(r'datawrapper\.dwcdn\.net/([A-Za-z0-9]+)/', html)):
                if len(images) >= MAX_IMAGES:
                    break
                candidates = []
                m = re.search(r'src="([^"]*datawrapper\.dwcdn\.net/' + chart_id + r'/[^"]*)"', html)
                if m:
                    candidates.append(m.group(1).replace('&amp;', '&').replace('&#038;', '&'))
                candidates.append(f"https://datawrapper.dwcdn.net/{chart_id}/full.png")
                for img_url in candidates:
                    try:
                        images.append(fetch_image_b64(img_url))
                        print(f"OK tabla-imagen: {img_url}")
                        break
                    except Exception as e:
                        print(f"AVISO: no se pudo leer la imagen {img_url}: {e}")
        except Exception as e:
            print(f"AVISO: no se pudo leer {url}: {e}")
    if not sources:
        print("ERROR: ninguna fuente disponible")
        sys.exit(1)
    return sources, images


def call_claude(sources, images):
    system = (
        "Extraes descuentos de combustible en Chile desde texto de paginas web. "
        "Responde UNICAMENTE un array JSON, sin markdown ni texto adicional. "
        "Cada elemento: {\"card\": str (nombre del banco/tarjeta/convenio), "
        "\"cardType\": str (credito|debito|prepago|socio o combinaciones con '/'), "
        "\"brand\": str (solo Copec, Shell o Aramco; descarta otras marcas), "
        "\"discountPerLiter\": int (pesos por litro; si es un rango, usa el maximo), "
        "\"days\": array de int (0=domingo ... 6=sabado), "
        "\"app\": str (app o medio para activar el descuento), "
        "\"maxMonthly\": int o null (tope mensual en pesos si se indica), "
        "\"notes\": str (condiciones relevantes en una linea, o \"\")}. "
        "Las fuentes incluyen texto de articulos y una o mas IMAGENES con tablas. "
        "La tabla-imagen es la fuente PRINCIPAL y contiene mas descuentos que el texto: "
        "extrae TODAS las filas de la tabla, una por una, ademas de lo que aporte el texto. "
        "Incluye solo descuentos vigentes del mes indicado. Si una tarjeta aplica a "
        "varias marcas, crea una entrada por marca. No inventes montos: usa solo valores "
        "presentes en el texto o en las tablas adjuntas."
    )
    today = date.today()
    user = f"Mes actual: {today.strftime('%Y-%m')}. Fuentes:\n\n" + "\n\n".join(
        f"=== FUENTE: {url} ===\n{text}" for url, text in sources
    )
    if images:
        user += ("\n\nSe adjuntan ademas las tablas (imagenes) de las mismas fuentes con el "
                 "detalle completo; si la tabla y el texto difieren, manda la tabla.")
    content = [{"type": "text", "text": user}] + [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}
        for b64, mt in images
    ]
    payload = {
        "model": MODEL,
        "max_tokens": 8192,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
    text = "".join(b.get("text", "") for b in data.get("content", []))
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    usage = data.get("usage", {})
    print(f"Tokens: in={usage.get('input_tokens')} out={usage.get('output_tokens')}")
    return json.loads(text)


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def make_id(e):
    days = sorted(e["days"])
    d = "todos" if len(days) == 7 else "-".join(DAY_ABBR[x] for x in days)
    return f"{slug(e['card'])}-{e['brand'].lower()}-{d}"


def validate(raw):
    if not isinstance(raw, list):
        sys.exit("ERROR: el modelo no devolvio una lista")
    out = []
    for e in raw:
        try:
            brand = str(e["brand"]).strip()
            if brand not in VALID_BRANDS:
                continue
            disc = int(e["discountPerLiter"])
            days = sorted({int(d) for d in e["days"]})
            if not (50 <= disc <= 500) or not days or not set(days) <= VALID_DAYS:
                print(f"DESCARTADA (fuera de rango): {e.get('card')}")
                continue
            mm = e.get("maxMonthly")
            mm = int(mm) if mm not in (None, "", 0) else None
            if mm is not None and not (1000 <= mm <= 100000):
                mm = None
            entry = {
                "card": str(e["card"]).strip(),
                "cardType": str(e.get("cardType", "")).strip(),
                "brand": brand,
                "discountPerLiter": disc,
                "days": days,
                "app": str(e.get("app", "")).strip(),
                "maxMonthly": mm,
                "notes": str(e.get("notes", "")).strip()[:200],
            }
            entry = {"id": make_id(entry), **entry}
            out.append(entry)
        except (KeyError, TypeError, ValueError) as err:
            print(f"DESCARTADA (malformada): {err}")
    # dedup por id, conservando el mayor descuento
    byid = {}
    for e in out:
        if e["id"] not in byid or e["discountPerLiter"] > byid[e["id"]]["discountPerLiter"]:
            byid[e["id"]] = e
    out = sorted(byid.values(), key=lambda x: (min(x["days"]), x["id"]))
    if not (MIN_ENTRIES <= len(out) <= MAX_ENTRIES):
        sys.exit(f"ERROR: {len(out)} entradas validas; fuera del rango esperado "
                 f"[{MIN_ENTRIES}, {MAX_ENTRIES}]. No se escribe nada.")
    return out


def build_doc(discounts, source_urls):
    today = date.today()
    last_day = monthrange(today.year, today.month)[1]
    return {
        "lastUpdated": today.isoformat(),
        "validUntil": date(today.year, today.month, last_day).isoformat(),
        "source": "Extraccion automatica (Claude) desde: " + ", ".join(source_urls),
        "discounts": discounts,
    }


def diff_summary(old, new):
    o = {e["id"]: e for e in old.get("discounts", [])}
    n = {e["id"]: e for e in new["discounts"]}
    lines = ["## Cambios propuestos en descuentos", ""]
    added = [n[i] for i in n if i not in o]
    removed = [o[i] for i in o if i not in n]
    changed = [(o[i], n[i]) for i in n if i in o and o[i] != n[i]]
    if added:
        lines.append("**Nuevos:**")
        lines += [f"- {e['card']} / {e['brand']}: ${e['discountPerLiter']}/L dias {e['days']}"
                  for e in added]
    if removed:
        lines.append("\n**Eliminados:**")
        lines += [f"- {e['card']} / {e['brand']} (${e['discountPerLiter']}/L)" for e in removed]
    if changed:
        lines.append("\n**Modificados:**")
        for a, b in changed:
            fields = [k for k in b if a.get(k) != b.get(k)]
            lines.append(f"- {b['card']} / {b['brand']}: cambia {', '.join(fields)} "
                         f"(descuento {a['discountPerLiter']} -> {b['discountPerLiter']})")
    lines.append(f"\nVigencia propuesta: hasta {new['validUntil']}. "
                 "Revisar montos contra las fuentes antes de aprobar.")
    return "\n".join(lines)


def main():
    sources, images = gather_sources()
    raw = call_claude(sources, images)
    print(f"Entradas extraidas por el modelo: {len(raw)}")
    discounts = validate(raw)
    new_doc = build_doc(discounts, [u for u, _ in sources])

    old_doc = {}
    if os.path.exists("discounts.json"):
        with open("discounts.json", encoding="utf-8") as f:
            old_doc = json.load(f)

    # Si lo material no cambio, conservar la redaccion anterior (evita PRs de ruido)
    material_fields = ("card", "brand", "discountPerLiter", "days", "maxMonthly")
    old_by_id = {e["id"]: e for e in old_doc.get("discounts", [])}
    for e in new_doc["discounts"]:
        prev = old_by_id.get(e["id"])
        if prev and all(prev.get(k) == e.get(k) for k in material_fields):
            for k in ("cardType", "app", "notes"):
                e[k] = prev.get(k, e[k])

    same_data = old_doc.get("discounts") == new_doc["discounts"] \
        and old_doc.get("validUntil") == new_doc["validUntil"]
    if same_data:
        print("Sin cambios. No se escribe nada.")
        return

    with open("discounts.json", "w", encoding="utf-8") as f:
        json.dump(new_doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open("pr_body.md", "w", encoding="utf-8") as f:
        f.write(diff_summary(old_doc, new_doc))
    print(f"Cambios detectados: {len(discounts)} descuentos escritos.")


if __name__ == "__main__":
    main()