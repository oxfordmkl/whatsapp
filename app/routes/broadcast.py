import time
from flask import Blueprint, request, jsonify
from app.config import BROADCAST_API_KEY
from app.services.whatsapp_service import send_text, send_template, upload_media

broadcast_bp = Blueprint("broadcast", __name__)


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
        # Image header first (Meta requires header before body ordering).
        if header_image_id:
            components.append({"type": "header",
                               "parameters": [{"type": "image",
                                               "image": {"id": header_image_id}}]})
        if resolved:
            components.append({"type": "body",
                               "parameters": [{"type": "text", "text": v} for v in resolved]})

        r = send_template(num, template_name, language, components or None)
        results.append({"number": num, "status": r.status_code, "ok": r.status_code == 200})
        time.sleep(delay)

    ok = sum(1 for x in results if x["ok"])
    return jsonify({"total": len(numbers), "success": ok,
                    "failed": len(numbers) - ok, "results": results})
