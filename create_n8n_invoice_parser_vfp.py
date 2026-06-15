"""Create or update the n8n invoice parser workflow for VFP handoff."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


WORKFLOW_NAME = "Parser Facturas PDF Imagen a JSON XML para VFP"
DEFAULT_OUTPUT_DIR = "/var/data/facturas_parseadas"


def main() -> int:
    config = load_config()
    missing = missing_variables(config)
    workflow = build_workflow(config)

    if not config["N8N_API_URL"] or not config["N8N_API_KEY"]:
        print("STATUS: SKIPPED_API")
        print("workflow_id: null")
        print("message: faltan N8N_API_URL o N8N_API_KEY; no se creo/actualizo en n8n")
        print(f"missing_variables: {', '.join(missing) if missing else '(ninguna)'}")
        print(f"output_dir: {config['INVOICE_OUTPUT_DIR']}")
        print(f"local_workflow_preview: {Path('workflow_invoice_parser_vfp.preview.json').resolve()}")
        Path("workflow_invoice_parser_vfp.preview.json").write_text(
            json.dumps(workflow, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0

    client = N8nClient(config["N8N_API_URL"], config["N8N_API_KEY"])
    existing = client.find_workflow_by_name(WORKFLOW_NAME)
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

    webhook_path = "facturas/vfp-parser"
    webhook_url = config["N8N_API_URL"].rstrip("/") + f"/webhook/{webhook_path}"
    print(f"STATUS: {status}")
    print(f"workflow_id: {workflow_id}")
    print("active: false")
    print(f"webhook_url: {webhook_url}")
    print(f"missing_variables: {', '.join(missing) if missing else '(ninguna)'}")
    print(f"output_dir: {config['INVOICE_OUTPUT_DIR']}")
    return 0


def api_workflow_payload(workflow: dict[str, Any]) -> dict[str, Any]:
    payload = dict(workflow)
    payload.pop("active", None)
    payload.pop("staticData", None)
    return payload


def load_config() -> dict[str, str]:
    load_env_file(Path(".env"))
    api_key = os.environ.get("N8N_API_KEY", "")
    key_file = Path(r"C:\tmp\n8n_api_key.txt")
    if not api_key and key_file.exists():
        api_key = key_file.read_text(encoding="utf-8").strip()

    return {
        "N8N_API_URL": os.environ.get("N8N_API_URL", "").rstrip("/"),
        "N8N_API_KEY": api_key,
        "INVOICE_OUTPUT_DIR": os.environ.get("INVOICE_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
        "INVOICE_GENERATE_XML": os.environ.get("INVOICE_GENERATE_XML", "true"),
        "DEDUPE_TTL_HOURS": os.environ.get("DEDUPE_TTL_HOURS", "72"),
        "DEDUPE_PHASH_HAMMING": os.environ.get("DEDUPE_PHASH_HAMMING", "12"),
        "INVOICE_HELPER_URL": os.environ.get("INVOICE_HELPER_URL", ""),
        "DRIVE_FOLDER_ID": os.environ.get("DRIVE_FOLDER_ID", ""),
        "IMAP_HOST": os.environ.get("IMAP_HOST", ""),
        "IMAP_PORT": os.environ.get("IMAP_PORT", ""),
        "IMAP_USER": os.environ.get("IMAP_USER", ""),
        "IMAP_PASSWORD": os.environ.get("IMAP_PASSWORD", ""),
    }


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def missing_variables(config: dict[str, str]) -> list[str]:
    required = [
        "N8N_API_URL",
        "N8N_API_KEY",
        "INVOICE_OUTPUT_DIR",
        "INVOICE_GENERATE_XML",
        "DEDUPE_TTL_HOURS",
        "DEDUPE_PHASH_HAMMING",
    ]
    optional = ["DRIVE_FOLDER_ID", "IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASSWORD"]
    return [key for key in required + optional if not config.get(key)]


def build_workflow(config: dict[str, str]) -> dict[str, Any]:
    if config.get("INVOICE_HELPER_URL"):
        return build_host_helper_workflow(config)

    nodes = [
        node("Webhook Trigger", "n8n-nodes-base.webhook", [-1040, -60], {
            "httpMethod": "POST",
            "path": "facturas/vfp-parser",
            "responseMode": "responseNode",
            "options": {"binaryData": True},
        }),
        node("IMAP Email Trigger", "n8n-nodes-base.emailReadImap", [-1040, 180], {
            "downloadAttachments": True,
            "options": {"allowUnauthorizedCerts": True},
        }),
        node("Google Drive Trigger", "n8n-nodes-base.googleDriveTrigger", [-1040, 420], {
            "triggerOn": "specificFolder",
            "folderToWatch": config["DRIVE_FOLDER_ID"] or "__SET_DRIVE_FOLDER_ID__",
            "event": "fileCreated",
            "options": {},
        }),
        node("Normalize Input", "n8n-nodes-base.code", [-760, 180], {"jsCode": normalize_code(config)}),
        node("Fingerprint SHA256", "n8n-nodes-base.code", [-500, 180], {"jsCode": fingerprint_code()}),
        node("Save Original Temp", "n8n-nodes-base.writeBinaryFile", [-240, 180], {
            "fileName": "={{$json.temp_original_path}}",
            "dataPropertyName": "data",
        }),
        node("Convert To OCR Image", "n8n-nodes-base.code", [20, 180], {"jsCode": convert_code()}),
        node("pHash", "n8n-nodes-base.code", [280, 180], {"jsCode": phash_code()}),
        node("Dedupe", "n8n-nodes-base.code", [540, 180], {"jsCode": dedupe_code(config)}),
        node("OCR Tesseract", "n8n-nodes-base.code", [800, 180], {"jsCode": ocr_code()}),
        node("Parser Build JSON XML", "n8n-nodes-base.code", [1060, 180], {"jsCode": parser_save_code()}),
        node("Prepare Response", "n8n-nodes-base.code", [1320, 180], {"jsCode": prepare_response_code()}),
        node("Response Webhook", "n8n-nodes-base.respondToWebhook", [1580, 180], {
            "respondWith": "json",
            "responseBody": "={{$json.response_body}}",
            "options": {},
        }),
    ]
    for workflow_node in nodes:
        if workflow_node["name"] in {"IMAP Email Trigger", "Google Drive Trigger"}:
            workflow_node["disabled"] = True
    return {
        "name": WORKFLOW_NAME,
        "nodes": nodes,
        "connections": connections(),
        "settings": {"executionOrder": "v1", "saveManualExecutions": True},
        "staticData": None,
        "pinData": {},
        "active": False,
    }


def build_host_helper_workflow(config: dict[str, str]) -> dict[str, Any]:
    helper_url = config["INVOICE_HELPER_URL"].rstrip("/") + "?source_type=webhook"
    nodes = [
        node("Webhook Trigger", "n8n-nodes-base.webhook", [-620, 120], {
            "httpMethod": "POST",
            "path": "facturas/vfp-parser",
            "responseMode": "responseNode",
            "options": {"binaryData": True},
        }),
        node("Enviar a Parser Host", "n8n-nodes-base.httpRequest", [-320, 120], {
            "method": "POST",
            "url": helper_url,
            "authentication": "none",
            "responseFormat": "json",
            "sendBody": True,
            "contentType": "multipart-form-data",
            "bodyParameters": {
                "parameters": [
                    {
                        "parameterType": "formBinaryData",
                        "name": "file",
                        "inputDataFieldName": "file",
                    }
                ]
            },
            "options": {},
        }),
        node("Response Webhook", "n8n-nodes-base.respondToWebhook", [-20, 120], {
            "respondWith": "json",
            "responseBody": "={{$json}}",
            "options": {},
        }),
    ]
    return {
        "name": WORKFLOW_NAME,
        "nodes": nodes,
        "connections": {
            "Webhook Trigger": {"main": [[{"node": "Enviar a Parser Host", "type": "main", "index": 0}]]},
            "Enviar a Parser Host": {"main": [[{"node": "Response Webhook", "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1", "saveManualExecutions": True},
        "pinData": {},
        "active": False,
    }


def node(name: str, type_name: str, position: list[int], parameters: dict[str, Any]) -> dict[str, Any]:
    type_version = 1
    if type_name == "n8n-nodes-base.httpRequest":
        type_version = 4.2
    elif type_name == "n8n-nodes-base.webhook":
        type_version = 2
    elif type_name == "n8n-nodes-base.respondToWebhook":
        type_version = 1.5
    return {
        "parameters": parameters,
        "id": slug(name),
        "name": name,
        "type": type_name,
        "typeVersion": type_version,
        "position": position,
    }


def connections() -> dict[str, Any]:
    chain = [
        "Normalize Input",
        "Fingerprint SHA256",
        "Save Original Temp",
        "Convert To OCR Image",
        "pHash",
        "Dedupe",
        "OCR Tesseract",
        "Parser Build JSON XML",
        "Prepare Response",
        "Response Webhook",
    ]
    result: dict[str, Any] = {
        "Webhook Trigger": {"main": [[{"node": "Normalize Input", "type": "main", "index": 0}]]},
        "IMAP Email Trigger": {"main": [[{"node": "Normalize Input", "type": "main", "index": 0}]]},
        "Google Drive Trigger": {"main": [[{"node": "Normalize Input", "type": "main", "index": 0}]]},
    }
    for current, nxt in zip(chain, chain[1:]):
        result[current] = {"main": [[{"node": nxt, "type": "main", "index": 0}]]}
    return result


def normalize_code(config: dict[str, str]) -> str:
    code = r"""
const out = [];
for (const item of items) {
  const binaryKeys = Object.keys(item.binary || {});
  const firstKey = binaryKeys[0] || 'data';
  const bin = item.binary?.[firstKey] || {};
  const filename = bin.fileName || item.json?.name || item.json?.filename || 'factura.bin';
  const extension = (filename.split('.').pop() || 'bin').toLowerCase();
  const sourceType = item.json?.headers ? 'webhook' : (item.json?.email ? 'email' : 'drive');
  item.binary = { data: bin };
  item.json = {
    ...item.json,
    source_type: sourceType,
    original_filename: filename,
    mime_type: bin.mimeType || item.json?.mimeType || 'application/octet-stream',
    extension,
    output_dir: $env.INVOICE_OUTPUT_DIR || '/var/data/facturas_parseadas',
    generate_xml: String($env.INVOICE_GENERATE_XML || 'true').toLowerCase() === 'true',
  };
  out.push(item);
}
return out;
""".strip()
    return (
        code.replace("$env.INVOICE_OUTPUT_DIR || '/var/data/facturas_parseadas'", json.dumps(config["INVOICE_OUTPUT_DIR"]))
        .replace(
            "String($env.INVOICE_GENERATE_XML || 'true').toLowerCase() === 'true'",
            "true" if config["INVOICE_GENERATE_XML"].lower() == "true" else "false",
        )
    )


def fingerprint_code() -> str:
    return r"""
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

for (const item of items) {
  const buf = await this.helpers.getBinaryDataBuffer(0, 'data');
  const sha = crypto.createHash('sha256').update(buf).digest('hex');
  const safeExt = (item.json.extension || 'bin').replace(/[^a-z0-9]/gi, '').toLowerCase() || 'bin';
  const tempDir = path.join('/tmp', 'n8n_invoice_parser');
  fs.mkdirSync(tempDir, { recursive: true });
  const originalPath = path.join(tempDir, `${sha}.${safeExt}`);
  const imagePath = path.join(tempDir, `${sha}.png`);
  const ocrTextPath = path.join(tempDir, `${sha}.txt`);
  item.json.sha256 = sha;
  item.json.temp_original_path = originalPath;
  item.json.temp_image_path = imagePath;
  item.json.ocr_text_path = ocrTextPath;
}
return items;
""".strip()


def convert_code() -> str:
    return r"""
const cp = require('child_process');
const fs = require('fs');
const path = require('path');

for (const item of items) {
  const ext = String(item.json.extension || '').toLowerCase();
  const originalPath = item.json.temp_original_path;
  const imagePath = item.json.temp_image_path;
  fs.mkdirSync(path.dirname(imagePath), { recursive: true });
  if (ext === 'pdf') {
    const prefix = imagePath.replace(/\.png$/i, '');
    const result = cp.spawnSync('pdftoppm', ['-png', '-f', '1', '-singlefile', originalPath, prefix], { encoding: 'utf8' });
    if (result.status !== 0) {
      throw new Error(`pdftoppm fallo: ${result.stderr || result.stdout}`);
    }
  } else {
    fs.copyFileSync(originalPath, imagePath);
  }
}
return items;
""".strip()


def phash_code() -> str:
    return r"""
const cp = require('child_process');

for (const item of items) {
  try {
    const script = `const imghash=require('imghash'); imghash.hash(${JSON.stringify(item.json.temp_image_path)}, 16).then(h=>console.log(h)).catch(e=>{console.error(e.message);process.exit(1)})`;
    const result = cp.spawnSync('node', ['-e', script], { encoding: 'utf8' });
    item.json.phash = result.status === 0 ? String(result.stdout || '').trim() : '';
    if (result.status !== 0) item.json.phash_error = result.stderr || result.stdout || 'imghash failed';
  } catch (error) {
    item.json.phash = '';
    item.json.phash_error = error.message;
  }
}
return items;
""".strip()


def dedupe_code(config: dict[str, str]) -> str:
    code = r"""
const ttlHours = Number($env.DEDUPE_TTL_HOURS || 72);
const hammingMax = Number($env.DEDUPE_PHASH_HAMMING || 12);
const now = Date.now();
const data = this.getWorkflowStaticData('global');
data.invoices = data.invoices || {};
data.phashes = data.phashes || {};

function hamming(a, b) {
  if (!a || !b) return 9999;
  const x = BigInt('0x' + a) ^ BigInt('0x' + b);
  return x.toString(2).split('1').length - 1;
}

for (const item of items) {
  const shaKey = `invoice_sha_${item.json.sha256}`;
  const phash = String(item.json.phash || item.json.stdout || '').trim();
  item.json.phash = phash;
  item.json.is_duplicate = false;
  item.json.duplicate_reason = null;

  for (const [key, seen] of Object.entries(data.invoices)) {
    if (now - seen.ts > ttlHours * 3600 * 1000) delete data.invoices[key];
  }
  for (const [key, seen] of Object.entries(data.phashes)) {
    if (now - seen.ts > ttlHours * 3600 * 1000) delete data.phashes[key];
  }

  if (data.invoices[shaKey]) {
    item.json.is_duplicate = true;
    item.json.duplicate_reason = 'sha256_exacto';
  } else if (phash) {
    for (const seen of Object.values(data.phashes)) {
      if (hamming(phash, seen.phash) <= hammingMax) {
        item.json.is_duplicate = true;
        item.json.duplicate_reason = 'phash_similar';
        break;
      }
    }
  }

  data.invoices[shaKey] = { ts: now };
  if (phash) data.phashes[`invoice_ph_${phash}`] = { ts: now, phash };
}
return items;
""".strip()
    return (
        code.replace("$env.DEDUPE_TTL_HOURS || 72", json.dumps(config["DEDUPE_TTL_HOURS"]) + " || 72")
        .replace("$env.DEDUPE_PHASH_HAMMING || 12", json.dumps(config["DEDUPE_PHASH_HAMMING"]) + " || 12")
    )


def ocr_code() -> str:
    return r"""
const cp = require('child_process');

for (const item of items) {
  const result = cp.spawnSync('tesseract', [item.json.temp_image_path, 'stdout', '-l', 'spa'], {
    encoding: 'utf8',
    maxBuffer: 20 * 1024 * 1024,
  });
  if (result.status !== 0) {
    throw new Error(`tesseract fallo: ${result.stderr || result.stdout}`);
  }
  item.json.ocr_text = result.stdout || '';
  item.json.ocr_confidence = 0;
}
return items;
""".strip()


def parser_save_code() -> str:
    return r"""
const fs = require('fs');
const path = require('path');

function amount(value) {
  if (!value) return 0;
  let s = String(value).replace(/\$/g, '').replace(/ARS/g, '').replace(/\s/g, '').replace(/[^0-9,.-]/g, '');
  if (!s) return 0;
  if (s.includes(',') && s.includes('.')) {
    s = s.lastIndexOf(',') > s.lastIndexOf('.') ? s.replace(/\./g, '').replace(',', '.') : s.replace(/,/g, '');
  } else if (s.includes(',')) {
    s = s.replace(/\./g, '').replace(',', '.');
  }
  const n = Number(s);
  return Number.isFinite(n) ? Number(n.toFixed(2)) : 0;
}

function firstAmount(text, patterns) {
  for (const pattern of patterns) {
    const m = text.match(pattern);
    if (m) return amount(m[1]);
  }
  return 0;
}

function dateIso(text) {
  const iso = text.match(/\b(\d{4})-(\d{2})-(\d{2})\b/);
  if (iso) return iso[0];
  const m = text.match(/\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b/);
  if (!m) return null;
  const y = m[3].length === 2 ? `20${m[3]}` : m[3];
  return `${y.padStart(4, '0')}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
}

function cuit(text) {
  const m = text.match(/\b(?:CUIT|C\.?U\.?I\.?T\.?)?\s*[:#-]?\s*(\d{2}[- ]?\d{8}[- ]?\d)\b/i);
  if (!m) return '';
  const d = m[1].replace(/\D/g, '');
  return d.length === 11 ? `${d.slice(0,2)}-${d.slice(2,10)}-${d.slice(10)}` : d;
}

function invoiceNumber(text) {
  const letter = text.match(/\bFactura\s+([ABCM])\b|\bTipo\s*[:#-]?\s*([ABCM])\b/i);
  const num = text.match(/\b(?:Factura|Comp\.?|Comprobante|N(?:ro|[uú]mero)?\.?)?\s*(?:[A-Z]\s*)?(?:N\s*[:#-]?\s*)?(\d{4,5})\s*[- ]\s*(\d{6,10})\b/i);
  return {
    letra: letter ? String(letter[1] || letter[2]).toUpperCase() : null,
    punto_venta: num ? num[1].padStart(4, '0') : '',
    numero: num ? num[2].padStart(8, '0') : '',
  };
}

function xmlEscape(value) {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function invoiceXml(invoice) {
  return `<factura><version>${invoice.version}</version><estado>${invoice.estado}</estado><fecha_proceso>${invoice.fecha_proceso}</fecha_proceso><origen><tipo>${invoice.origen.tipo}</tipo><archivo_original>${xmlEscape(invoice.origen.archivo_original)}</archivo_original><sha256>${invoice.origen.sha256}</sha256><phash>${invoice.origen.phash}</phash><duplicado>${invoice.origen.duplicado}</duplicado></origen><comprobante><tipo>${invoice.comprobante.tipo}</tipo><letra>${invoice.comprobante.letra || ''}</letra><punto_venta>${invoice.comprobante.punto_venta}</punto_venta><numero>${invoice.comprobante.numero}</numero><fecha_emision>${invoice.comprobante.fecha_emision || ''}</fecha_emision><moneda>${invoice.comprobante.moneda}</moneda></comprobante><emisor><razon_social>${xmlEscape(invoice.emisor.razon_social)}</razon_social><cuit>${invoice.emisor.cuit}</cuit></emisor><importes><neto_gravado>${invoice.importes.neto_gravado.toFixed(2)}</neto_gravado><iva_21>${invoice.importes.iva_21.toFixed(2)}</iva_21><iva_105>${invoice.importes.iva_105.toFixed(2)}</iva_105><total>${invoice.importes.total.toFixed(2)}</total></importes></factura>`;
}

function writeAtomic(filePath, content) {
  const tmp = `${filePath}.tmp`;
  fs.writeFileSync(tmp, content);
  fs.renameSync(tmp, filePath);
}

for (const item of items) {
  const text = item.json.ocr_text || '';
  const invNum = invoiceNumber(text);
  const issueDate = dateIso(text);
  const total = firstAmount(text, [/\bTotal\s*(?:a\s*pagar)?\s*[:$ ]+\s*([0-9][0-9.,]*)/i, /\bImporte\s*total\s*[:$ ]+\s*([0-9][0-9.,]*)/i]);
  const issuerCuit = cuit(text);
  const duplicate = Boolean(item.json.is_duplicate);
  const requiresReview = !(total > 0 && issuerCuit && issueDate && invNum.numero);
  const now = new Date();
  const dateStamp = now.toISOString().slice(0, 10).replace(/-/g, '');
  const shortSha = item.json.sha256.slice(0, 8);
  const baseDir = duplicate ? path.join(item.json.output_dir, 'duplicados') : item.json.output_dir;
  const originalsDir = path.join(item.json.output_dir, 'originales');
  fs.mkdirSync(baseDir, { recursive: true });
  fs.mkdirSync(originalsDir, { recursive: true });
  const baseName = `FACTURA_${dateStamp}_${shortSha}`;
  const jsonFile = path.join(baseDir, `${baseName}.json`);
  const xmlFile = path.join(baseDir, `${baseName}.xml`);
  const readyFile = path.join(baseDir, `${baseName}.ready`);
  const originalFile = path.join(originalsDir, `${item.json.sha256}.${item.json.extension || 'bin'}`);

  const invoice = {
    version: '1.0',
    estado: duplicate ? 'DUPLICADO' : 'OK',
    fecha_proceso: now.toISOString(),
    origen: {
      tipo: item.json.source_type,
      archivo_original: item.json.original_filename,
      mime_type: item.json.mime_type,
      sha256: item.json.sha256,
      phash: item.json.phash || '',
      duplicado: duplicate,
      motivo_duplicado: item.json.duplicate_reason || null,
    },
    comprobante: {
      tipo: 'FACTURA',
      letra: invNum.letra,
      punto_venta: invNum.punto_venta,
      numero: invNum.numero,
      fecha_emision: issueDate,
      fecha_vencimiento: null,
      moneda: /\bUSD\b|U\$S|D[oó]lares/i.test(text) ? 'USD' : 'ARS',
      cae: (text.match(/\bCAE\s*[:#-]?\s*(\d{10,20})\b/i) || [null, null])[1],
      cae_vencimiento: null,
    },
    emisor: { razon_social: '', cuit: issuerCuit, iva_condicion: '', domicilio: '' },
    receptor: { razon_social: '', cuit: '', iva_condicion: '' },
    importes: {
      neto_gravado: firstAmount(text, [/\bNeto\s*(?:gravado)?\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      iva_21: firstAmount(text, [/\bIVA\s*21\s*%?\s*[:$ ]+\s*([0-9][0-9.,]*)/i, /\b21\s*%?\s*IVA\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      iva_105: firstAmount(text, [/\bIVA\s*10[,.]5\s*%?\s*[:$ ]+\s*([0-9][0-9.,]*)/i, /\b10[,.]5\s*%?\s*IVA\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      iva_27: firstAmount(text, [/\bIVA\s*27\s*%?\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      exento: firstAmount(text, [/\bExento\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      no_gravado: firstAmount(text, [/\bNo\s*gravado\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      percepciones: firstAmount(text, [/\bPercepciones?\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      otros_impuestos: firstAmount(text, [/\bOtros\s*impuestos\s*[:$ ]+\s*([0-9][0-9.,]*)/i]),
      total,
    },
    items: [],
    ocr: { texto: text, confianza: item.json.ocr_confidence || 0, motor: 'tesseract' },
    validaciones: {
      total_detectado: total > 0,
      cuit_detectado: Boolean(issuerCuit),
      fecha_detectada: Boolean(issueDate),
      numero_detectado: Boolean(invNum.numero),
      requiere_revision: requiresReview,
      observaciones: requiresReview ? ['Revisar parser con formatos reales de factura'] : [],
    },
  };

  writeAtomic(jsonFile, JSON.stringify(invoice, null, 2));
  if (item.json.generate_xml) writeAtomic(xmlFile, invoiceXml(invoice));
  fs.copyFileSync(item.json.temp_original_path, `${originalFile}.tmp`);
  fs.renameSync(`${originalFile}.tmp`, originalFile);
  fs.writeFileSync(readyFile, '');

  item.json.response_body = {
    status: invoice.estado,
    json_file: jsonFile,
    xml_file: item.json.generate_xml ? xmlFile : null,
    sha256: item.json.sha256,
    requires_review: requiresReview,
  };
}
return items;
""".strip()


def prepare_response_code() -> str:
    return r"""
for (const item of items) {
  if (item.json.response_body) continue;
  let parsed = {};
  try {
    parsed = JSON.parse(item.json.stdout || '{}');
  } catch (error) {
    parsed = { status: 'ERROR', error: error.message, raw: item.json.stdout || '' };
  }
  item.json.response_body = parsed;
}
return items;
""".strip()


def slug(value: str) -> str:
    return value.lower().replace(" ", "-").replace("/", "-")


class N8nClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def find_workflow_by_name(self, name: str) -> dict[str, Any] | None:
        cursor = None
        while True:
            params = {"limit": "100"}
            if cursor:
                params["cursor"] = cursor
            response = self.request("GET", "/api/v1/workflows", query=params)
            workflows = response.get("data", response if isinstance(response, list) else [])
            for workflow in workflows:
                if workflow.get("name") == name:
                    return workflow
            cursor = response.get("nextCursor") if isinstance(response, dict) else None
            if not cursor:
                return None

    def create_workflow(self, workflow: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/api/v1/workflows", body=workflow)

    def update_workflow(self, workflow_id: str, workflow: dict[str, Any]) -> dict[str, Any]:
        for method in ("PUT", "PATCH"):
            try:
                return self.request(method, f"/api/v1/workflows/{workflow_id}", body=workflow)
            except RuntimeError as exc:
                if method == "PATCH" and "405" not in str(exc):
                    raise
        raise RuntimeError("No se pudo actualizar el workflow")

    def deactivate_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/workflows/{workflow_id}/deactivate")

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-N8N-API-KEY", self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"n8n API {method} {path} fallo: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"n8n API no accesible: {exc}") from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STATUS: ERROR\nerror: {error}", file=sys.stderr)
        raise SystemExit(1)
