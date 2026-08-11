import csv
import os
from datetime import datetime

import airbnb_imap_bot_fixed as bot


OUTPUT_CSV = os.getenv(
    "REPAIR_OUTPUT",
    "airbnb_totales_corregidos.csv"
)

MAX_EMAILS = int(
    os.getenv("REPAIR_MAX_EMAILS", "0")
)  # 0 = todos


def date_sort_value(value):
    try:
        return datetime.strptime(value, "%d/%m/%Y")
    except Exception:
        return datetime.max


def main():
    if not bot.IMAP_PASS:
        raise RuntimeError(
            'Falta IMAP_PASS. En PowerShell usa: '
            '$env:IMAP_PASS="TU_APP_PASSWORD_DE_GMAIL"'
        )

    # =========================================================
    # CONEXIÓN GMAIL
    # =========================================================
    mail = bot.imaplib.IMAP4_SSL(bot.IMAP_HOST)
    mail.login(bot.IMAP_USER, bot.IMAP_PASS)

    mailbox = bot.select_mailbox(mail)

    print(
        f"📬 Revisando reservas históricas de Airbnb en {mailbox}...",
        flush=True
    )

    # =========================================================
    # SOLO CORREOS DESDE EL 5 DE JULIO DE 2026
    # =========================================================
    uids = bot.uid_search(
        mail,
        '(FROM "automated@airbnb.com" SINCE "05-Jul-2026")'
    )

    if MAX_EMAILS > 0:
        uids = uids[-MAX_EMAILS:]

    print(
        f"🔎 Correos Airbnb encontrados: {len(uids)}",
        flush=True
    )

    # =========================================================
    # RESERVAS ENCONTRADAS
    # =========================================================
    # Reserva -> datos
    #
    # Si existe más de un correo para el mismo código HM,
    # nos quedamos con el UID más reciente.
    # =========================================================

    reservas = {}

    for n, uid_int in enumerate(uids, start=1):

        msg = bot.uid_fetch_msg(mail, uid_int)

        if not msg:
            continue

        subject = bot.get_subject(msg)

        # Solo reservas confirmadas.
        # Ignora:
        # - cancelaciones
        # - modificaciones
        # - recordatorios
        # - mensajes
        # - reservas rechazadas
        if not bot.is_airbnb_confirmation_subject(subject):
            continue

        content = bot.safe_get_html_or_text(msg)

        if not content:
            continue

        # =====================================================
        # USA EXACTAMENTE EL PARSER CORREGIDO DEL BOT
        # =====================================================
        datos = bot.parse_airbnb_from_content(
            content,
            subject=subject
        )

        reserva = (
            datos.get("Reserva") or ""
        ).strip()

        total = float(
            datos.get("Total Pagado") or 0
        )

        limpieza = float(
            datos.get("Limpieza") or 0
        )

        # =====================================================
        # VALIDACIÓN
        # =====================================================
        if not reserva or total <= 0:

            print(
                f"⚠️ UID {uid_int}: "
                f"no pude obtener Reserva/Total. "
                f"Reserva={reserva!r} "
                f"Total={total} "
                f"Subject={subject}",
                flush=True
            )

            continue

        # =====================================================
        # GUARDAR RESERVA
        # =====================================================
        reservas[reserva] = {
            "UID": uid_int,
            "Reserva": reserva,
            "Apartamento": datos.get("Apartamento") or "",
            "Huesped": datos.get("Huesped") or "",
            "Checkin": datos.get("Checkin") or "",
            "Checkout": datos.get("Checkout") or "",
            "# noches": datos.get("# noches") or 0,
            "# huespedes": datos.get("# huespedes") or "",
            "Total Pagado Correcto": round(total, 2),
            "Limpieza": round(limpieza, 2),
            "Subject": subject,
        }

        # Mostrar progreso
        if n % 100 == 0:
            print(
                f"   Procesados {n}/{len(uids)} correos...",
                flush=True
            )

    # =========================================================
    # CERRAR GMAIL
    # =========================================================
    mail.logout()

    # =========================================================
    # PREPARAR RESULTADOS
    # =========================================================
    rows = list(reservas.values())

    rows.sort(
        key=lambda r: (
            date_sort_value(r["Checkin"]),
            r["Reserva"]
        )
    )

    fieldnames = [
        "UID",
        "Reserva",
        "Apartamento",
        "Huesped",
        "Checkin",
        "Checkout",
        "# noches",
        "# huespedes",
        "Total Pagado Correcto",
        "Limpieza",
        "Subject",
    ]

    # =========================================================
    # CREAR CSV
    # =========================================================
    with open(
        OUTPUT_CSV,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(rows)

    # =========================================================
    # RESULTADO
    # =========================================================
    print("", flush=True)

    print(
        f"✅ Reservas únicas extraídas: {len(rows)}",
        flush=True
    )

    print(
        f"✅ Archivo generado: {OUTPUT_CSV}",
        flush=True
    )

    print("", flush=True)

    print(
        "IMPORTANTE: este script NO envía nada a n8n "
        "y NO modifica tu hoja. "
        "Solo genera la lista histórica utilizando "
        "el parser actual del bot.",
        flush=True
    )


if __name__ == "__main__":
    main()