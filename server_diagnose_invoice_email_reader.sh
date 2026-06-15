#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"

echo "== diagnostico lector email invoice-parser =="
echo "N8N_DIR=$N8N_DIR"

cd "$N8N_DIR"

echo "== contenedores =="
docker ps --filter name=invoice-parser --filter name=n8n --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

echo "== variables relevantes en invoice-parser =="
docker exec invoice-parser sh -lc 'env | sort | grep -E "^(INVOICE_EMAIL_IMAP_POLL|INVOICE_EMAIL_IMAP_HOST|INVOICE_EMAIL_IMAP_USER|INVOICE_EMAIL_FOLDER|INVOICE_EMAIL_ALLOWED|INVOICE_QR_|FASA_MYSQL_HOST|FASA_MYSQL_DATABASE|FASA_MYSQL_USER)=" || true'
docker exec invoice-parser sh -lc 'if [ -n "${INVOICE_EMAIL_IMAP_PASSWORD:-}" ]; then echo "INVOICE_EMAIL_IMAP_PASSWORD=***"; else echo "INVOICE_EMAIL_IMAP_PASSWORD=(vacio)"; fi'

echo "== health desde n8n =="
docker exec n8n sh -lc 'wget -qO- http://invoice-parser:8765/health'
echo

echo "== prueba IMAP desde invoice-parser =="
docker exec -i invoice-parser python - <<'PY'
import imaplib
import os
import ssl

host = os.environ.get("INVOICE_EMAIL_IMAP_HOST") or os.environ.get("IMAP_HOST")
port = int(os.environ.get("INVOICE_EMAIL_IMAP_PORT") or os.environ.get("IMAP_PORT") or 993)
user = os.environ.get("INVOICE_EMAIL_IMAP_USER") or os.environ.get("IMAP_USER")
password = os.environ.get("INVOICE_EMAIL_IMAP_PASSWORD") or os.environ.get("IMAP_PASSWORD")
folder = os.environ.get("INVOICE_EMAIL_FOLDER", "INBOX")
print("host=", host)
print("user=", user)
print("folder=", folder)
if not all([host, user, password]):
    raise SystemExit("ERROR: faltan variables IMAP en invoice-parser")
mail = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
try:
    mail.login(user, password)
    typ, data = mail.select(folder, readonly=True)
    if typ != "OK":
        raise SystemExit(f"ERROR: no se pudo abrir carpeta {folder}: {data!r}")
    typ, unseen = mail.search(None, "UNSEEN")
    unseen_count = len((unseen[0] or b"").split()) if typ == "OK" and unseen else 0
    print("IMAP_OK")
    print("unseen=", unseen_count)
finally:
    try:
        mail.logout()
    except Exception:
        pass
PY

echo "== cola invoice-parser =="
docker exec invoice-parser sh -lc 'find /var/data/facturas_parseadas/cola -maxdepth 2 -type f 2>/dev/null | sed "s#^#/##" | tail -30 || true'

echo "== ultimas facturas staging =="
docker exec -i invoice-parser python - <<'PY'
import os
import pymysql

conn = pymysql.connect(
    host=os.environ["FASA_MYSQL_HOST"],
    port=int(os.environ.get("FASA_MYSQL_PORT") or 3306),
    user=os.environ["FASA_MYSQL_USER"],
    password=os.environ["FASA_MYSQL_PASSWORD"],
    database=os.environ.get("FASA_MYSQL_DATABASE") or "fasa",
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    connect_timeout=5,
    read_timeout=8,
    write_timeout=8,
)
with conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.fecha_proceso, c.archivo_original, c.comprobante_tipo,
                   c.emisor_razon_social, c.total, c.requiere_revision,
                   eo.email_from, eo.attachment_name
            FROM facturas_ocr_cabecera c
            LEFT JOIN facturas_ocr_email_origen eo ON eo.factura_id = c.id
            ORDER BY c.fecha_proceso DESC, c.id DESC
            LIMIT 8
            """
        )
        for row in cur.fetchall():
            print(row)
print("OK: diagnostico lector email finalizado")
PY

