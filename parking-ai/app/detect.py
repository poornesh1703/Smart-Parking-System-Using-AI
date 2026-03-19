# app/detect.py
from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required
from .models import ParkingLot, ParkingSlot
from . import db

import io
import cv2
import numpy as np
from PIL import Image
import re
from datetime import datetime

# ML imports
try:
    from ultralytics import YOLO
    import easyocr
    _YOLO = YOLO("app/weights/license_plate_detector.pt")  # path to your detector
    _READER = easyocr.Reader(['en'])  # works fine for IN plates
    _DETECT_READY = True
except Exception as e:
    print("[DETECT] Model load failed:", e)
    _YOLO, _READER, _DETECT_READY = None, None, False

detect_bp = Blueprint("detect", __name__)

@detect_bp.route("/detect")
@login_required
def detect_page():
    """UI page to choose Entry/Exit, select Lot/Slot, open camera, capture frame, run OCR, post to camera APIs."""
    return render_template("detect.html")

@detect_bp.route("/api/lots")
@login_required
def api_lots():
    lots = ParkingLot.query.order_by(ParkingLot.name.asc()).all()
    out = []
    for lot in lots:
        slots = ParkingSlot.query.filter_by(lot_id=lot.id).order_by(ParkingSlot.number.asc()).all()
        out.append({
            "id": lot.id,
            "name": lot.name,
            "slots": [{"id": s.id, "number": s.number, "status": s.status} for s in slots]
        })
    return jsonify({"ok": True, "items": out})

@detect_bp.route("/api/ocr", methods=["POST"])
@login_required
def api_ocr():
    """
    Accepts multipart form with 'image' (jpeg/png). Returns detected plate string.
    Steps:
      1) YOLO detects plate bbox
      2) crop plate region
      3) OCR with EasyOCR
    """
    if not _DETECT_READY:
        return jsonify({"ok": False, "msg": "Detector not available. Check model/requirements."}), 500

    file = request.files.get("image")
    if not file:
        return jsonify({"ok": False, "msg": "image required"}), 400

    # Read image to numpy
    img_bytes = file.read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

    # Run detection
    try:
        res = _YOLO(source=frame, verbose=False)
        # take best bbox from first result
        boxes = res[0].boxes
        if boxes is None or boxes.xyxy is None or len(boxes.xyxy) == 0:
            return jsonify({"ok": False, "msg": "No plate detected"}), 422
        # pick highest confidence
        confs = boxes.conf.cpu().numpy()
        xyxy = boxes.xyxy.cpu().numpy()
        idx = int(np.argmax(confs))
        x1, y1, x2, y2 = xyxy[idx].astype(int)

        # crop plate region
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = max(0,x1), max(0,y1), min(w,x2), min(h,y2)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return jsonify({"ok": False, "msg": "Bad crop"}), 422

        # OCR
        # EasyOCR expects RGB
        crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        ocr_res = _READER.readtext(crop_rgb, detail=0)
        text = " ".join(ocr_res).upper().strip()

        # sanitize to typical IN plate pattern (keep A-Z 0-9)
        text = re.sub(r"[^A-Z0-9]", "", text)

        # common IN plate cleanups: merge similar chars
        text = text.replace("O", "0") if text.count("O") and text.count("0")==0 else text
        text = text.replace("I", "1") if text.count("I") and text.count("1")==0 else text

        if len(text) < 6:  # too short
            return jsonify({"ok": False, "msg": "Unreadable plate"}), 422

        return jsonify({"ok": True, "car_number": text})
    except Exception as e:
        print("[OCR] error:", e)
        return jsonify({"ok": False, "msg": "Detection error"}), 500
