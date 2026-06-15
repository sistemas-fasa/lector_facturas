#!/usr/bin/env bash
set -euo pipefail

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"

echo "== verificar staging facturas OCR =="
echo "N8N_DIR=$N8N_DIR"

cd "$N8N_DIR"

echo "== estado contenedores =="
docker ps --filter name=invoice-parser --filter name=n8n --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"

echo "== health sidecar =="
docker exec n8n sh -lc 'wget -qO- http://invoice-parser:8765/health'
echo

echo "== tablas, vistas y filas staging =="
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

required_tables = [
    "facturas_ocr_cabecera",
    "facturas_ocr_detalle",
    "facturas_ocr_percepciones_iibb",
    "facturas_ocr_eventos",
    "facturas_ocr_email_origen",
]
required_views = ["vw_facturas_ocr_pendientes", "vw_facturas_ocr_detalle", "vw_facturas_ocr_percepciones_iibb"]

with conn:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT TABLE_NAME
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN %s
            ORDER BY TABLE_NAME
            """,
            (required_tables,),
        )
        tables = [row["TABLE_NAME"] for row in cur.fetchall()]
        print("tables=", tables)
        missing_tables = sorted(set(required_tables) - set(tables))
        if missing_tables:
            raise SystemExit("ERROR: faltan tablas staging: " + ", ".join(missing_tables))

        cur.execute(
            """
            SELECT TABLE_NAME
            FROM information_schema.VIEWS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN %s
            ORDER BY TABLE_NAME
            """,
            (required_views,),
        )
        views = [row["TABLE_NAME"] for row in cur.fetchall()]
        print("views=", views)
        missing_views = sorted(set(required_views) - set(views))
        if missing_views:
            raise SystemExit("ERROR: faltan vistas staging: " + ", ".join(missing_views))

        for table in required_tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
            print(f"{table}_rows=", cur.fetchone()["n"])

        cur.execute(
            """
            SELECT id, sha256, emisor_razon_social, letra, punto_venta, numero, total, importada, requiere_revision
            FROM facturas_ocr_cabecera
            ORDER BY id DESC
            LIMIT 5
            """
        )
        print("ultimas_cabeceras=")
        for row in cur.fetchall():
            print(row)

        cur.execute(
            """
            SELECT factura_id, linea, descripcion_factura, cantidad, precio_unitario, subtotal, cuenta_contable, cuenta_descripcion
            FROM facturas_ocr_detalle
            ORDER BY id DESC
            LIMIT 10
            """
        )
        print("ultimos_detalles=")
        for row in cur.fetchall():
            print(row)

        cur.execute(
            """
            SELECT factura_id, linea, jurisdiccion, codjur, importe
            FROM facturas_ocr_percepciones_iibb
            ORDER BY id DESC
            LIMIT 10
            """
        )
        print("ultimas_percepciones_iibb=")
        for row in cur.fetchall():
            print(row)

print("OK: staging facturas OCR verificable desde invoice-parser")
PY

echo "OK: verificacion staging finalizada"
