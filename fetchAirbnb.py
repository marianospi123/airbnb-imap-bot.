import imaplib
import email
from bs4 import BeautifulSoup
import re
from datetime import datetime
import requests
import time
import json
import os

# --- CONFIGURACIÓN ---
IMAP_HOST = "imap.gmail.com"
IMAP_USER = "huespedex.ve@gmail.com"
IMAP_PASS = os.getenv("IMAP_PASS")
WEBHOOK_URL = "https://airbnb-n8n-81gs.onrender.com/webhook/airbnb"

STATE_FILE = "last_ids.json"

# --------------------------
# Helpers
# --------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def clean_line(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def parse_monto(monto_str):
    if not monto_str:
        return 0.0

    monto = str(monto_str).strip().replace("\xa0", "")
    monto = monto.replace("US$", "").replace("$", "").replace("USD", "").strip()

    # Manejo formato español: 149,38 -> 149.38
    if "." in monto and "," in monto:
        monto = monto.replace(".", "").replace(",", ".")
    elif "," in monto and "." not in monto:
        monto = monto.replace(",", ".")
    elif monto.count(".") > 1:
        partes = monto.split(".")
        monto = "".join(partes[:-1]) + "." + partes[-1]

    try:
        return float(monto)
    except Exception:
        return 0.0

def safe_get_html(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if not payload:
                return None
            try:
                return payload.decode()
            except UnicodeDecodeError:
                return payload.decode("latin1", errors="ignore")
    return None

def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    return text.replace("\xa0", " ")

def post_to_n8n(payload):
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=45)
        return 200 <= r.status_code < 300, r.status_code
    except Exception as e:
        return False, str(e)

def compute_noches_ddmmyyyy(ci, co):
    try:
        d1 = datetime.strptime(ci, "%d/%m/%Y")
        d2 = datetime.strptime(co, "%d/%m/%Y")
        return max(0, (d2 - d1).days)
    except Exception:
        return 0

MESES_ABBR = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "sept": 9,
    "oct": 10, "nov": 11, "dic": 12
}

def parse_airbnb_short_date(s):
    """
    Soporta:
    - mar, 8 sept
    - mar, 8 sep
    - martes 9 de junio de 2026
    - 09/06/2026
    """
    if not s:
        return None

    s = str(s).lower().replace("\xa0", " ").strip()
    s = clean_line(s)

    # dd/mm/yyyy o d/m/yyyy
    m_full = re.search(r"([0-3]?\d)[/.-]([01]?\d)[/.-](\d{4})", s)
    if m_full:
        day = int(m_full.group(1))
        mon = int(m_full.group(2))
        year = int(m_full.group(3))
        return datetime(year, mon, day)

    # martes 9 de junio de 2026
    meses_full = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "setiembre": 9, "octubre": 10,
        "noviembre": 11, "diciembre": 12
    }

    m_long = re.search(
        r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})",
        s,
        re.IGNORECASE
    )
    if m_long:
        day = int(m_long.group(1))
        mon = meses_full.get(m_long.group(2))
        year = int(m_long.group(3))
        if mon:
            return datetime(year, mon, day)

    # mar, 8 sept / mar, 8 jun
    s = re.sub(r"^[a-záéíóúñ]{2,8}\s*,\s*", "", s)

    m = re.search(r"(\d{1,2})\s+([a-záéíóúñ]{3,4})\.?", s)
    if not m:
        return None

    day = int(m.group(1))
    mon_txt = m.group(2).strip(".")
    mon = MESES_ABBR.get(mon_txt)
    if not mon:
        return None

    now = datetime.now()
    year = now.year

    # Si el mes ya pasó mucho, asumimos año siguiente
    if mon < now.month - 6:
        year += 1

    return datetime(year, mon, day)

def format_airbnb_date_ddmmyyyy(raw_date):
    d = parse_airbnb_short_date(raw_date)
    if not d:
        return clean_line(raw_date)
    return d.strftime("%d/%m/%Y")

def compute_noches_from_airbnb_dates(ci_raw, co_raw):
    d1 = parse_airbnb_short_date(ci_raw)
    d2 = parse_airbnb_short_date(co_raw)
    if not d1 or not d2:
        return 0
    return max(0, (d2 - d1).days)

def get_lines(text):
    return [clean_line(x) for x in text.split("\n") if clean_line(x)]

def pick_first(*patterns, text="", flags=re.IGNORECASE):
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return clean_line(m.group(1))
    return ""

def get_value_after_label(lines, label_patterns, skip_time=True):
    """
    Busca un label exacto en líneas y devuelve la siguiente línea útil.
    Evita confundir 'detalles de la llegada...' con el bloque real 'Llegada'.
    """
    for i, line in enumerate(lines):
        for pat in label_patterns:
            if re.fullmatch(pat, line, re.IGNORECASE):
                for j in range(i + 1, min(i + 8, len(lines))):
                    candidate = clean_line(lines[j])
                    if not candidate:
                        continue

                    # Saltar horas tipo 16:00 / 11:00
                    if skip_time and re.fullmatch(r"\d{1,2}:\d{2}", candidate):
                        continue

                    # Saltar labels
                    if re.fullmatch(r"Llegada|Salida|Viajeros|Huéspedes", candidate, re.IGNORECASE):
                        continue

                    return candidate
    return ""

def find_money_after_keywords(text, keywords):
    """
    Busca montos cerca de etiquetas.
    Soporta:
    - $149,38
    - USD 149,38
    - US$149,38
    - 149,38 $
    - 149,38 USD
    """
    lines = get_lines(text)

    patterns = [
        # Símbolo antes
        r"US\$\s*([\d\.,]+)",
        r"\$\s*([\d\.,]+)",
        r"USD\s*([\d\.,]+)",

        # Símbolo después
        r"([\d\.,]+)\s*US\$",
        r"([\d\.,]+)\s*\$",
        r"([\d\.,]+)\s*USD",
    ]

    for i, line in enumerate(lines):
        line_low = line.lower()

        for kw in keywords:
            if kw.lower() in line_low:
                nearby = " ".join(lines[i:i+8])

                for p in patterns:
                    m = re.search(p, nearby, re.IGNORECASE)
                    if m:
                        amount = parse_monto(m.group(1))
                        if amount > 0:
                            return amount

    return 0.0

def find_all_money_values(text):
    values = []

    patterns = [
        r"US\$\s*([\d\.,]+)",
        r"\$\s*([\d\.,]+)",
        r"USD\s*([\d\.,]+)",
        r"([\d\.,]+)\s*US\$",
        r"([\d\.,]+)\s*\$",
        r"([\d\.,]+)\s*USD",
    ]

    for p in patterns:
        for m in re.finditer(p, text, re.IGNORECASE):
            amount = parse_monto(m.group(1))
            if amount > 0:
                values.append(amount)

    return values

def find_guest_from_airbnb(text, soup):
    huesped = ""

    tag = soup.find(string=re.compile(r"bienvenida a\s+([^\n\r,!.]+)", re.IGNORECASE))
    if tag:
        m = re.search(r"bienvenida a\s+([^\n\r,!.]+)", str(tag), re.IGNORECASE)
        if m:
            huesped = clean_line(m.group(1))

    if not huesped:
        huesped = pick_first(
            r"¡Nueva reserva confirmada!\s*([^\n\r]+?)\s+llega",
            r"Reserva confirmada:\s*([^\n\r]+?)\s+llega",
            r"dar la bienvenida a\s+([^\n\r,!.]+)",
            r"Recibe a\s+([^\n\r,!.]+)",
            text=text
        )

    return huesped

def find_apartment_from_airbnb(text):
    apartamento = pick_first(
        r"\n([^\n]+)\nCasa/apto\.?\s*entero",
        r"\n([^\n]+)\nApartamento\s*-\s*Casa/apto\.?\s*entero",
        r"\n([^\n]+)\nAlojamiento entero",
        r"\n([^\n]+)\nLoft",
        r"\n([^\n]+)\nApartamento",
        r"\n([^\n]+)\nCondominio",
        r"\n([^\n]+)\nVivienda rentada",
        r"Reserva de «([^»]+)»",
        r"Reserva confirmada\s*\n([^\n]+)",
        text=text
    )

    return apartamento

# --------------------------
# Parsers
# --------------------------

def parse_airbnb_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    text = html_to_text(html)
    lines = get_lines(text)

    huesped = find_guest_from_airbnb(text, soup)

    reserva = pick_first(
        r"Código de confirmación\s*([A-Z0-9]+)",
        r"Confirmación\s*#?\s*([A-Z0-9]+)",
        r"\b(HM[A-Z0-9]{7,12})\b",
        text=text
    )

    apartamento = find_apartment_from_airbnb(text)

    checkin_raw = get_value_after_label(lines, [r"Llegada", r"Check-in", r"Fecha de llegada"])
    checkout_raw = get_value_after_label(lines, [r"Salida", r"Check-out", r"Fecha de salida"])

    # Fallback si no consigue por labels
    if not parse_airbnb_short_date(checkin_raw) or not parse_airbnb_short_date(checkout_raw):
        dates_found = re.findall(
            r"(?:lun|mar|mié|mie|jue|vie|sáb|sab|dom),?\s+\d{1,2}\s+(?:ene|feb|mar|abr|may|jun|jul|ago|sep|set|sept|oct|nov|dic)",
            text,
            flags=re.IGNORECASE
        )

        if len(dates_found) >= 1 and not parse_airbnb_short_date(checkin_raw):
            checkin_raw = dates_found[0]
        if len(dates_found) >= 2 and not parse_airbnb_short_date(checkout_raw):
            checkout_raw = dates_found[1]

    checkin = format_airbnb_date_ddmmyyyy(checkin_raw)
    checkout = format_airbnb_date_ddmmyyyy(checkout_raw)

    noches = compute_noches_from_airbnb_dates(checkin_raw, checkout_raw)
    if noches == 0:
        m_noches = re.search(r"(\d+)\s+noch", text, re.IGNORECASE)
        if m_noches:
            noches = int(m_noches.group(1))

    viajeros = get_value_after_label(lines, [r"Viajeros", r"Huéspedes"], skip_time=False)
    if not viajeros:
        viajeros = pick_first(
            r"(\d+\s+adultos?(?:,\s*\d+\s+niños?)?(?:,\s*\d+\s+beb[eé])?)",
            text=text
        )

    # IMPORTANTE:
    # En la hoja usamos Total Pagado como lo que "Ganas" / cobro anfitrión.
    total_pagado = find_money_after_keywords(text, [
        "Ganas",
        "Ganás",
        "Cobro del anfitrión",
        "Tu ingreso",
        "Ingreso del anfitrión",
        "Ingresos del anfitrión",
        "Host payout",
        "You earn",
        "Your earnings",
        "Payout"
    ])

    # Si no aparece Ganas, usamos fallback a total pagado / total.
    if total_pagado <= 0:
        total_pagado = find_money_after_keywords(text, [
            "Total pagado",
            "Pago total",
            "Total (USD)",
            "Total"
        ])

    limpieza = find_money_after_keywords(text, [
        "Gastos de limpieza",
        "Tarifa de limpieza",
        "Limpieza",
        "Cleaning fee"
    ])

    return {
        "Canal": "Airbnb",
        "Huesped": huesped,
        "Reserva": reserva,
        "Checkin": checkin,
        "Checkout": checkout,
        "# noches": noches,
        "# huespedes": viajeros,
        "Total Pagado": total_pagado,
        "Limpieza": limpieza,
        "Apartamento": apartamento,
        "_debug_money_values": find_all_money_values(text),
    }

def parse_estei_from_html(html):
    text = html_to_text(html)

    m_res = re.search(r"reserva\s*#\s*(\d+)", text, re.IGNORECASE)
    reserva = m_res.group(1) if m_res else ""

    m_nom = re.search(r"Nombre:\s*([^\n\r]+)", text, re.IGNORECASE)
    huesped = m_nom.group(1).strip() if m_nom else ""

    m_apto = re.search(r"Alojamiento:\s*([^\n\r]+)", text, re.IGNORECASE)
    apartamento = m_apto.group(1).strip() if m_apto else ""

    m_ci = re.search(r"Fecha de check-in:\s*([0-3]\d/[01]\d/\d{4})", text, re.IGNORECASE)
    m_co = re.search(r"Fecha de check-out:\s*([0-3]\d/[01]\d/\d{4})", text, re.IGNORECASE)
    checkin = m_ci.group(1) if m_ci else ""
    checkout = m_co.group(1) if m_co else ""

    noches = compute_noches_ddmmyyyy(checkin, checkout)

    m_v = re.search(r"Cantidad de viajeros:\s*(\d+)", text, re.IGNORECASE)
    viajeros = m_v.group(1).strip() if m_v else ""

    m_total = re.search(r"Ingreso estimado:\s*\$?\s*([\d\.,]+)", text, re.IGNORECASE)
    total_pagado = parse_monto(m_total.group(1)) if m_total else 0.0

    return {
        "Canal": "Estei",
        "Huesped": huesped,
        "Reserva": reserva,
        "Checkin": checkin,
        "Checkout": checkout,
        "# noches": noches,
        "# huespedes": viajeros,
        "Total Pagado": total_pagado,
        "Limpieza": 0.0,
        "Apartamento": apartamento,
    }

# --------------------------
# IMAP UID helpers
# --------------------------

def uid_search(mail, criteria):
    status, data = mail.uid("search", None, criteria)
    if status != "OK":
        return []
    uids = data[0].split()
    return [int(u) for u in uids]

def uid_fetch_msg(mail, uid_int):
    status, data = mail.uid("fetch", str(uid_int), "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return None
    raw = data[0][1]
    return email.message_from_bytes(raw)

# --------------------------
# Payload helpers
# --------------------------

def clean_payload_for_send(datos):
    payload = dict(datos)
    payload.pop("_debug_money_values", None)
    return payload

def should_send_payload(datos, label, uid_int):
    if not datos.get("Reserva"):
        print(f"⚠️ {label}: parse incompleto en UID {uid_int} -> {datos} (no avanzo last_uid).")
        return False

    if not datos.get("Apartamento"):
        print(f"⚠️ {label}: UID {uid_int} sin apartamento. Envío igual. Reserva={datos.get('Reserva')}")

    # Regla crítica: Airbnb no debe enviarse con total 0.
    # Eso fue lo que dañó la hoja.
    if datos.get("Canal") == "Airbnb" and float(datos.get("Total Pagado") or 0) <= 0:
        print(
            f"⚠️ Airbnb: UID {uid_int} Reserva={datos.get('Reserva')} "
            f"sin monto válido. NO se envía para evitar guardar Total Pagado=0. "
            f"Montos detectados={datos.get('_debug_money_values')}"
        )
        return False

    return True

# --------------------------
# Bootstrap + processing
# --------------------------

def bootstrap_last_uid(mail, state, key, criteria, label):
    """
    Si no existe last_uid, lo inicializa al UID más reciente y NO envía nada.
    """
    if key in state and isinstance(state[key], int):
        return

    uids = uid_search(mail, criteria)
    if not uids:
        print(f"ℹ️ {label}: no hay correos para bootstrap.")
        return

    state[key] = max(uids)
    save_state(state)
    print(f"✅ {label}: bootstrap listo. last_uid={state[key]} (no se envió nada)")

def process_new_since(mail, state, key, criteria, parser_fn, label):
    """
    Procesa solo UIDs > last_uid guardado.
    """
    last_uid = state.get(key)
    if not isinstance(last_uid, int):
        print(f"⚠️ {label}: last_uid no inicializado.")
        return

    uids = uid_search(mail, criteria)
    if not uids:
        print(f"ℹ️ {label}: no hay correos que cumplan filtro.")
        return

    new_uids = sorted([u for u in uids if u > last_uid])
    if not new_uids:
        print(f"✅ {label}: no hay nuevos (last_uid={last_uid}).")
        return

    print(f"📩 {label}: nuevos={len(new_uids)} (desde last_uid={last_uid}).")

    for uid_int in new_uids:
        msg = uid_fetch_msg(mail, uid_int)
        if not msg:
            print(f"⚠️ {label}: no pude fetch UID {uid_int}")
            continue

        html = safe_get_html(msg)
        if not html:
            print(f"⚠️ {label}: sin HTML en UID {uid_int} (no avanzo last_uid).")
            continue

        datos = parser_fn(html)

        print(
            f"🔎 {label} UID {uid_int}: "
            f"Reserva={datos.get('Reserva')} | "
            f"Apto={datos.get('Apartamento')} | "
            f"CI={datos.get('Checkin')} | "
            f"CO={datos.get('Checkout')} | "
            f"Total={datos.get('Total Pagado')} | "
            f"Limpieza={datos.get('Limpieza')}"
        )

        if not should_send_payload(datos, label, uid_int):
            # No avanzamos last_uid para que se pueda revisar/reprocesar.
            continue

        payload = clean_payload_for_send(datos)

        ok, info = post_to_n8n(payload)
        if ok:
            state[key] = uid_int
            save_state(state)
            print(f"✅ {label}: enviado UID {uid_int} (HTTP {info}) Reserva={datos.get('Reserva')}")
        else:
            print(f"⚠️ {label}: fallo enviando UID {uid_int} ({info}). Detengo para reintentar.")
            break

def report_pending(mail, state, key, criteria, parser_fn, label, limit=50):
    last_uid = state.get(key)
    if not isinstance(last_uid, int):
        print(f"⚠️ {label}: last_uid no inicializado.")
        return

    uids = uid_search(mail, criteria)
    pending = sorted([u for u in uids if u > last_uid])

    print(f"\n📌 {label} PENDIENTES")
    print(f"last_uid guardado = {last_uid}")
    print(f"pendientes (UID > last_uid) = {len(pending)}")

    sample = pending[:limit]
    for uid_int in sample:
        msg = uid_fetch_msg(mail, uid_int)
        html = safe_get_html(msg) if msg else None
        if not html:
            print(f" - UID {uid_int}: (sin html / fetch fail)")
            continue

        datos = parser_fn(html)
        print(
            f" - UID {uid_int}: "
            f"Reserva={datos.get('Reserva')} | "
            f"Apto={datos.get('Apartamento')} | "
            f"CI={datos.get('Checkin')} | "
            f"CO={datos.get('Checkout')} | "
            f"Total={datos.get('Total Pagado')} | "
            f"Limpieza={datos.get('Limpieza')}"
        )

def main():
    if not IMAP_PASS:
        raise RuntimeError("Falta IMAP_PASS en variables de entorno.")

    state = load_state()

    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(IMAP_USER, IMAP_PASS)
    mail.select("inbox")

    airbnb_criteria = '(FROM "automated@airbnb.com")'
    estei_criteria = '(FROM "noreply@estei.app")'

    bootstrap_last_uid(mail, state, "airbnb_last_uid", airbnb_criteria, "Airbnb")
    bootstrap_last_uid(mail, state, "estei_last_uid", estei_criteria, "Estei")

    process_new_since(mail, state, "airbnb_last_uid", airbnb_criteria, parse_airbnb_from_html, "Airbnb")
    process_new_since(mail, state, "estei_last_uid", estei_criteria, parse_estei_from_html, "Estei")

    mail.logout()

if __name__ == "__main__":
    while True:
        print("\n🔄 Ejecutando fetch (Airbnb + Estei) ...")
        try:
            main()
        except Exception as e:
            print(f"❌ Error en ciclo principal: {e}")
        print("⏳ Esperando 5 minutos...\n")
        time.sleep(300)