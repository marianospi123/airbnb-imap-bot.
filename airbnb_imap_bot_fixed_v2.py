import imaplib
import email
from email.header import decode_header
from bs4 import BeautifulSoup
import re
from datetime import datetime
import requests
import time
import json
import os

# =========================================================
# BOT RESERVAS AIRBNB + ESTEI - PRODUCCION
# =========================================================
# Cambios principales:
# - Airbnb: soporta montos tipo $149.38 y 149,38 $
# - Airbnb: prioriza Total (USD) como ingreso neto del anfitrion
# - Estei: soporta "Ingreso estimado: $252.2"
# - No envia Airbnb/Estei si Total Pagado queda en 0
# - Ignora canceladas, rechazadas, vencidas y recordatorios
# - Usa state por UID para no reprocesar correos antiguos
# - Si un correo no es valido, avanza UID para evitar que bloquee el bot
# =========================================================

# --- CONFIGURACION ---
IMAP_HOST = "imap.gmail.com"
IMAP_USER = "huespedex.ve@gmail.com"
IMAP_PASS = os.getenv("IMAP_PASS", "")

WEBHOOK_URL = os.getenv(
    "RESERVAS_WEBHOOK_URL",
    "https://airbnb-n8n-81gs.onrender.com/webhook/airbnb"
)

STATE_FILE = os.getenv("STATE_FILE", "last_ids.json")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))

# En produccion debe quedar True.
SEND_TO_WEBHOOK = os.getenv("SEND_TO_WEBHOOK", "true").lower() == "true"

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

# =========================================================
# HELPERS GENERALES
# =========================================================

def log(msg):
    print(msg, flush=True)


def clean_line(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def normalize_text(s):
    s = str(s or "").lower()
    s = s.replace("\xa0", " ")
    s = s.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    s = s.replace("ñ", "n")
    return clean_line(s)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


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


def safe_get_html_or_text(msg):
    html = None
    plain = None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                decoded = payload.decode()
            except UnicodeDecodeError:
                decoded = payload.decode("latin1", errors="ignore")

            if ctype == "text/html" and not html:
                html = decoded
            elif ctype == "text/plain" and not plain:
                plain = decoded
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                decoded = payload.decode()
            except UnicodeDecodeError:
                decoded = payload.decode("latin1", errors="ignore")

            if msg.get_content_type() == "text/html":
                html = decoded
            else:
                plain = decoded

    return html or plain


def html_to_text(content):
    if not content:
        return ""
    if "<html" in content.lower() or "<body" in content.lower() or "</" in content:
        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text(separator="\n")
    else:
        text = content
    return text.replace("\xa0", " ")


def get_lines(text):
    return [clean_line(x) for x in text.split("\n") if clean_line(x)]


def pick_first(text, *patterns, flags=re.IGNORECASE):
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return clean_line(m.group(1))
    return ""


def parse_monto(monto_str):
    if not monto_str:
        return 0.0

    monto = str(monto_str).strip().replace("\xa0", "")
    monto = monto.replace("US$", "").replace("USD", "").replace("$", "").strip()
    monto = monto.replace(" ", "")

    # 1.234,56 => 1234.56
    if "." in monto and "," in monto:
        monto = monto.replace(".", "").replace(",", ".")
    # 149,38 => 149.38
    elif "," in monto and "." not in monto:
        monto = monto.replace(",", ".")
    # 1.234.56 => 1234.56
    elif monto.count(".") > 1:
        partes = monto.split(".")
        monto = "".join(partes[:-1]) + "." + partes[-1]

    try:
        return float(monto)
    except Exception:
        return 0.0


def extract_money_from_line(line):
    """
    Extrae el primer monto monetario de una sola linea.
    Soporta: $457.99, US$457.99, USD 457.99, 457,99 $, etc.
    """
    patterns = [
        r"US\$\s*([\d\.,]+)",
        r"\$\s*([\d\.,]+)",
        r"USD\s*([\d\.,]+)",
        r"([\d\.,]+)\s*US\$",
        r"([\d\.,]+)\s*\$",
        r"([\d\.,]+)\s*USD",
    ]

    for pattern in patterns:
        m = re.search(pattern, line or "", re.IGNORECASE)
        if m:
            amount = parse_monto(m.group(1))
            if amount > 0:
                return amount

    return 0.0


def find_money_after_exact_label(text, labels, max_next_lines=3):
    """
    Busca una etiqueta exacta y toma el primer monto asociado a esa etiqueta.

    Ejemplo Airbnb:
        Total (USD)
        $457.99

    Devuelve 457.99 y evita confundirlo con "Precio total de la estadia".
    """
    lines = get_lines(text)
    normalized_labels = [normalize_text(label).rstrip(":") for label in labels]

    for i, line in enumerate(lines):
        line_normalized = normalize_text(line)
        line_without_colon = line_normalized.rstrip(":")

        for label in normalized_labels:
            # Etiqueta sola: Total (USD)
            exact_match = line_without_colon == label

            # Etiqueta y monto en la misma linea: Total (USD): $457.99
            same_line_match = (
                line_normalized.startswith(label + ":")
                or line_normalized.startswith(label + " $")
                or line_normalized.startswith(label + " us$")
                or line_normalized.startswith(label + " usd")
            )

            if not exact_match and not same_line_match:
                continue

            # Primero revisa si el monto esta en la misma linea.
            amount = extract_money_from_line(line)
            if amount > 0:
                return amount

            # Si no, toma el PRIMER monto de las siguientes lineas.
            for j in range(i + 1, min(i + 1 + max_next_lines, len(lines))):
                amount = extract_money_from_line(lines[j])
                if amount > 0:
                    return amount

    return 0.0


def find_money_after_prefix_label(text, labels, max_next_lines=3):
    """Busca un monto despues de una etiqueta que puede tener un sufijo."""
    lines = get_lines(text)
    normalized_labels = [normalize_text(label).rstrip(":") for label in labels]

    for i, line in enumerate(lines):
        line_normalized = normalize_text(line).rstrip(":")
        if not any(
            line_normalized == label or line_normalized.startswith(label + " ")
            for label in normalized_labels
        ):
            continue

        amount = extract_money_from_line(line)
        if amount > 0:
            return amount

        for j in range(i + 1, min(i + 1 + max_next_lines, len(lines))):
            amount = extract_money_from_line(lines[j])
            if amount > 0:
                return amount

    return 0.0


# =========================================================
# HELPERS DE MONTOS AIRBNB ROBUSTOS
# =========================================================

def parse_signed_monto(monto_str):
    """Convierte montos con signo, por ejemplo -$84.01 o ($84.01)."""
    if not monto_str:
        return 0.0

    raw = str(monto_str).strip().replace("\xa0", " ")
    negative = bool(re.search(r"(^|\s)-\s*(?:US\$|\$|USD)?", raw, re.IGNORECASE))
    if raw.startswith("(") and raw.endswith(")"):
        negative = True

    amount = parse_monto(raw.replace("-", "").replace("(", "").replace(")", ""))
    return -amount if negative else amount


def extract_signed_money_from_line(line):
    """Extrae el primer monto de una línea conservando el signo negativo."""
    if not line:
        return 0.0

    patterns = [
        r"(-?\s*US\$\s*[\d\.,]+)",
        r"(-?\s*\$\s*[\d\.,]+)",
        r"(-?\s*USD\s*[\d\.,]+)",
        r"(-?\s*[\d\.,]+\s*US\$)",
        r"(-?\s*[\d\.,]+\s*\$)",
        r"(-?\s*[\d\.,]+\s*USD)",
    ]

    for pattern in patterns:
        m = re.search(pattern, line, re.IGNORECASE)
        if m:
            return parse_signed_monto(m.group(1))

    return 0.0


def find_money_in_html_row(content, labels, signed=False):
    """
    Busca la etiqueta dentro de una fila <tr> del HTML y toma el monto de ESA fila.
    Esto evita depender del orden que produce BeautifulSoup.get_text().
    """
    if not content or "<" not in content:
        return 0.0

    soup = BeautifulSoup(content, "html.parser")
    normalized_labels = [normalize_text(x).rstrip(":") for x in labels]

    for text_node in soup.find_all(string=True):
        node_text = clean_line(str(text_node))
        node_norm = normalize_text(node_text).rstrip(":")
        if not node_norm:
            continue

        matched = False
        for label in normalized_labels:
            if node_norm == label or node_norm.startswith(label + " "):
                matched = True
                break

        if not matched:
            continue

        row = text_node.find_parent("tr")
        if row is None:
            continue

        row_text = clean_line(row.get_text(" ", strip=True))
        amount = (
            extract_signed_money_from_line(row_text)
            if signed
            else extract_money_from_line(row_text)
        )

        if signed:
            if amount != 0:
                return amount
        elif amount > 0:
            return amount

    return 0.0


def find_signed_money_after_exact_label(text, labels, max_next_lines=4):
    """Fallback para texto plano: etiqueta exacta + primer monto con signo."""
    lines = get_lines(text)
    normalized_labels = [normalize_text(label).rstrip(":") for label in labels]

    for i, line in enumerate(lines):
        line_normalized = normalize_text(line).rstrip(":")

        for label in normalized_labels:
            if not (line_normalized == label or line_normalized.startswith(label + " ")):
                continue

            amount = extract_signed_money_from_line(line)
            if amount != 0:
                return amount

            for j in range(i + 1, min(i + 1 + max_next_lines, len(lines))):
                amount = extract_signed_money_from_line(lines[j])
                if amount != 0:
                    return amount

    return 0.0


def find_money_after_anchor_and_label(
    text,
    anchor_labels,
    value_labels,
    max_anchor_distance=8,
    max_value_distance=2,
):
    """
    Busca un importe usando el contexto de una seccion.

    Airbnb puede incluir dos ``Total (USD)`` en el mismo correo: el total que
    ve el huesped y el neto del anfitrion. El neto correcto es el Total (USD)
    que aparece despues de la tarifa de servicio para anfitriones.
    """
    lines = get_lines(text)
    anchors = [normalize_text(x).rstrip(":") for x in anchor_labels]
    values = [normalize_text(x).rstrip(":") for x in value_labels]

    for i, line in enumerate(lines):
        line_norm = normalize_text(line).rstrip(":")
        anchor_match = any(
            line_norm == anchor or line_norm.startswith(anchor + " ")
            for anchor in anchors
        )
        if not anchor_match:
            continue

        anchor_end = min(i + 1 + max_anchor_distance, len(lines))
        for j in range(i + 1, anchor_end):
            value_norm = normalize_text(lines[j]).rstrip(":")
            value_match = any(
                value_norm == label
                or value_norm.startswith(label + ":")
                or value_norm.startswith(label + " ")
                for label in values
            )
            if not value_match:
                continue

            amount = extract_money_from_line(lines[j])
            if amount > 0:
                return amount

            value_end = min(j + 1 + max_value_distance, len(lines))
            for k in range(j + 1, value_end):
                amount = extract_money_from_line(lines[k])
                if amount > 0:
                    return amount

    return 0.0


def get_airbnb_host_total(content, text):
    """
    Obtiene el NETO real del anfitrión.

    Estrategia principal:
      precio total de la estadía + limpieza + tarifa de servicio anfitrión

    La tarifa de servicio viene negativa, por ejemplo -84.01.
    Ejemplo Amalia: 490 + 52 - 84.01 = 457.99.
    """

    host_fee_labels = [
        "Comisión de servicio del anfitrión",
        "Comision de servicio del anfitrion",
        "Comisión de servicio para anfitriones",
        "Comision de servicio para anfitriones",
        "Tarifa de servicio para anfitriones",
        "Tarifa de servicio para el anfitrión",
        "Tarifa de servicio para el anfitrion",
        "Tarifa por servicio para anfitriones",
        "Tarifa por servicio para el anfitrión",
        "Tarifa por servicio para el anfitrion",
        "Host service fee",
    ]

    # "Ganas" es el pago neto que Airbnb muestra al anfitrion. Debe tener
    # prioridad sobre encabezados de seccion como "Cobro del anfitrion",
    # porque el primer importe despues de ese encabezado es el alojamiento
    # bruto y no el neto.
    direct_earnings = find_money_after_exact_label(text, [
        "Ganas",
        "Ganás",
        "You earn",
        "Your earnings",
    ], max_next_lines=2)
    if direct_earnings > 0:
        return round(direct_earnings, 2)

    # En la plantilla nueva hay dos Total (USD). Elegimos especificamente el
    # que sigue a la comision del anfitrion, no el total bruto del huesped.
    contextual_total = find_money_after_anchor_and_label(
        text,
        host_fee_labels,
        ["Total (USD)"],
        max_anchor_distance=8,
        max_value_distance=2,
    )
    if contextual_total > 0:
        return round(contextual_total, 2)

    stay_labels = [
        "Precio total de la estadía",
        "Precio total de la estadia",
        "Precio total de la estancia",
        "Precio de la estadía",
        "Precio de la estadia",
        "Precio de la estancia",
        "Alojamiento",
        "Tarifa de la habitación",
        "Tarifa de la habitacion",
        "Accommodation fare",
        "Room rate",
        "Room fee",
        "Price for stay",
    ]

    cleaning_labels = [
        "Tarifa de limpieza",
        "Gastos de limpieza",
        "Cleaning fee",
    ]

    # 1) Preferimos componentes encontrados en la MISMA fila HTML.
    stay = find_money_in_html_row(content, stay_labels, signed=False)
    cleaning = find_money_in_html_row(content, cleaning_labels, signed=False)
    host_fee = find_money_in_html_row(content, host_fee_labels, signed=True)

    # 2) Fallback de texto plano si alguna fila HTML no pudo leerse.
    if stay <= 0:
        stay = find_money_after_exact_label(text, stay_labels, max_next_lines=4)
    if stay <= 0:
        stay = find_money_after_prefix_label(text, stay_labels, max_next_lines=2)
    if cleaning <= 0:
        cleaning = find_money_after_exact_label(text, cleaning_labels, max_next_lines=4)
    if host_fee == 0:
        host_fee = find_signed_money_after_exact_label(text, host_fee_labels, max_next_lines=4)

    # Si tenemos estadía y comisión, reconstruimos el neto.
    # Limpieza puede ser 0 en algunos alojamientos.
    if stay > 0 and host_fee != 0:
        # Si por alguna razón el parser perdió el signo, forzamos la comisión a negativa.
        host_fee = -abs(host_fee)
        return round(stay + max(cleaning, 0.0) + host_fee, 2)

    # 3) Intentar total neto en la MISMA fila HTML.
    html_total = find_money_in_html_row(content, ["Total (USD)"], signed=False)
    if html_total > 0:
        return round(html_total, 2)

    # 4) Formatos de Airbnb que dicen explícitamente cuánto ganas.
    earnings = find_money_after_exact_label(text, [
        "Tu ingreso",
        "Ingreso del anfitrión",
        "Ingreso del anfitrion",
        "Ingresos del anfitrión",
        "Ingresos del anfitrion",
        "Host payout",
        "Payout",
    ], max_next_lines=4)
    if earnings > 0:
        return round(earnings, 2)

    # 5) Último fallback: etiqueta Total (USD) en texto plano.
    # Se deja al final porque el orden visual del HTML puede engañar.
    total = find_money_after_exact_label(text, ["Total (USD)"], max_next_lines=3)
    if total > 0:
        return round(total, 2)

    return 0.0


def parse_date_any(s):
    if not s:
        return None

    raw = clean_line(str(s).lower().replace("\xa0", " "))

    # ISO con hora: 2026-06-04T04:00:00.000Z
    m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m_iso:
        return datetime(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))

    # dd/mm/yyyy, d/m/yyyy
    m_full = re.search(r"([0-3]?\d)[/.-]([01]?\d)[/.-](\d{4})", raw)
    if m_full:
        return datetime(int(m_full.group(3)), int(m_full.group(2)), int(m_full.group(1)))

    # martes 9 de junio de 2026
    m_long = re.search(r"(\d{1,2})\s+de\s+([a-záéíóúñ]+)\s+de\s+(\d{4})", raw)
    if m_long:
        day = int(m_long.group(1))
        mon = MESES_FULL.get(m_long.group(2))
        year = int(m_long.group(3))
        if mon:
            return datetime(year, mon, day)

    # mar, 8 sept / mar, 9 jun
    raw2 = re.sub(r"^[a-záéíóúñ]{2,10}\s*,\s*", "", raw)
    m_short = re.search(r"(\d{1,2})\s+([a-záéíóúñ]{3,4})\.?", raw2)
    if m_short:
        day = int(m_short.group(1))
        mon = MESES_ABBR.get(m_short.group(2).strip("."))
        if mon:
            now = datetime.now()
            year = now.year
            if mon < now.month - 6:
                year += 1
            return datetime(year, mon, day)

    return None


def format_date(raw):
    d = parse_date_any(raw)
    if not d:
        return clean_line(raw)
    return d.strftime("%d/%m/%Y")


def compute_noches(ci_raw, co_raw):
    d1 = parse_date_any(ci_raw)
    d2 = parse_date_any(co_raw)
    if not d1 or not d2:
        return 0
    return max(0, (d2 - d1).days)


def get_value_after_label(lines, label_patterns, skip_time=True):
    for i, line in enumerate(lines):
        for pat in label_patterns:
            if re.fullmatch(pat, line, re.IGNORECASE):
                for j in range(i + 1, min(i + 10, len(lines))):
                    candidate = clean_line(lines[j])
                    if not candidate:
                        continue
                    if skip_time and re.fullmatch(r"\d{1,2}:\d{2}", candidate):
                        continue
                    if re.fullmatch(r"Llegada|Salida|Viajeros|Huéspedes|Huespedes|Detalles de la reserva", candidate, re.IGNORECASE):
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
        line_low = normalize_text(line)
        for kw in keywords:
            if normalize_text(kw) in line_low:
                nearby = " ".join(lines[i:i + 10])
                for p in patterns:
                    m = re.search(p, nearby, re.IGNORECASE)
                    if m:
                        amount = parse_monto(m.group(1))
                        if amount > 0:
                            return amount
    return 0.0


def find_money_after_label(text, labels):
    # Para Estei y otros correos con formato "Ingreso estimado: $252.2"
    for label in labels:
        pattern = (
            rf"{re.escape(label)}\s*:?\s*"
            rf"(?:US\$|\$|USD)?\s*"
            rf"([\d\.,]+)"
            rf"\s*(?:US\$|\$|USD)?"
        )
        m = re.search(pattern, text, re.IGNORECASE)
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

# =========================================================
# FILTROS DE CORREOS
# =========================================================

def is_bad_status_subject(subject):
    s = normalize_text(subject)
    bad_words = [
        "cancelada", "cancelado", "rechazada", "rechazado", "vencida", "vencido",
        "recordatorio", "modificada", "modificado", "mensaje de", "te envio un mensaje"
    ]
    return any(w in s for w in bad_words)


def is_airbnb_confirmation_subject(subject):
    s = normalize_text(subject)
    if is_bad_status_subject(subject):
        return False
    return "reserva confirmada" in s or "nueva reserva confirmada" in s


def is_estei_valid_subject(subject):
    s = normalize_text(subject)
    if is_bad_status_subject(subject):
        return False

    # Produccion: solo aprobadas/confirmadas.
    # No metemos solicitudes, rechazadas, vencidas ni canceladas.
    return (
        "reserva aprobada" in s
        or "reservacion aprobada" in s
        or "reserva confirmada" in s
    )

# =========================================================
# PARSER AIRBNB
# =========================================================

def parse_airbnb_from_content(content, subject=""):
    text = html_to_text(content)
    lines = get_lines(text)

    reserva = pick_first(
        text,
        r"Código de confirmación\s*([A-Z0-9]+)",
        r"Codigo de confirmacion\s*([A-Z0-9]+)",
        r"Confirmación\s*#?\s*([A-Z0-9]+)",
        r"Confirmacion\s*#?\s*([A-Z0-9]+)",
        r"\b(HM[A-Z0-9]{7,12})\b"
    )

    huesped = pick_first(
        text,
        r"¡Nueva reserva confirmada!\s*([^\n\r]+?)\s+llega",
        r"Nueva reserva confirmada!\s*([^\n\r]+?)\s+llega",
        r"Reserva confirmada:\s*([^\n\r]+?)\s+llega",
        r"dar la bienvenida a\s+([^\n\r,!.]+)",
        r"Recibe a\s+([^\n\r,!.]+)"
    )
    if not huesped and subject:
        huesped = pick_first(subject, r"Reserva confirmada:\s*([^\n\r]+?)\s+llega")

    apartamento = pick_first(
        text,
        r"\n([^\n]+)\nCasa/apto\.?\s*entero",
        r"\n([^\n]+)\nApartamento\s*-\s*Casa/apto\.?\s*entero",
        r"\n([^\n]+)\nAlojamiento entero",
        r"\n([^\n]+)\nLoft",
        r"\n([^\n]+)\nCondominio",
        r"\n([^\n]+)\nVivienda rentada",
        r"Reserva de «([^»]+)»",
        r"Reserva confirmada\s*\n([^\n]+)"
    )

    checkin_raw = get_value_after_label(lines, [r"Llegada", r"Check-in", r"Fecha de llegada"])
    checkout_raw = get_value_after_label(lines, [r"Salida", r"Check-out", r"Fecha de salida"])

    if not parse_date_any(checkin_raw) or not parse_date_any(checkout_raw):
        dates_found = re.findall(
            r"(?:lun|mar|mié|mie|jue|vie|sáb|sab|dom),?\s+\d{1,2}\s+(?:ene|feb|mar|abr|may|jun|jul|ago|sep|set|sept|oct|nov|dic)",
            text,
            flags=re.IGNORECASE
        )
        if len(dates_found) >= 1 and not parse_date_any(checkin_raw):
            checkin_raw = dates_found[0]
        if len(dates_found) >= 2 and not parse_date_any(checkout_raw):
            checkout_raw = dates_found[1]

    checkin = format_date(checkin_raw)
    checkout = format_date(checkout_raw)

    noches = compute_noches(checkin_raw, checkout_raw)
    if noches == 0:
        m_noches = re.search(r"(\d+)\s+noch", text, re.IGNORECASE)
        if m_noches:
            noches = int(m_noches.group(1))

    viajeros = get_value_after_label(lines, [r"Viajeros", r"Huéspedes", r"Huespedes"], skip_time=False)
    if not viajeros:
        viajeros = pick_first(
            text,
            r"(\d+\s+adultos?(?:,\s*\d+\s+niños?)?(?:,\s*\d+\s+beb[eé])?)"
        )

    # =========================================================
    # TOTAL PAGADO AIRBNB - NETO REAL DEL ANFITRION
    # =========================================================
    # No dependemos del orden visual de "Total (USD)".
    # Primero reconstruimos:
    #   estadia + limpieza - tarifa de servicio del anfitrion
    # Ejemplo Amalia: 490 + 52 - 84.01 = 457.99
    # =========================================================
    total_pagado = get_airbnb_host_total(content, text)

    # Limpieza: primero por fila HTML; fallback al texto plano.
    limpieza = find_money_in_html_row(content, [
        "Tarifa de limpieza",
        "Gastos de limpieza",
        "Cleaning fee",
    ], signed=False)

    if limpieza <= 0:
        limpieza = find_money_after_exact_label(text, [
            "Tarifa de limpieza",
            "Gastos de limpieza",
            "Cleaning fee",
        ], max_next_lines=4)

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
        "_subject": subject,
        "_debug_money_values": find_all_money_values(text),
    }

# =========================================================
# PARSER ESTEI
# =========================================================

def parse_estei_from_content(content, subject=""):
    text = html_to_text(content)

    reserva = pick_first(
        text,
        r"reserva\s*#\s*(\d+)",
        r"reservación\s*#\s*(\d+)",
        r"reservacion\s*#\s*(\d+)",
        r"reserva\s+(\d{10,})",
        r"\b(17\d{18,20})\b"
    )
    if not reserva and subject:
        reserva = pick_first(subject, r"#\s*(\d{10,})", r"\b(17\d{18,20})\b")

    huesped = pick_first(
        text,
        r"Nombre:\s*([^\n\r]+)",
        r"Acabas de aprobar la reservación de:\s*([^\n\r]+)",
        r"Acabas de aprobar la reservacion de:\s*([^\n\r]+)"
    )

    apartamento = pick_first(text, r"Alojamiento:\s*([^\n\r]+)")
    if not apartamento and subject:
        apartamento = pick_first(subject, r"para\s+(.+?)\s+(?:\d{2}/\d{2}/\d{4}|fecha)")

    checkin = pick_first(text, r"Fecha de check-in:\s*([0-3]?\d/[01]?\d/\d{4})")
    checkout = pick_first(text, r"Fecha de check-out:\s*([0-3]?\d/[01]?\d/\d{4})")

    if not checkin and subject:
        checkin = pick_first(subject, r"(\d{2}/\d{2}/\d{4})")

    noches = compute_noches(checkin, checkout)

    viajeros = pick_first(
        text,
        r"Cantidad de viajeros:\s*(\d+)",
        r"Viajeros:\s*(\d+)",
        r"Huéspedes:\s*(\d+)",
        r"Huespedes:\s*(\d+)"
    )

    total_pagado = find_money_after_label(text, [
        "Ingreso estimado",
        "Monto total",
        "Total pagado",
        "Total",
        "Ingreso"
    ])

    return {
        "Canal": "Estei",
        "Huesped": huesped,
        "Reserva": reserva,
        "Checkin": format_date(checkin),
        "Checkout": format_date(checkout),
        "# noches": noches,
        "# huespedes": viajeros,
        "Total Pagado": total_pagado,
        "Limpieza": 0.0,
        "Apartamento": apartamento,
        "_subject": subject,
        "_debug_money_values": find_all_money_values(text),
    }

# =========================================================
# VALIDACION Y ENVIO
# =========================================================

def clean_payload(datos):
    payload = dict(datos)
    payload.pop("_subject", None)
    payload.pop("_debug_money_values", None)
    return payload


def valid_payload(datos):
    canal = datos.get("Canal")
    total = float(datos.get("Total Pagado") or 0)

    required = [
        datos.get("Reserva"),
        datos.get("Apartamento"),
        parse_date_any(datos.get("Checkin")),
        parse_date_any(datos.get("Checkout")),
        total > 0,
    ]

    if not all(required):
        return False

    if canal == "Airbnb":
        return True

    if canal == "Estei":
        return True

    return False


def reason_invalid(datos):
    reasons = []
    if not datos.get("Reserva"):
        reasons.append("sin_reserva")
    if not datos.get("Apartamento"):
        reasons.append("sin_apartamento")
    if not parse_date_any(datos.get("Checkin")):
        reasons.append("checkin_invalido")
    if not parse_date_any(datos.get("Checkout")):
        reasons.append("checkout_invalido")
    if float(datos.get("Total Pagado") or 0) <= 0:
        reasons.append("total_0_o_invalido")
    return ",".join(reasons) or "desconocido"


def post_to_n8n(payload):
    payload = clean_payload(payload)

    if not SEND_TO_WEBHOOK:
        return True, "DRY_RUN", ""

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=45)
        return 200 <= r.status_code < 300, r.status_code, r.text[:500]
    except Exception as e:
        return False, str(e), ""

# =========================================================
# IMAP HELPERS
# =========================================================

def uid_search(mail, criteria):
    status, data = mail.uid("search", None, criteria)
    if status != "OK" or not data or not data[0]:
        return []
    return [int(u) for u in data[0].split()]


def uid_fetch_msg(mail, uid_int):
    status, data = mail.uid("fetch", str(uid_int), "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return None
    return email.message_from_bytes(data[0][1])


def select_mailbox(mail):
    # Para bot normal preferimos inbox. Si Gmail archiva automatico, usa All Mail.
    for mailbox in ["inbox", '"[Gmail]/All Mail"', '"[Gmail]/Todos"']:
        status, _ = mail.select(mailbox)
        if status == "OK":
            log(f"📬 Buzón seleccionado: {mailbox}")
            return mailbox
    raise RuntimeError("No pude seleccionar inbox ni All Mail.")

# =========================================================
# PROCESAMIENTO PRINCIPAL
# =========================================================

def bootstrap_last_uid(mail, state, key, criteria, label):
    if key in state and isinstance(state[key], int):
        return

    uids = uid_search(mail, criteria)
    if not uids:
        log(f"ℹ️ {label}: no hay correos para bootstrap.")
        return

    state[key] = max(uids)
    save_state(state)
    log(f"✅ {label}: bootstrap listo. last_uid={state[key]} (no se envió nada)")


def process_new_since(mail, state, key, criteria, parser_fn, label, subject_filter_fn):
    last_uid = state.get(key)
    if not isinstance(last_uid, int):
        log(f"⚠️ {label}: last_uid no inicializado.")
        return

    uids = uid_search(mail, criteria)
    if not uids:
        log(f"ℹ️ {label}: no hay correos que cumplan filtro.")
        return

    new_uids = sorted([u for u in uids if u > last_uid])
    if not new_uids:
        log(f"✅ {label}: no hay nuevos (last_uid={last_uid}).")
        return

    log(f"📩 {label}: nuevos={len(new_uids)} (desde last_uid={last_uid}).")

    for uid_int in new_uids:
        msg = uid_fetch_msg(mail, uid_int)
        if not msg:
            log(f"⚠️ {label}: no pude fetch UID {uid_int}. Avanzo UID para no bloquear.")
            state[key] = uid_int
            save_state(state)
            continue

        subject = get_subject(msg)

        if not subject_filter_fn(subject):
            log(f"⏭️ {label}: UID {uid_int} ignorado por subject: {subject}")
            state[key] = uid_int
            save_state(state)
            continue

        content = safe_get_html_or_text(msg)
        if not content:
            log(f"⚠️ {label}: UID {uid_int} sin HTML/texto. Avanzo UID para no bloquear.")
            state[key] = uid_int
            save_state(state)
            continue

        datos = parser_fn(content, subject=subject)

        log(
            f"🔎 {label} UID {uid_int}: "
            f"Reserva={datos.get('Reserva')} | "
            f"Apto={datos.get('Apartamento')} | "
            f"Huesped={datos.get('Huesped')} | "
            f"CI={datos.get('Checkin')} | "
            f"CO={datos.get('Checkout')} | "
            f"Total={datos.get('Total Pagado')} | "
            f"Limpieza={datos.get('Limpieza')} | "
            f"Subject={subject}"
        )

        if not valid_payload(datos):
            log(
                f"⚠️ {label}: UID {uid_int} NO enviado ({reason_invalid(datos)}). "
                f"Reserva={datos.get('Reserva')} Montos={datos.get('_debug_money_values')}"
            )
            # Avanzamos UID para que un correo malo no bloquee el bot.
            state[key] = uid_int
            save_state(state)
            continue

        payload = clean_payload(datos)
        ok, info, response = post_to_n8n(payload)

        if ok:
            state[key] = uid_int
            save_state(state)
            log(f"✅ {label}: enviado UID {uid_int} ({info}) Reserva={datos.get('Reserva')}")
        else:
            log(f"⚠️ {label}: fallo enviando UID {uid_int} ({info}). Detengo para reintentar.")
            if response:
                log(f"Respuesta webhook: {response}")
            break


def main_once():
    if not IMAP_PASS:
        raise RuntimeError("Falta IMAP_PASS en variables de entorno.")

    state = load_state()

    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(IMAP_USER, IMAP_PASS)
    select_mailbox(mail)

    airbnb_criteria = '(FROM "automated@airbnb.com")'
    estei_criteria = '(FROM "noreply@estei.app")'

    bootstrap_last_uid(mail, state, "airbnb_last_uid", airbnb_criteria, "Airbnb")
    bootstrap_last_uid(mail, state, "estei_last_uid", estei_criteria, "Estei")

    process_new_since(
        mail,
        state,
        "airbnb_last_uid",
        airbnb_criteria,
        parse_airbnb_from_content,
        "Airbnb",
        is_airbnb_confirmation_subject,
    )

    process_new_since(
        mail,
        state,
        "estei_last_uid",
        estei_criteria,
        parse_estei_from_content,
        "Estei",
        is_estei_valid_subject,
    )

    mail.logout()


def main_loop():
    while True:
        log("\n🔄 Ejecutando fetch reservas (Airbnb + Estei) ...")
        try:
            main_once()
        except Exception as e:
            log(f"❌ Error en ciclo principal: {e}")
        log(f"⏳ Esperando {POLL_SECONDS} segundos...\n")
        time.sleep(POLL_SECONDS)


def main():
    main_loop()


if __name__ == "__main__":
    main()
