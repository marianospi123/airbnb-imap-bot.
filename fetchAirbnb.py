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
IMAP_PASS = os.getenv("IMAP_PASS", "nyqy xcnc eaak czpp")
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

def parse_monto(monto_str):
    if not monto_str:
        return 0.0
    monto = monto_str.strip().replace('\xa0', '')
    monto = monto.replace('$', '').replace('USD', '').strip()

    if '.' in monto and ',' in monto:
        monto = monto.replace('.', '').replace(',', '.')
    elif ',' in monto and '.' not in monto:
        monto = monto.replace(',', '.')
    elif monto.count('.') > 1:
        partes = monto.split('.')
        monto = ''.join(partes[:-1]) + '.' + partes[-1]

    try:
        return float(monto)
    except:
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
    return soup.get_text(separator="\n")

def post_to_n8n(payload):
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=20)
        return r.status_code == 200, r.status_code
    except Exception as e:
        return False, str(e)

def compute_noches_ddmmyyyy(ci, co):
    try:
        d1 = datetime.strptime(ci, "%d/%m/%Y")
        d2 = datetime.strptime(co, "%d/%m/%Y")
        return max(0, (d2 - d1).days)
    except:
        return 0

# --------------------------
# Parsers
# --------------------------

def parse_airbnb_from_html(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")

    huesped = ""
    tag = soup.find(string=re.compile(r"bienvenida a\s+(\w+)", re.IGNORECASE))
    if tag:
        m = re.search(r"bienvenida a\s+(\w+)", tag, re.IGNORECASE)
        if m:
            huesped = m.group(1)

    reserva_match = re.search(r"Código de confirmación\s*([A-Z0-9]+)", text)
    reserva = reserva_match.group(1) if reserva_match else ""

    apt_match = re.search(r"\n([^\n]+)\nCasa/apto\. entero", text)
    apartamento = apt_match.group(1).strip() if apt_match else ""

    checkin_match = re.search(r"Llegada\s*(\w+,\s*\d+\s*\w+)", text)
    checkout_match = re.search(r"Salida\s*(\w+,\s*\d+\s*\w+)", text)
    checkin = checkin_match.group(1) if checkin_match else ""
    checkout = checkout_match.group(1) if checkout_match else ""

    noches = 0
    noches_match = re.search(r"por (\d+) noches", text)
    if noches_match:
        noches = int(noches_match.group(1))

    viajeros_match = re.search(r"Viajeros\s*([\d\s\w,]+)", text)
    viajeros = viajeros_match.group(1).strip().split("\n")[0] if viajeros_match else ""

    total_match = re.search(r"Ganas\s*\$([\d,\.]+)", text)
    total_pagado = parse_monto(total_match.group(1)) if total_match else 0.0

    limpieza_match = re.search(r"Gastos de limpieza\s*\$([\d,\.]+)", text)
    limpieza = parse_monto(limpieza_match.group(1)) if limpieza_match else 0.0

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
    return [int(u) for u in uids]  # convert to int for comparisons

def uid_fetch_msg(mail, uid_int):
    status, data = mail.uid("fetch", str(uid_int), "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return None
    raw = data[0][1]
    return email.message_from_bytes(raw)

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

    # Procesar en orden, y avanzar last_uid solo cuando se envía OK
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

        # Validación mínima: evita “basura”
        if not datos.get("Reserva") or not datos.get("Apartamento"):
            print(f"⚠️ {label}: parse incompleto en UID {uid_int} -> {datos} (no avanzo last_uid).")
            continue

        ok, info = post_to_n8n(datos)
        if ok:
            state[key] = uid_int
            save_state(state)
            print(f"✅ {label}: enviado UID {uid_int} (HTTP {info}) Reserva={datos.get('Reserva')}")
        else:
            print(f"⚠️ {label}: fallo enviando UID {uid_int} ({info}). Detengo para reintentar.")
            # Si falló n8n/red, paro para reintentar en el próximo ciclo y no saltarme nada.
            break

def main():
    state = load_state()

    mail = imaplib.IMAP4_SSL(IMAP_HOST)
    mail.login(IMAP_USER, IMAP_PASS)
    mail.select("inbox")

    # Criteria (filtros exactos)
    airbnb_criteria = '(FROM "automated@airbnb.com" SUBJECT "Reserva confirmada")'
    estei_criteria  = '(FROM "noreply@estei.app" SUBJECT "Reserva Confirmada")'

    # Bootstrap: al arrancar por primera vez, guarda el último UID y NO envía nada
    bootstrap_last_uid(mail, state, "airbnb_last_uid", airbnb_criteria, "Airbnb")
    bootstrap_last_uid(mail, state, "estei_last_uid",  estei_criteria,  "Estei")

    # Luego de bootstrap, solo envía lo nuevo
    process_new_since(mail, state, "airbnb_last_uid", airbnb_criteria, parse_airbnb_from_html, "Airbnb")
    process_new_since(mail, state, "estei_last_uid",  estei_criteria,  parse_estei_from_html,  "Estei")

    mail.logout()

if __name__ == "__main__":
    while True:
        print("\n🔄 Ejecutando fetch (Airbnb + Estei) ...")
        main()
        print("⏳ Esperando 5 minutos...\n")
        time.sleep(300)
