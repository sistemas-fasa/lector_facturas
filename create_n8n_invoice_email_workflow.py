"""Create or update the n8n email intake workflow for invoice OCR."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from create_n8n_invoice_parser_vfp import N8nClient, api_workflow_payload, load_env_file, node


WORKFLOW_NAME = "Facturas OCR - Email a staging FoxPro"
PREVIEW_FILE = "workflow_invoice_email.preview.json"
DEFAULT_HELPER_URL = "http://invoice-parser:8765/parse"


def main() -> int:
    config = load_config()
    workflow = build_workflow(config)
    missing = missing_variables(config)

    if not config["N8N_API_URL"] or not config["N8N_API_KEY"]:
        Path(PREVIEW_FILE).write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        print("STATUS: SKIPPED_API")
        print("workflow_id: null")
        print("message: faltan N8N_API_URL o N8N_API_KEY; deje preview local")
        print(f"local_workflow_preview: {Path(PREVIEW_FILE).resolve()}")
        print(f"missing_variables: {', '.join(missing) if missing else '(ninguna)'}")
        return 0

    client = N8nClient(config["N8N_API_URL"], config["N8N_API_KEY"])
    workflow_name = config["INVOICE_EMAIL_WORKFLOW_NAME"] or WORKFLOW_NAME
    existing = client.find_workflow_by_name(workflow_name)
    payload = api_workflow_payload(workflow)
    if existing:
        workflow_id = existing["id"]
        result = client.update_workflow(workflow_id, payload)
        status = "UPDATED"
    else:
        result = client.create_workflow(payload)
        workflow_id = result["id"]
        status = "CREATED"

    if result.get("active"):
        client.deactivate_workflow(workflow_id)

    Path(PREVIEW_FILE).write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"STATUS: {status}")
    print(f"workflow_id: {workflow_id}")
    print("active: false")
    print(f"helper_url: {config['INVOICE_EMAIL_HELPER_URL']}")
    print(f"credential_configured: {bool(config['INVOICE_EMAIL_IMAP_CREDENTIAL_ID'] or config['INVOICE_EMAIL_IMAP_CREDENTIAL_NAME'])}")
    print(f"missing_variables: {', '.join(missing) if missing else '(ninguna)'}")
    print("next: revisar credencial IMAP en n8n y activar workflow cuando estemos conformes")
    return 0


def load_config() -> dict[str, str]:
    load_env_file(Path(".env"))
    api_key = os.environ.get("N8N_API_KEY", "")
    key_file = Path(r"C:\tmp\n8n_api_key.txt")
    if not api_key and key_file.exists():
        api_key = key_file.read_text(encoding="utf-8").strip()

    return {
        "N8N_API_URL": os.environ.get("N8N_API_URL", "").rstrip("/"),
        "N8N_API_KEY": api_key,
        "INVOICE_EMAIL_WORKFLOW_NAME": os.environ.get("INVOICE_EMAIL_WORKFLOW_NAME", WORKFLOW_NAME),
        "INVOICE_EMAIL_HELPER_URL": os.environ.get("INVOICE_EMAIL_HELPER_URL", os.environ.get("INVOICE_HELPER_URL", DEFAULT_HELPER_URL)).rstrip("/"),
        "INVOICE_EMAIL_FOLDER": os.environ.get("INVOICE_EMAIL_FOLDER", "INBOX"),
        "INVOICE_EMAIL_ALLOWED_EXTENSIONS": os.environ.get("INVOICE_EMAIL_ALLOWED_EXTENSIONS", "pdf,jpg,jpeg,png"),
        "INVOICE_EMAIL_ALLOWED_SENDERS": os.environ.get("INVOICE_EMAIL_ALLOWED_SENDERS", ""),
        "INVOICE_EMAIL_PARSE_TIMEOUT_MS": os.environ.get("INVOICE_EMAIL_PARSE_TIMEOUT_MS", "600000"),
        "INVOICE_EMAIL_IMAP_CREDENTIAL_ID": os.environ.get("INVOICE_EMAIL_IMAP_CREDENTIAL_ID", ""),
        "INVOICE_EMAIL_IMAP_CREDENTIAL_NAME": os.environ.get("INVOICE_EMAIL_IMAP_CREDENTIAL_NAME", ""),
    }


def missing_variables(config: dict[str, str]) -> list[str]:
    required = ["N8N_API_URL", "N8N_API_KEY", "INVOICE_EMAIL_HELPER_URL"]
    missing = [key for key in required if not config.get(key)]
    if not config.get("INVOICE_EMAIL_IMAP_CREDENTIAL_ID") and not config.get("INVOICE_EMAIL_IMAP_CREDENTIAL_NAME"):
        missing.append("INVOICE_EMAIL_IMAP_CREDENTIAL_ID o INVOICE_EMAIL_IMAP_CREDENTIAL_NAME")
    return missing


def build_workflow(config: dict[str, str]) -> dict[str, Any]:
    helper_url = config["INVOICE_EMAIL_HELPER_URL"].rstrip("/")
    if helper_url.endswith("/parse"):
        helper_url = helper_url[: -len("/parse")] + "/enqueue"
    elif not helper_url.endswith("/enqueue"):
        helper_url = helper_url + "/enqueue"
    helper_url += "?source_type=email"
    email_node = node(
        "Leer Mail Facturas",
        "n8n-nodes-base.emailReadImap",
        [-900, 100],
        {
            "downloadAttachments": True,
            "options": {
                "mailbox": config["INVOICE_EMAIL_FOLDER"] or "INBOX",
                "postProcessAction": "read",
                "allowUnauthorizedCerts": True,
            },
        },
    )
    email_node["typeVersion"] = 2
    credentials = credential_payload(config)
    if credentials:
        email_node["credentials"] = {"imap": credentials}

    nodes = [
        email_node,
        node(
            "Extraer Adjuntos",
            "n8n-nodes-base.code",
            [-620, 100],
            {"jsCode": extract_attachments_code(config["INVOICE_EMAIL_ALLOWED_EXTENSIONS"], config["INVOICE_EMAIL_ALLOWED_SENDERS"])},
        ),
        node(
            "Enviar a Parser OCR",
            "n8n-nodes-base.httpRequest",
            [-320, 100],
            {
                "method": "POST",
                "url": helper_url,
                "authentication": "none",
                "responseFormat": "json",
                "sendBody": True,
                "contentType": "multipart-form-data",
                "bodyParameters": {
                    "parameters": [
                        {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "file"},
                        {"name": "email_from", "value": "={{$json.email_from}}"},
                        {"name": "email_to", "value": "={{$json.email_to}}"},
                        {"name": "email_subject", "value": "={{$json.email_subject}}"},
                        {"name": "email_date", "value": "={{$json.email_date}}"},
                        {"name": "email_message_id", "value": "={{$json.email_message_id}}"},
                        {"name": "email_attachment_name", "value": "={{$json.email_attachment_name}}"},
                    ]
                },
                "options": {"timeout": int(config["INVOICE_EMAIL_PARSE_TIMEOUT_MS"] or "600000")},
            },
        ),
        node(
            "Registrar Resultado",
            "n8n-nodes-base.code",
            [-40, 100],
            {"jsCode": register_result_code()},
        ),
    ]
    return {
        "name": config["INVOICE_EMAIL_WORKFLOW_NAME"] or WORKFLOW_NAME,
        "nodes": nodes,
        "connections": {
            "Leer Mail Facturas": {"main": [[{"node": "Extraer Adjuntos", "type": "main", "index": 0}]]},
            "Extraer Adjuntos": {"main": [[{"node": "Enviar a Parser OCR", "type": "main", "index": 0}]]},
            "Enviar a Parser OCR": {"main": [[{"node": "Registrar Resultado", "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1", "saveManualExecutions": True},
        "pinData": {},
        "active": False,
    }


def credential_payload(config: dict[str, str]) -> dict[str, str]:
    credential: dict[str, str] = {}
    if config["INVOICE_EMAIL_IMAP_CREDENTIAL_ID"]:
        credential["id"] = config["INVOICE_EMAIL_IMAP_CREDENTIAL_ID"]
    if config["INVOICE_EMAIL_IMAP_CREDENTIAL_NAME"]:
        credential["name"] = config["INVOICE_EMAIL_IMAP_CREDENTIAL_NAME"]
    return credential


def extract_attachments_code(allowed_extensions: str, allowed_senders: str) -> str:
    extensions = [ext.strip().lower().lstrip(".") for ext in allowed_extensions.split(",") if ext.strip()]
    senders = [sender.strip().lower() for sender in allowed_senders.split(",") if sender.strip()]
    return f"""
const allowed = new Set({json.dumps(extensions)});
const allowedSenders = {json.dumps(senders)};
const out = [];

function pick(...values) {{
  for (const value of values) {{
    if (value !== undefined && value !== null && String(value).trim() !== '') return String(value);
  }}
  return '';
}}

function normalizeEmail(value) {{
  const raw = String(value || '').toLowerCase();
  const match = raw.match(/[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{{2,}}/i);
  return match ? match[0].toLowerCase() : raw.trim();
}}

function senderAllowed(value) {{
  if (!allowedSenders.length) return true;
  const email = normalizeEmail(value);
  return allowedSenders.some((rule) => {{
    if (!rule) return false;
    if (rule.startsWith('@')) return email.endsWith(rule);
    if (rule.includes('@')) return email === rule;
    return email.endsWith('@' + rule);
  }});
}}

function extensionFromMime(mimeType) {{
  const mime = String(mimeType || '').toLowerCase();
  if (mime.includes('pdf')) return 'pdf';
  if (mime.includes('jpeg') || mime.includes('jpg')) return 'jpg';
  if (mime.includes('png')) return 'png';
  return '';
}}

for (const item of items) {{
  const binary = item.binary || {{}};
  const json = item.json || {{}};
  const from = pick(json.from, json.fromEmail, json.sender, json.headers?.from);
  const to = pick(json.to, json.toEmail, json.headers?.to);
  const subject = pick(json.subject);
  const date = pick(json.date, json.headers?.date);
  const messageId = pick(json.messageId, json.message_id, json.headers?.['message-id']);

  if (!senderAllowed(from)) {{
    continue;
  }}

  for (const [key, value] of Object.entries(binary)) {{
    const fileName = pick(value.fileName, value.filename, key);
    const mimeType = pick(value.mimeType, value.mime);
    const ext = fileName.includes('.') ? fileName.split('.').pop().toLowerCase() : '';
    const inferredExt = ext || extensionFromMime(mimeType);
    const normalizedFileName = fileName.includes('.') || !inferredExt ? fileName : `${{fileName}}.${{inferredExt}}`;
    const allowedByExt = allowed.size === 0 || allowed.has(ext);
    const allowedByMime = /^(application\\/pdf|image\\/(jpeg|png))$/i.test(mimeType);
    if (!allowedByExt && !allowedByMime) continue;

    out.push({{
      json: {{
        email_from: from,
        email_to: to,
        email_subject: subject,
        email_date: date,
        email_message_id: messageId,
        email_attachment_key: key,
        email_attachment_name: normalizedFileName,
        email_attachment_mime: mimeType,
      }},
      binary: {{ file: {{ ...value, fileName: normalizedFileName }} }},
    }});
  }}
}}

return out;
""".strip()


def register_result_code() -> str:
    return """
for (const item of items) {
  const result = item.json || {};
  if (result.status === 'ERROR') {
    throw new Error(result.error || 'invoice-parser devolvio ERROR');
  }
  if (result.status === 'QUEUED') {
    item.json.queued = true;
  }
}
return items;
""".strip()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STATUS: ERROR\\nerror: {error}", file=sys.stderr)
        raise SystemExit(1)
