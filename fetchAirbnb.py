import imaplib
import email
from bs4 import BeautifulSoup
import re
from datetime import datetime
import requests

# --- CONFIGURACIÓN ---
IMAP_HOST = "imap.gmail.com"
IMAP_USER = "marianospinelli18@gmail.com"
IMAP_PASS = "ggtz czbh yfcy ghco"
WEBHOOK_URL = "http://localhost:5678/webhook-test/airbnb"  # Cambia a tu webhook real

def main():
    try:
        # Conexión al servidor IMAP
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select("inbox")

        # Buscar correos de Airbnb con asunto de reserva confirmada
        status, messages = mail.search(
            None,
            '(FROM "automated@airbnb.com" SUBJECT "Reserva confirmada")'
        )
        messages = messages[0].split()

        if not messages:
            print("No hay correos de reservas confirmadas de Airbnb.")
            return

        # Tomar solo el más reciente
        latest_email_id = messages[-1]
        status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        # Obtener contenido HTML
        html_content = None
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html_content = part.get_payload(decode=True).decode()
                except UnicodeDecodeError:
                    html_content = part.get_payload(decode=True).decode('latin1')
                break

        if not html_content:
            print("No se encontró contenido HTML en el correo.")
            return

        soup = BeautifulSoup(html_content, 'html.parser')
        text = soup.get_text(separator="\n")

        # --- EXTRAER DATOS ---
        # Huésped
        huesped = ''
        huesped_tag = soup.find(string=re.compile(r"bienvenida a\s+(\w+)", re.IGNORECASE))
        if huesped_tag:
            m = re.search(r"bienvenida a\s+(\w+)", huesped_tag, re.IGNORECASE)
            if m:
                huesped = m.group(1)

        # Código de reserva
        reserva_match = re.search(r"Código de confirmación\s*([A-Z0-9]+)", text)
        reserva = reserva_match.group(1) if reserva_match else ''

        # Apartamento
        apt_match = re.search(r"\n([^\n]+)\nCasa/apto\. entero", text)
        apartamento = apt_match.group(1).strip() if apt_match else ''

        # Check-in y Check-out
        checkin_match = re.search(r"Llegada\s*(\w+,\s*\d+\s*\w+)", text)
        checkout_match = re.search(r"Salida\s*(\w+,\s*\d+\s*\w+)", text)
        checkin = checkin_match.group(1) if checkin_match else ''
        checkout = checkout_match.group(1) if checkout_match else ''

        # Noches
        noches = 0
        noches_match = re.search(r"por (\d+) noches", text)
        if noches_match:
            noches = int(noches_match.group(1))
        elif checkin and checkout:
            meses = {
                'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6,
                'jul': 7, 'ago': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dic': 12
            }
            def parse_fecha(fecha_str):
                partes = fecha_str.split()
                dia = int(partes[1])
                mes = meses[partes[2].lower()]
                return datetime(datetime.now().year, mes, dia)
            try:
                noches = (parse_fecha(checkout) - parse_fecha(checkin)).days
            except:
                noches = 0

        # Viajeros
        viajeros_match = re.search(r"Viajeros\s*([\d\s\w,]+)", text)
        viajeros = viajeros_match.group(1).strip().split('\n')[0] if viajeros_match else ''

        # Total pagado (Ganas)
        total_match = re.search(r"Ganas\s*\$([\d,\.]+)", text)
        total_pagado = float(total_match.group(1).replace(',', '.')) if total_match else 0.0

        # Limpieza
        limpieza_match = re.search(r"Gastos de limpieza\s*\$([\d,\.]+)", text)
        limpieza = float(limpieza_match.group(1).replace(',', '.')) if limpieza_match else 0.0

        # --- RESULTADO FINAL ---
        datos = {
            'Huesped': huesped,
            'Reserva': reserva,
            'Checkin': checkin,
            'Checkout': checkout,
            '# noches': noches,
            '# huespedes': viajeros,
            'Total Pagado': total_pagado,
            'Limpieza': limpieza,
            'Apartamento': apartamento
        }

        print("Datos extraídos:", datos)

        # --- ENVIAR AL WEBHOOK ---
        if WEBHOOK_URL:
            try:
                response = requests.post(WEBHOOK_URL, json=datos)
                if response.status_code == 200:
                    print("Datos enviados correctamente al webhook.")
                else:
                    print(f"Error enviando datos al webhook: {response.status_code}")
            except Exception as e:
                print("Error enviando datos al webhook:", e)

    except Exception as e:
        print("⚠️ Error en fetchAirbnb:", e)

if __name__ == "__main__":
    main()
