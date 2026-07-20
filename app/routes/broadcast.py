import time
from flask import Blueprint, request, jsonify
from app.config import BROADCAST_API_KEY
from app.services.whatsapp_service import (
    send_text, send_template, upload_media, fetch_templates)

broadcast_bp = Blueprint("broadcast", __name__)


@broadcast_bp.route("/templates", methods=["GET"])
def templates_route():
    """Phase: Template Registry. Return approved WhatsApp templates so the panel
    can offer a searchable dropdown and auto-detect each template's header type,
    language, category, variables, buttons and status. Same X-API-Key auth."""
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = fetch_templates()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"templates": data, "count": len(data)})


@broadcast_bp.route("/upload-media", methods=["POST"])
def upload_media_route():
    """Phase: Image Header Template Support.

    Upload one image for use as a template IMAGE header. Returns a media_id the
    panel then passes to /broadcast-template as header_image_id. Same X-API-Key
    auth as the other broadcast endpoints; no change to text-template flow.
    """
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file is required"}), 400

    try:
        media_id = upload_media(
            f.read(), f.filename, f.mimetype or "image/jpeg")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"media_id": media_id})

@broadcast_bp.route("/broadcast", methods=["POST"])
def broadcast():
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    body    = request.get_json(silent=True) or {}
    numbers = body.get("numbers", [])
    message = body.get("message", "")
    delay   = body.get("delay_seconds", 2)

    if not numbers or not message:
        return jsonify({"error": "numbers and message are required"}), 400

    results = []
    for num in numbers:
        num = str(num).strip()
        if not num.startswith("91"):
            num = "91" + num.lstrip("0")
        r = send_text(num, message)
        results.append({"number": num, "status": r.status_code, "ok": r.status_code == 200})
        time.sleep(delay)

    ok = sum(1 for x in results if x["ok"])
    # Phase 0 Sprint 3: sovereign audit log (Constitution I.7) — counts only,
    # never message bodies or phone lists.
    from flask import current_app
    from app.services.audit_service import log_audit, request_ip
    log_audit("BROADCAST_SEND", actor="broadcast-api",
              tenant_id=current_app.config.get("PRIMARY_TENANT_ID") or None,
              target="/broadcast",
              detail={"total": len(numbers), "success": ok,
                      "failed": len(numbers) - ok},
              ip=request_ip())
    return jsonify({"total": len(numbers), "success": ok,
                    "failed": len(numbers) - ok, "results": results})


@broadcast_bp.route("/broadcast-template", methods=["POST"])
def broadcast_template():
    if request.headers.get("X-API-Key") != BROADCAST_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    body          = request.get_json(silent=True) or {}
    numbers       = body.get("numbers", [])
    template_name = body.get("template_name", "")
    language      = body.get("language", "en")
    variables     = body.get("variables", [])
    delay         = body.get("delay_seconds", 2)
    # Phase: Image Header Template Support. Optional. When present, an IMAGE
    # header component is sent alongside the body. Absent → text-template flow
    # is byte-for-byte unchanged (backward compat with oxford_special_offer).
    header_image_id = body.get("header_image_id")
    # Phase: Template Registry. Generic media header for image/video/document,
    # {"type": "image"|"video"|"document", "id": "<media_id>"}. header_image_id
    # is still honoured for any existing caller.
    header_media = body.get("header_media") or None

    if not numbers or not template_name:
        return jsonify({"error": "numbers and template_name are required"}), 400

    results = []
    for item in numbers:
        if isinstance(item, dict):
            num  = str(item.get("phone", "")).strip()
            name = str(item.get("name", ""))
        else:
            num  = str(item).strip()
            name = ""

        if not num.startswith("91"):
            num = "91" + num.lstrip("0")

        resolved = [name if v == "{name}" else str(v) for v in variables]
        components = []
        # Media header first (Meta requires header before body ordering).
        if header_image_id:
            components.append({"type": "header",
                               "parameters": [{"type": "image",
                                               "image": {"id": header_image_id}}]})
        elif header_media and header_media.get("id") and header_media.get("type") in ("image", "video", "document"):
            _mt = header_media["type"]
            components.append({"type": "header",
                               "parameters": [{"type": _mt,
                                               _mt: {"id": header_media["id"]}}]})
        if resolved:
            components.append({"type": "body",
                               "parameters": [{"type": "text", "text": v} for v in resolved]})

        r = send_template(num, template_name, language, components or None)
        results.append({"number": num, "status": r.status_code, "ok": r.status_code == 200})
        time.sleep(delay)

    ok = sum(1 for x in results if x["ok"])
    # Phase 0 Sprint 3: sovereign audit log (Constitution I.7)
    from flask import current_app
    from app.services.audit_service import log_audit, request_ip
    log_audit("BROADCAST_SEND", actor="broadcast-api",
              tenant_id=current_app.config.get("PRIMARY_TENANT_ID") or None,
              target="/broadcast-template",
              detail={"template": template_name, "language": language,
                      "total": len(numbers), "success": ok,
                      "failed": len(numbers) - ok},
              ip=request_ip())
    return jsonify({"total": len(numbers), "success": ok,
                    "failed": len(numbers) - ok, "results": results})
