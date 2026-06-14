import imaplib
import email
from email.header import decode_header
from bs4 import BeautifulSoup
import re
from datetime import datetime
import requests
import json
import os

# --- CONFIGURACIÓN ---
IMAP_HOST = "imap.gmail.com"
IMAP_USER = "huespedex.ve@gmail.com"

# Para prueba rápida, coloca tu App Password aquí.
# Mejor luego volver a variable de entorno.
IMAP_PASS = "nyqy xcnc eaak czpp"

WEBHOOK_URL = "https://airbnb-n8n-81gs.onrender.com/webhook/airbnb"

TARGET_MONTH = 6
TARGET_YEAR = 2026

# IMPORTANTE:
# Si tu webhook AGREGA filas nuevas, puedes duplicar reservas ya corregidas.
# Si tu webhook ACTUALIZA por Reserva / ID_UNICO, no hay problema.
SEND_TO_WEBHOOK = True

MESES_ABBR = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
    "jul": 7, "ago": 8, "sep": 9, "set": 9, "sept": 9,
    "oct": 10, "nov": 11, "dic": 12
}

MESES_FULL = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12
}


def clean_line(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def html_to_text(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    return text.replace("\xa0", " ")


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


def get_subject(msg):
    subject = msg.get("Subject", "")

    try:
        decoded = decode_header(subject)
        parts = []

        for part, enc in decoded:
            if isinstance(part, bytes):
                parts.append(part.decode(enc or "utf-8", errors="ignore"))
            else:
                parts.append(part)

        return "".join(parts)
    except Exception:
        return subject


def parse_monto(monto_str):
    if not monto_str:
        return 0.0

    monto = str(monto_str).strip().replace("\xa0", "")
    monto = monto.replace("US$", "").replace("$", "").replace("USD", "").strip()

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


def get_lines(text):
    return [clean_line(x) for x in text.split("\n") if clean_line(x)]


def pick_first(text, *patterns, flags=re.IGNORECASE):
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return clean_line(m.group(1))
    return ""


def parse_airbnb_short_date(s):
    if not s:
        return None

    s = clean_line(str(s).lower().replace("\xa0", " "))

    # 09/06/2026
    m_full = re.search(r"([0-3]?\d)[/.-]([01]?\d)[/.-](\d{4})", s)
    if m_full:
        return datetime(
            int(m_full.group(3)),
            int(m_full.group(2)),
            int(m_full.group(1))
        )

    # martes 9 de junio de 2026
    m_long = re.search(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})", s)
    if m_long:
        day = int(m_long.group(1))
        mon = MESES_FULL.get(m_long.group(2))
        year = int(m_long.group(3))
        if mon:
            return datetime(year, mon, day)

    # mar, 8 sept
    s = re.sub(r"^[a-záéíóúñ]{2,8}\s*,\s*", "", s)
    m = re.search(r"(\d{1,2})\s+([a-záéíóúñ]{3,4})\.?", s)
    if not m:
        return None

    day = int(m.group(1))
    mon = MESES_ABBR.get(m.group(2).strip("."))
    if not mon:
        return None

    now = datetime.now()
    year = now.year

    if mon < now.month - 6:
        year += 1

    return datetime(year, mon, day)


def format_date(raw):
    d = parse_airbnb_short_date(raw)
    if not d:
        return clean_line(raw)
    return d.strftime("%d/%m/%Y")


def compute_noches(ci_raw, co_raw):
    d1 = parse_airbnb_short_date(ci_raw)
    d2 = parse_airbnb_short_date(co_raw)
    if not d1 or not d2:
        return 0
    return max(0, (d2 - d1).days)


def get_value_after_label(lines, label_patterns, skip_time=True):
    for i, line in enumerate(lines):
        for pat in label_patterns:
            if re.fullmatch(pat, line, re.IGNORECASE):
                for j in range(i + 1, min(i + 8, len(lines))):
                    candidate = clean_line(lines[j])

                    if not candidate:
                        continue

                    if skip_time and re.fullmatch(r"\d{1,2}:\d{2}", candidate):
                        continue

                    if re.fullmatch(r"Llegada|Salida|Viajeros|Huéspedes", candidate, re.IGNORECASE):
                        continue

                    return candidate
    return ""


def find_money_after_keywords(text, keywords):
    lines = get_lines(text)

    patterns = [
        r"US\$\s*([\d\.,]+)",
        r"\$\s*([\d\.,]+)",
        r"USD\s*([\d\.,]+)",
        r"([\d\.,]+)\s*US\$",
        r"([\d\.,]+)\s*\$",
        r"([\d\.,]+)\s*USD",
    ]

    for i, line in enumerate(lines):
        line_low = line.lower()

        for kw in keywords:
            if kw.lower() in line_low:
                nearby = " ".join(lines[i:i + 8])

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


def parse_airbnb_from_html(html, subject=""):
    soup = BeautifulSoup(html, "html.parser")
    text = html_to_text(html)
    lines = get_lines(text)

    reserva = pick_first(
        text,
        r"Código de confirmación\s*([A-Z0-9]+)",
        r"Confirmación\s*#?\s*([A-Z0-9]+)",
        r"\b(HM[A-Z0-9]{7,12})\b"
    )

    huesped = pick_first(
        text,
        r"¡Nueva reserva confirmada!\s*([^\n\r]+?)\s+llega",
        r"Reserva confirmada:\s*([^\n\r]+?)\s+llega",
        r"dar la bienvenida a\s+([^\n\r,!.]+)"
    )

    if not huesped and subject:
        huesped = pick_first(
            subject,
            r"Reserva confirmada:\s*([^\n\r]+?)\s+llega"
        )

    apartamento = pick_first(
        text,
        r"\n([^\n]+)\nCasa/apto\.?\s*entero",
        r"\n([^\n]+)\nApartamento\s*-\s*Casa/apto\.?\s*entero",
        r"Reserva de «([^»]+)»",
        r"Reserva confirmada\s*\n([^\n]+)"
    )

    checkin_raw = get_value_after_label(lines, [r"Llegada", r"Check-in", r"Fecha de llegada"])
    checkout_raw = get_value_after_label(lines, [r"Salida", r"Check-out", r"Fecha de salida"])

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

    checkin = format_date(checkin_raw)
    checkout = format_date(checkout_raw)

    noches = compute_noches(checkin_raw, checkout_raw)

    if noches == 0:
        m_noches = re.search(r"(\d+)\s+noch", text, re.IGNORECASE)
        if m_noches:
            noches = int(m_noches.group(1))

    viajeros = get_value_after_label(lines, [r"Viajeros", r"Huéspedes"], skip_time=False)

    if not viajeros:
        viajeros = pick_first(
            text,
            r"(\d+\s+adultos?(?:,\s*\d+\s+niños?)?(?:,\s*\d+\s+beb[eé])?)"
        )

    # Para tu hoja: Total Pagado = Ganas / ingreso anfitrión
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

    # Fallback si no aparece Ganas
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
        "_subject": subject,
    }


def uid_search(mail, criteria):
    status, data = mail.uid("search", None, criteria)
    if status != "OK":
        return []
    return [int(u) for u in data[0].split()]


def uid_fetch_msg(mail, uid_int):
    status, data = mail.uid("fetch", str(uid_int), "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return None
    return email.message_from_bytes(data[0][1])


def clean_payload(datos):
    payload = dict(datos)
    payload.pop("_debug_money_values", None)
    payload.pop("_subject", None)
    return payload


def post_to_n8n(payload):
    payload = clean_payload(payload)
    r = requests.post(WEBHOOK_URL, json=payload, timeout=45)
    return 200 <= r.status_code < 300, r.status_code, r.text[:500]


def is_june_2026(datos):
    d = parse_airbnb_short_date(datos.get("Checkin"))
    return bool(d and d.month == TARGET_MONTH and d.year == TARGET_YEAR)


def valid_airbnb_payload(datos):
    return (
        datos.get("Reserva")
        and datos.get("Apartamento")
        and parse_airbnb_short_date(datos.get("Checkin"))
        and parse_airbnb_short_date(datos.get("Checkout"))
        and float(datos.get("Total Pagado") or 0) > 0
    )


def select_all_mail(mail):
    """
    Intenta seleccionar Todos / All Mail. Si falla, usa inbox.
    """
    for mailbox in ['"[Gmail]/All Mail"', '"[Gmail]/Todos"', "inbox"]:
        status, _ = mail.select(mailbox)
        if status == "OK":
            print(f"📬 Buzón seleccionado: {mailbox}")
            return mailbox

    raise RuntimeError("No pude seleccionar inbox ni All Mail.")


def replay_junio(mail):
    print("\n🔁 Buscando y enviando TODAS las reservas Airbnb con check-in en junio 2026...\n")

    # Buscamos correos recibidos desde mayo hasta julio para agarrar reservas de junio
    # que pudieron haber sido confirmadas antes o después.
    criteria = '(FROM "automated@airbnb.com" SINCE "01-May-2026" BEFORE "01-Jul-2026")'
    uids = uid_search(mail, criteria)

    if not uids:
        print("❌ No encontré correos Airbnb en ese rango.")
        return

    print(f"🔎 Correos Airbnb encontrados en rango: {len(uids)}")

    enviados = set()
    candidatos = []
    sin_monto = []
    fuera_junio = []

    for uid in sorted(uids):
        msg = uid_fetch_msg(mail, uid)

        if not msg:
            continue

        subject = get_subject(msg)
        html = safe_get_html(msg)

        if not html:
            continue

        datos = parse_airbnb_from_html(html, subject=subject)

        if not datos.get("Reserva"):
            continue

        # Evitar procesar recordatorios/correos repetidos de la misma reserva sin monto
        if datos["Reserva"] in enviados:
            continue

        if not is_june_2026(datos):
            fuera_junio.append(datos.get("Reserva"))
            continue

        print(
            f"\n📌 UID {uid} | {datos.get('Reserva')} | "
            f"CI={datos.get('Checkin')} | CO={datos.get('Checkout')} | "
            f"Total={datos.get('Total Pagado')} | Limpieza={datos.get('Limpieza')} | "
            f"Apto={datos.get('Apartamento')}"
        )

        if not valid_airbnb_payload(datos):
            sin_monto.append(datos)
            print(f"⚠️ No envío {datos.get('Reserva')}: falta monto/apto/fecha válida. Montos={datos.get('_debug_money_values')}")
            continue

        candidatos.append((uid, datos))
        enviados.add(datos["Reserva"])

    print("\n==============================")
    print("📊 RESUMEN PREVIO")
    print("==============================")
    print(f"Reservas listas para enviar: {len(candidatos)}")
    print(f"Reservas de junio sin monto válido: {len(sin_monto)}")
    print("==============================\n")

    for uid, datos in candidatos:
        payload = clean_payload(datos)

        print("\n📤 Enviando:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        if not SEND_TO_WEBHOOK:
            print("🧪 DRY RUN: no se envió porque SEND_TO_WEBHOOK=False")
            continue

        ok, status, response = post_to_n8n(datos)

        if ok:
            print(f"✅ Enviada {datos.get('Reserva')} UID {uid} HTTP {status}")
        else:
            print(f"❌ Falló {datos.get('Reserva')} UID {uid}: HTTP/status {status}")
            if response:
                print(response)

    if sin_monto:
        print("\n⚠️ Reservas de junio encontradas pero NO enviadas por monto inválido:")
        for d in sin_monto:
            print(
                f" - {d.get('Reserva')} | CI={d.get('Checkin')} | "
                f"Total={d.get('Total Pagado')} | Limpieza={d.get('Limpieza')} | "
                f"Montos={d.get('_debug_money_values')} | Subject={d.get('_subject')}"
            )


def main():
    if not IMAP_PASS or IMAP_PASS == "PEGA_AQUI_TU_APP_PASSWORD":
        raise RuntimeError("Falta colocar IMAP_PASS / App Password de Gmail.")

    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(IMAP_USER, IMAP_PASS)

    select_all_mail(mail)
    replay_junio(mail)

    mail.logout()


if __name__ == "__main__":
    main()