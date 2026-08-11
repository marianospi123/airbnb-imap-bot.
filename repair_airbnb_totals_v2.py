import csv
import os
from datetime import datetime

import airbnb_imap_bot_fixed_v2 as bot


OUTPUT_CSV = os.getenv("REPAIR_OUTPUT", "airbnb_totales_corregidos_v2.csv")
MAX_EMAILS = int(os.getenv("REPAIR_MAX_EMAILS", "0"))  # 0 = todos


def date_sort_value(value):
    try:
        return datetime.strptime(value, "%d/%m/%Y")
    except Exception:
        return datetime.max


def select_all_mail(mail):
    """Para reparación histórica buscamos también correos archivados."""
    for mailbox in ['"[Gmail]/All Mail"', '"[Gmail]/Todos"', "inbox"]:
        status, _ = mail.select(mailbox)
        if status == "OK":
            print(f"📬 Buzón seleccionado: {mailbox}", flush=True)
            return mailbox
    raise RuntimeError("No pude seleccionar All Mail / Todos / Inbox.")


def main():
    if not bot.IMAP_PASS:
        raise RuntimeError(
            'Falta IMAP_PASS. En PowerShell usa: '
            '$env:IMAP_PASS="TU_APP_PASSWORD_DE_GMAIL"'
        )

    mail = bot.imaplib.IMAP4_SSL(bot.IMAP_HOST)
    mail.login(bot.IMAP_USER, bot.IMAP_PASS)
    mailbox = select_all_mail(mail)

    print(
        f"📬 Revisando confirmaciones Airbnb desde 05-Jul-2026 en {mailbox}...",
        flush=True,
    )

    uids = bot.uid_search(
        mail,
        '(FROM "automated@airbnb.com" SINCE "05-Jul-2026")',
    )

    if MAX_EMAILS > 0:
        uids = uids[-MAX_EMAILS:]

    print(f"🔎 Correos Airbnb encontrados: {len(uids)}", flush=True)

    reservas = {}

    for n, uid_int in enumerate(uids, start=1):
        msg = bot.uid_fetch_msg(mail, uid_int)
        if not msg:
            continue

        subject = bot.get_subject(msg)

        if not bot.is_airbnb_confirmation_subject(subject):
            continue

        content = bot.safe_get_html_or_text(msg)
        if not content:
            continue

        datos = bot.parse_airbnb_from_content(content, subject=subject)

        reserva = (datos.get("Reserva") or "").strip()
        total = float(datos.get("Total Pagado") or 0)
        limpieza = float(datos.get("Limpieza") or 0)

        if not reserva or total <= 0:
            print(
                f"⚠️ UID {uid_int}: no pude obtener Reserva/Total. "
                f"Reserva={reserva!r} Total={total} Subject={subject}",
                flush=True,
            )
            continue

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

        # Para verificar inmediatamente el caso que venimos usando.
        if reserva == "HM4R98XXN9":
            print(
                f"🎯 AMALIA HM4R98XXN9 => Total={total:.2f} | Limpieza={limpieza:.2f}",
                flush=True,
            )

        if n % 100 == 0:
            print(f"   Procesados {n}/{len(uids)} correos...", flush=True)

    mail.logout()

    rows = list(reservas.values())
    rows.sort(key=lambda r: (date_sort_value(r["Checkin"]), r["Reserva"]))

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

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print("", flush=True)
    print(f"✅ Reservas únicas extraídas: {len(rows)}", flush=True)
    print(f"✅ Archivo generado: {OUTPUT_CSV}", flush=True)
    print(
        "✅ No se envió nada a n8n y no se modificó ninguna reserva.",
        flush=True,
    )


if __name__ == "__main__":
    main()
