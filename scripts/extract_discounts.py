#!/usr/bin/env python3
"""Extrae descuentos de bencina (Chile) desde Chocale con Claude y actualiza
discounts.json si hay cambios materiales. Pensado para GitHub Actions.

Version consolidada. Incluye:
- Solo articulos del mes corriente (fallback: el mas reciente) para evitar
  contaminacion de meses anteriores.
- Tablas Datawrapper: intenta el CSV de datos; si no, rebana la imagen en
  franjas legibles (requiere Pillow) y las pasa como vision.
- Prompt con: nombres cortos canonicos (anclados al JSON vigente), maximo de
  rangos, omision de porcentajes y de beneficios de una sola vez, campo fuel.
- temperature=0 para minimizar variacion entre corridas.
- Anti-ruido: si lo material no cambio, conserva la redaccion anterior;
  corridas sin cambios reales no escriben nada.

Salida:
  - exit 0 sin tocar archivos -> sin cambios
  - exit 0 con discounts.json y pr_body.md reescritos -> hay cambios
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
MAX_ARTICLES = 2
MAX_IMAGE_PARTS = 12      # franjas de imagen en total (una tabla alta se rebana)
STRIP_PX, OVERLAP_PX = 1300, 80
VALID_BRANDS = {"Copec", "Shell", "Aramco"}
VALID_FUELS = {"93", "95", "97", "diesel"}
VALID_DAYS = set(range(7))
MIN_ENTRIES = 8           # tipicamente hay 25-35; menos de 8 = extraccion degenerada
MAX_ENTRIES = 60
MATERIAL_FIELDS = ("card", "brand", "fuel", "discountPerLiter", "days", "maxMonthly")
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


def fetch_image_parts(url):
    """Descarga una imagen y la rebana en franjas horizontales legibles."""
    import io
    from PIL import Image
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    if len(data) > 8_000_000:
        raise ValueError(f"imagen muy grande ({len(data)} bytes)")
    img = Image.open(io.BytesIO(data))
    img.load()
    w, h = img.size
    parts, top = [], 0
    while top < h:
        bottom = min(h, top + STRIP_PX)
        buf = io.BytesIO()
        img.crop((0, top, w, bottom)).convert("RGB").save(buf, format="PNG")
        parts.append(base64.b64encode(buf.getvalue()).decode())
        if bottom >= h:
            break
        top = bottom - OVERLAP_PX  # traslape para no cortar filas
    return parts


def pick_article_links(tag_html):
    """Articulos de bencina: prioriza los del mes corriente; fallback, el mas reciente."""
    links = list(dict.fromkeys(
        re.findall(r'href="(https://chocale\.cl/\d{4}/\d{2}/[^"]*bencina[^"]*)"', tag_html)
    ))
    ym = date.today().strftime("/%Y/%m/")
    current = [u for u in links if ym in u]
    chosen = current[:MAX_ARTICLES] if current else links[:1]
    if current:
        print(f"Articulos del mes corriente: {len(current)}")
    else:
        print("AVISO: ningun articulo del mes corriente; usando el mas reciente")
    return chosen


def gather_sources():
    """Articulos elegidos de Chocale + sus tablas (CSV si existe; si no, imagen rebanada)."""
    sources, images = [], []
    chosen = pick_article_links(fetch(CHOCALE_TAG_URL))
    for url in chosen:
        try:
            html = fetch(url)
            sources.append((url, html_to_text(html)))
            print(f"OK fuente: {url}")
        except Exception as e:
            print(f"AVISO: no se pudo leer {url}: {e}")
            continue
        for chart_id in dict.fromkeys(re.findall(r'datawrapper\.dwcdn\.net/([A-Za-z0-9]+)/', html)):
            csv_url = f"https://datawrapper.dwcdn.net/{chart_id}/dataset.csv"
            try:
                csv_text = fetch(csv_url)
                if csv_text.count("\n") >= 3 and "<html" not in csv_text[:200].lower():
                    sources.append((csv_url, csv_text[:MAX_CHARS_PER_PAGE]))
                    print(f"OK tabla-csv: {csv_url}")
                    continue
            except Exception:
                pass
            if len(images) >= MAX_IMAGE_PARTS:
                break
            candidates = []
            m = re.search(r'src="([^"]*datawrapper\.dwcdn\.net/' + chart_id + r'/[^"]*)"', html)
            if m:
                candidates.append(m.group(1).replace('&amp;', '&').replace('&#038;', '&'))
            candidates.append(f"https://datawrapper.dwcdn.net/{chart_id}/full.png")
            for img_url in candidates:
                try:
                    parts = fetch_image_parts(img_url)
                    for b64 in parts[:MAX_IMAGE_PARTS - len(images)]:
                        images.append((b64, "image/png"))
                    print(f"OK tabla-imagen: {img_url} ({len(parts)} franjas)")
                    break
                except Exception as e:
                    print(f"AVISO: no se pudo leer la imagen {img_url}: {e}")
    if not sources:
        print("ERROR: ninguna fuente disponible")
        sys.exit(1)
    return sources, images


def call_claude(sources, images, current):
    system = (
        "Extraes descuentos de combustible en Chile desde texto de paginas web. "
        "Responde UNICAMENTE un array JSON, sin markdown ni texto adicional. "
        "Cada elemento: {\"card\": str (nombre CORTO del emisor o convenio, ej. 'Banco Security', "
        "'Lider Bci', 'Mercado Pago', 'Cencosud Scotiabank'; NUNCA prefijos como 'Tarjeta de "
        "credito de' — el tipo de tarjeta va en cardType), "
        "\"cardType\": str (credito|debito|prepago|socio o combinaciones con '/'), "
        "\"brand\": str (solo Copec, Shell o Aramco; descarta otras marcas), "
        "\"fuel\": str o null (SOLO si el descuento aplica a un unico combustible: "
        "'93'|'95'|'97'|'diesel'; null si aplica a todos), "
        "\"discountPerLiter\": int (pesos por litro; si es un rango, usa SIEMPRE el maximo, "
        "ej: 'entre $50 y $300' => 300; si el descuento esta expresado en PORCENTAJE, OMITE "
        "la entrada completa — nunca lo conviertas a pesos), "
        "\"days\": array de int (0=domingo ... 6=sabado), "
        "\"app\": str (app o medio para activar el descuento), "
        "\"maxMonthly\": int o null (tope mensual en pesos si se indica), "
        "\"notes\": str (condiciones relevantes en una linea, o \"\")}. "
        "Las fuentes incluyen texto de articulos, datos CSV de tablas y/o IMAGENES con tablas. "
        "La tabla (CSV o imagen) es la fuente PRINCIPAL y contiene mas descuentos que el texto: "
        "extrae TODAS sus filas, una por una, ademas de lo que aporte el texto. "
        "Incluye solo descuentos vigentes del mes indicado. OMITE beneficios de una sola "
        "vez o no recurrentes (primera compra, nuevos usuarios, cumpleanos, referidos). "
        "Si una tarjeta aplica a varias marcas, crea una entrada por marca. No inventes "
        "montos: usa solo valores presentes en el texto o en las tablas adjuntas."
    )
    today = date.today()
    user = f"Mes actual: {today.strftime('%Y-%m')}. Fuentes:\n\n" + "\n\n".join(
        f"=== FUENTE: {url} ===\n{text}" for url, text in sources
    )
    if current:
        ref = [{"card": e["card"], "brand": e["brand"], "days": e["days"]} for e in current]
        user += ("\n\nNOMBRES CANONICOS ya usados por la app: "
                 + json.dumps(ref, ensure_ascii=False)
                 + "\nSi un beneficio de las fuentes corresponde a uno de esta lista (misma "
                 "tarjeta/convenio y marca), usa EXACTAMENTE el mismo valor de 'card', aunque "
                 "la fuente lo redacte distinto. Nombres nuevos solo para beneficios nuevos.")
    if images:
        user += ("\n\nSe adjuntan ademas las tablas (imagenes) de las mismas fuentes con el "
                 "detalle completo; si la tabla y el texto difieren, manda la tabla. Una tabla "
                 "larga puede venir cortada en varias imagenes consecutivas: leelas como una sola.")
    content = [{"type": "text", "text": user}] + [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}}
        for b64, mt in images
    ]
    payload = {
        "model": MODEL,
        "max_tokens": 8192,
        "temperature": 0,
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
    base = f"{slug(e['card'])}-{e['brand'].lower()}"
    if e.get("fuel"):
        base += f"-{e['fuel']}"
    return f"{base}-{d}"


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
            fuel = e.get("fuel")
            fuel = str(fuel).strip().lower() if fuel not in (None, "") else None
            if fuel not in VALID_FUELS:
                fuel = None
            mm = e.get("maxMonthly")
            mm = int(mm) if mm not in (None, "", 0) else None
            if mm is not None and not (1000 <= mm <= 100000):
                mm = None
            entry = {
                "card": str(e["card"]).strip(),
                "cardType": str(e.get("cardType", "")).strip(),
                "brand": brand,
                "fuel": fuel,
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


def preserve_wording(old_doc, new_doc):
    """Si lo material no cambio, conservar la redaccion anterior (evita PRs de ruido)."""
    old_by_id = {e["id"]: e for e in old_doc.get("discounts", [])}
    for e in new_doc["discounts"]:
        prev = old_by_id.get(e["id"])
        if prev and all(prev.get(k) == e.get(k) for k in MATERIAL_FIELDS):
            for k in ("cardType", "app", "notes"):
                e[k] = prev.get(k, e[k])


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
    old_doc = {}
    if os.path.exists("discounts.json"):
        with open("discounts.json", encoding="utf-8") as f:
            old_doc = json.load(f)

    sources, images = gather_sources()
    raw = call_claude(sources, images, old_doc.get("discounts", []))
    print(f"Entradas extraidas por el modelo: {len(raw)}")
    discounts = validate(raw)
    new_doc = build_doc(discounts, [u for u, _ in sources])
    preserve_wording(old_doc, new_doc)

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
