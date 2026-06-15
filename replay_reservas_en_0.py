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

# Para prueba rápida, pega tu App Password aquí.
# Luego vuelve a usar variable de entorno por seguridad.
IMAP_PASS = "nyqy xcnc eaak czpp"

WEBHOOK_URL = "https://airbnb-n8n-81gs.onrender.com/webhook/airbnb"

# Reservas Estei que tienes en 0 / 0
RESERVAS_ESTEI_A_RECUPERAR = [
    "17692099410796413274",
    "17722583179205386761",
    "17723173522519756931",
    "17714433898946549393",
    "17749124469061707676",
    "17742383636634012369",
    "17753645758163353385",
    "17771001513611968448",
    "17784362537260441811",
    "17798700836311407010",
    "17773713889464239295",
    "17774106606033490041",
    "17791509481543391321",
]


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


def compute_noches_ddmmyyyy(ci, co):
    try:
        d1 = datetime.strptime(ci, "%d/%m/%Y")
        d2 = datetime.strptime(co, "%d/%m/%Y")
        return max(0, (d2 - d1).days)
    except Exception:
        return 0


def pick_first(text, *patterns, flags=re.IGNORECASE):
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return clean_line(m.group(1))
    return ""


def find_money_after_label(text, labels):
    """
    Soporta:
    Ingreso estimado: $252.2
    Ingreso estimado: 252.2 $
    Ingreso estimado: USD 252.2
    Total pagado: $252.2
    """
    for label in labels:
        pattern = (
            rf"{label}\s*:?\s*"
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


def parse_estei_from_html(html, fallback_reserva=""):
    text = html_to_text(html)

    reserva = pick_first(
        text,
        r"reserva\s*#\s*(\d+)",
        r"reservación\s*#\s*(\d+)",
        r"reserva\s+(\d{10,})",
        r"\b(17\d{18,20})\b"
    )

    if not reserva:
        reserva = fallback_reserva

    huesped = pick_first(
        text,
        r"Nombre:\s*([^\n\r]+)",
        r"Acabas de aprobar la reservación de:\s*([^\n\r]+)"
    )

    apartamento = pick_first(
        text,
        r"Alojamiento:\s*([^\n\r]+)"
    )

    checkin = pick_first(
        text,
        r"Fecha de check-in:\s*([0-3]?\d/[01]?\d/\d{4})"
    )

    checkout = pick_first(
        text,
        r"Fecha de check-out:\s*([0-3]?\d/[01]?\d/\d{4})"
    )

    noches = compute_noches_ddmmyyyy(checkin, checkout)

    viajeros = pick_first(
        text,
        r"Cantidad de viajeros:\s*(\d+)",
        r"Viajeros:\s*(\d+)",
        r"Huéspedes:\s*(\d+)"
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
        "Checkin": checkin,
        "Checkout": checkout,
        "# noches": noches,
        "# huespedes": viajeros,
        "Total Pagado": total_pagado,
        "Limpieza": 0.0,
        "Apartamento": apartamento,
        "_debug_text_found": text[:1000],
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


def clean_payload(payload):
    payload = dict(payload)
    payload.pop("_debug_text_found", None)
    return payload


def post_to_n8n(payload):
    payload = clean_payload(payload)
    r = requests.post(WEBHOOK_URL, json=payload, timeout=45)
    return 200 <= r.status_code < 300, r.status_code, r.text[:500]


def select_all_mail(mail):
    for mailbox in ['"[Gmail]/All Mail"', '"[Gmail]/Todos"', "inbox"]:
        status, _ = mail.select(mailbox)
        if status == "OK":
            print(f"📬 Buzón seleccionado: {mailbox}")
            return mailbox

    raise RuntimeError("No pude seleccionar inbox ni All Mail.")


def valid_estei_payload(datos):
    return (
        datos.get("Reserva")
        and datos.get("Apartamento")
        and datos.get("Checkin")
        and datos.get("Checkout")
        and float(datos.get("Total Pagado") or 0) > 0
    )


def replay_estei(mail):
    print("\n🔁 Recuperando reservas Estei que llegaron en 0...\n")

    for code in RESERVAS_ESTEI_A_RECUPERAR:
        # Buscamos por código en todos los correos de Estei.
        # Si el código no está en el cuerpo, puede que no aparezca.
        criteria = f'(FROM "noreply@estei.app" TEXT "{code}")'
        uids = uid_search(mail, criteria)

        # Fallback amplio: buscar por remitente y luego parsear correos cercanos.
        if not uids:
            print(f"\n⚠️ No encontré por código {code}. Buscando en correos Estei generales...")
            criteria_general = '(FROM "noreply@estei.app" SINCE "01-Feb-2026" BEFORE "01-Jul-2026")'
            uids = uid_search(mail, criteria_general)

        if not uids:
            print(f"❌ No encontré correos Estei para {code}")
            continue

        print(f"\n🔎 {code}: revisando {len(uids)} correo(s).")

        best = None
        best_uid = None

        for uid in sorted(uids, reverse=True):
            msg = uid_fetch_msg(mail, uid)
            if not msg:
                continue

            subject = get_subject(msg)
            html = safe_get_html(msg)
            if not html:
                continue

            datos = parse_estei_from_html(html, fallback_reserva=code)

            # Si el correo sí trae código, debe coincidir.
            # Si no trae código, usamos fallback, pero igual necesitamos datos válidos.
            if datos.get("Reserva") != code:
                continue

            print(
                f" - UID {uid} | Subject={subject} | "
                f"Reserva={datos.get('Reserva')} | "
                f"Apto={datos.get('Apartamento')} | "
                f"Huesped={datos.get('Huesped')} | "
                f"CI={datos.get('Checkin')} | "
                f"CO={datos.get('Checkout')} | "
                f"Total={datos.get('Total Pagado')}"
            )

            if valid_estei_payload(datos):
                best = datos
                best_uid = uid
                break

        if not best:
            print(f"⚠️ No envío {code}: no pude obtener datos completos con monto válido.")
            continue

        payload = clean_payload(best)

        print("\n📌 Payload Estei corregido que se va a enviar:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        ok, status, response = post_to_n8n(payload)

        if ok:
            print(f"✅ Recuperada y enviada Estei {code} desde UID {best_uid} HTTP {status}")
        else:
            print(f"❌ Falló envío Estei {code}: HTTP/status {status}")
            if response:
                print(response)


def main():
    if not IMAP_PASS or IMAP_PASS == "PEGA_AQUI_TU_APP_PASSWORD":
        raise RuntimeError("Falta colocar IMAP_PASS / App Password de Gmail.")

    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(IMAP_USER, IMAP_PASS)

    select_all_mail(mail)
    replay_estei(mail)

    mail.logout()


if __name__ == "__main__":
    main()