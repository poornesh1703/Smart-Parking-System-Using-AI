# app.py — YOLO plate detection + Robust Tesseract OCR (one car ⇒ one plate, laptop cam only)
import os
import re
import cv2
import time
import pytesseract
import numpy as np
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

import requests
from ultralytics import YOLO

# ------------------ Config (.env optional) ------------------
load_dotenv()

# DB (SQLite default). For MySQL set: mysql+pymysql://root:@localhost:3306/parking_ai
DB_URI = os.getenv("DB_URI", "sqlite:///plates.db")

# Tesseract (Windows; override in .env if different)
TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if os.path.exists(TESSERACT_CMD):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# Force laptop camera only
CAM_SRC = 0

# Capture sizes (modest to keep RAM/CPU low)
CAP_WIDTH  = int(os.getenv("CAP_WIDTH",  "960"))
CAP_HEIGHT = int(os.getenv("CAP_HEIGHT", "540"))
CAP_FPS    = int(os.getenv("CAP_FPS",    "30"))

# YOLO model weights (auto-download to models/lp_best.pt)
MODEL_DIR = Path("models")
MODEL_DIR.mkdir(exist_ok=True)
LP_MODEL_PATH = MODEL_DIR / "lp_best.pt"
LP_DOWNLOAD_URLS = [
    "https://raw.githubusercontent.com/Muhammad-Zeerak-Khan/Automatic-License-Plate-Recognition-using-YOLOv8/main/license_plate_detector.pt",
    "https://huggingface.co/keremberke/yolov5n-license-plate/resolve/main/best.pt",
]
YOLO_IMGSZ = int(os.getenv("LP_IMGSZ", "960"))
YOLO_CONF  = float(os.getenv("LP_CONF",  "0.35"))
YOLO_IOU   = float(os.getenv("LP_IOU",   "0.45"))

# Plate crop quality gates (tighter → fewer false OCRs)
MIN_AREA      = int(os.getenv("MIN_AREA", "22000"))      # min crop area (w*h)
SHARPNESS_THR = float(os.getenv("SHARPNESS_THR", "140")) # Laplacian variance
ASPECT_MIN    = float(os.getenv("ASPECT_MIN", "2.6"))
ASPECT_MAX    = float(os.getenv("ASPECT_MAX", "5.6"))

# OCR settings
ALLOWLIST   = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
BASE_TESS   = f"-c tessedit_char_whitelist={ALLOWLIST} --oem 3"
PSMS_TRY    = ["--psm 7", "--psm 6", "--psm 8"]  # line, block, word

# India-only format: AA 00 AA 0000 (1–2 digit RTO & 1–2 letter series)
IND_PLATE_REGEX = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,2}\d{4}$")

# Tracking / de-dup
TRACK_IOU_MATCH   = float(os.getenv("TRACK_IOU_MATCH", "0.5"))
TRACK_TTL         = float(os.getenv("TRACK_TTL", "3.0"))     # seconds to keep track
DEDUP_SECONDS     = int(os.getenv("DEDUP_SECONDS", "20"))     # avoid DB spam

# UI / Debug
SHOW_WINDOW   = os.getenv("SHOW_WINDOW", "1") == "1"
WINDOW_SCALE  = float(os.getenv("WINDOW_SCALE", "0.7"))  # shrink preview window
SHOW_CROPS    = os.getenv("SHOW_CROPS", "0") == "1"

SAVE_DIR = Path("plates")
SAVE_DIR.mkdir(exist_ok=True)

# ------------------ DB ------------------
engine = create_engine(DB_URI, pool_pre_ping=True, echo=False, future=True)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()

class PlateEvent(Base):
    __tablename__ = "plate_events"
    id = Column(Integer, primary_key=True)
    plate_text = Column(String(64), index=True, nullable=False)
    ocr_conf = Column(Float, nullable=True)
    sharpness = Column(Float, nullable=True)
    image_path = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False)

Base.metadata.create_all(engine)

# ------------------ Utils ------------------
def ensure_lp_weights(path: Path):
    if path.exists() and path.stat().st_size > 100_000:
        return
    for url in LP_DOWNLOAD_URLS:
        try:
            print(f"[DL] Downloading license-plate model: {url}")
            r = requests.get(url, timeout=90)
            r.raise_for_status()
            path.write_bytes(r.content)
            if path.stat().st_size > 100_000:
                print(f"[DL] Saved model to {path.resolve()}")
                return
        except Exception as e:
            print("[DL] Download failed:", e)
    raise FileNotFoundError(f"Could not download model weights. Place them at {path} manually.")

def variance_of_laplacian(img_gray: np.ndarray) -> float:
    return float(cv2.Laplacian(img_gray, cv2.CV_64F).var())

def looks_clear_plate_crop(w: int, h: int, sharpness: float) -> bool:
    if w * h < MIN_AREA:
        return False
    aspect = w / max(h, 1)
    if not (ASPECT_MIN <= aspect <= ASPECT_MAX):
        return False
    if sharpness < SHARPNESS_THR:
        return False
    return True

_CHAR_FIX = str.maketrans({'O':'0','Q':'0','I':'1','L':'1','Z':'2','S':'5','B':'8','G':'6','T':'7'})
def normalize_indian(txt: str) -> str:
    txt = re.sub(r"[^A-Z0-9]", "", (txt or "").upper())
    return txt.translate(_CHAR_FIX)

def _unsharp(img_gray):
    blur = cv2.GaussianBlur(img_gray, (0, 0), 1.0)
    sharp = cv2.addWeighted(img_gray, 1.6, blur, -0.6, 0)
    return sharp

def preprocess_for_tesseract(crop: np.ndarray):
    if crop is None or crop.size == 0:
        return []
    h, w = crop.shape[:2]
    # upscale smaller crops
    target_long = 280
    if max(h, w) < target_long:
        s = target_long / max(h, w)
        crop = cv2.resize(crop, (int(w*s), int(h*s)), interpolation=cv2.INTER_CUBIC)

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # CLAHE + mild denoise + unsharp
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    g1 = clahe.apply(gray)
    g1 = cv2.bilateralFilter(g1, 5, 60, 60)
    g1 = _unsharp(g1)

    # Binarizations
    th_otsu = cv2.threshold(g1, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    th_adap = cv2.adaptiveThreshold(g1, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 31, 10)

    def auto_invert(img):
        return cv2.bitwise_not(img) if img.mean() < 110 else img

    th_otsu_inv = auto_invert(th_otsu)
    th_adap_inv = auto_invert(th_adap)

    # Morphology to remove dots
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (2,2))
    m_otsu = cv2.morphologyEx(th_otsu_inv, cv2.MORPH_OPEN, k, iterations=1)
    m_adap = cv2.morphologyEx(th_adap_inv, cv2.MORPH_OPEN, k, iterations=1)

    return [g1, th_otsu_inv, th_adap_inv, m_otsu, m_adap]

def ocr_plate(crop: np.ndarray):
    """Try multiple preprocess variants and PSMs; prefer India-like strings."""
    best_txt, best_score = None, 0.0
    for variant in preprocess_for_tesseract(crop):
        for psm in PSMS_TRY:
            cfg = f"{BASE_TESS} {psm}"
            try:
                data = pytesseract.image_to_data(variant, config=cfg, output_type=pytesseract.Output.DICT)
            except Exception:
                continue
            for txt, conf_s in zip(data.get("text", []), data.get("conf", [])):
                if not txt:
                    continue
                txt = normalize_indian(txt)
                if not txt:
                    continue
                try:
                    conf = max(0.0, float(conf_s)) / 100.0
                except:
                    conf = 0.0
                score = conf + (0.8 if IND_PLATE_REGEX.fullmatch(txt) else 0.0) + min(len(txt), 12) / 40.0
                if score > best_score:
                    best_score, best_txt = score, txt
    return best_txt, best_score

def iou_xyxy(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    inter_x1, inter_y1 = max(ax, bx), max(ay, by)
    inter_x2, inter_y2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, inter_x2 - inter_x1), max(0, inter_y2 - inter_y1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = aw * ah
    area_b = bw * bh
    return inter / float(area_a + area_b - inter + 1e-6)

# ------------------ Main ------------------
def main():
    # Load YOLO model
    try:
        ensure_lp_weights(LP_MODEL_PATH)
    except Exception as e:
        print("[ERROR] Model weights missing:", e)
        return

    try:
        model = YOLO(str(LP_MODEL_PATH))
    except Exception as e:
        print("[ERROR] Could not load YOLO model:", e)
        return

    # Open laptop camera (DirectShow on Windows)
    cap = cv2.VideoCapture(CAM_SRC, cv2.CAP_DSHOW if os.name == "nt" else 0)
    if not cap.isOpened():
        print("[ERROR] Could not open laptop camera.")
        return

    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAP_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAP_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, CAP_FPS)
    except Exception:
        pass

    tracks = []     # {id, box(x,y,w,h), last_t, ocr_done, text, conf, sharp}
    next_id = 1
    last_seen_text = {}

    if SHOW_WINDOW:
        cv2.namedWindow("ANPR (YOLO + Tesseract, India-only)", cv2.WINDOW_NORMAL)

    print("[INFO] Using laptop camera. ESC to quit.")

    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.02)
            continue

        H, W = frame.shape[:2]
        now = time.time()

        # YOLO inference (CPU)
        try:
            res = model.predict(
                frame,
                imgsz=YOLO_IMGSZ,
                conf=YOLO_CONF,
                iou=YOLO_IOU,
                device="cpu",
                verbose=False
            )[0]
        except Exception as e:
            print("[WARN] YOLO predict error:", e)
            continue

        boxes = res.boxes.xyxy.cpu().numpy() if res.boxes is not None else []

        # xyxy -> xywh
        dets = []
        for (x1, y1, x2, y2) in boxes:
            x1 = int(max(0, x1)); y1 = int(max(0, y1))
            x2 = int(min(W, x2)); y2 = int(min(H, y2))
            w = x2 - x1; h = y2 - y1
            if w <= 0 or h <= 0:
                continue
            dets.append((x1, y1, w, h))

        # prune stale tracks
        tracks = [t for t in tracks if (now - t["last_t"]) <= TRACK_TTL]

        # match detections to tracks
        matched_idxs = set()
        for (x, y, w, h) in dets:
            best_i, best_iou = -1, 0.0
            for i, tr in enumerate(tracks):
                iou = iou_xyxy((x, y, w, h), tr["box"])
                if iou > TRACK_IOU_MATCH and iou > best_iou:
                    best_i, best_iou = i, iou
            if best_i >= 0:
                tracks[best_i]["box"] = (x, y, w, h)
                tracks[best_i]["last_t"] = now
                matched_idxs.add(best_i)
            else:
                tracks.append({
                    "id": next_id,
                    "box": (x, y, w, h),
                    "last_t": now,
                    "ocr_done": False,
                    "text": None,
                    "conf": 0.0,
                    "sharp": 0.0
                })
                matched_idxs.add(len(tracks) - 1)
                next_id += 1

        # process matched tracks (one OCR/save per car)
        for i in matched_idxs:
            tr = tracks[i]
            x, y, w, h = tr["box"]
            x = max(0, x); y = max(0, y)
            w = max(1, w); h = max(1, h)
            if x+w > W or y+h > H:
                continue

            crop = frame[y:y+h, x:x+w]
            if crop.size == 0:
                continue

            sharp = variance_of_laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
            good = looks_clear_plate_crop(w, h, sharp)

            # Only act when the crop is clear enough
            if not good:
                if SHOW_WINDOW:
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (50, 180, 255), 2)
                continue

            if tr["ocr_done"]:
                if SHOW_WINDOW and tr["text"]:
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 220, 0), 2)
                    cv2.putText(frame, f"{tr['text']} ({tr['conf']:.2f})", (x, max(y-6, 20)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)
                continue

            if SHOW_CROPS:
                variants = preprocess_for_tesseract(crop)[:3]
                row = np.hstack([cv2.cvtColor(v, cv2.COLOR_GRAY2BGR) if len(v.shape)==2 else v for v in variants])
                small = cv2.resize(row, None, fx=0.7, fy=0.7)
                cv2.imshow("OCR crops", small)

            txt, conf = ocr_plate(crop)
            if txt and IND_PLATE_REGEX.fullmatch(txt):
                if now - last_seen_text.get(txt, 0) >= DEDUP_SECONDS:
                    last_seen_text[txt] = now

                    out_path = None
                    try:
                        fname = f"{int(now)}_{txt}.jpg"
                        out_path = str(SAVE_DIR / fname)
                        cv2.imwrite(out_path, frame)
                    except Exception as e:
                        print("[WARN] Couldn't save image:", e)
                        out_path = None

                    try:
                        with SessionLocal() as s:
                            evt = PlateEvent(
                                plate_text=txt,
                                ocr_conf=round(conf, 3),
                                sharpness=round(sharp, 2),
                                image_path=out_path,
                                created_at=datetime.now(),
                            )
                            s.add(evt)
                            s.commit()
                    except Exception as e:
                        print("[DB ERROR]", e)

                    tr["ocr_done"] = True
                    tr["text"] = txt
                    tr["conf"] = conf
                    tr["sharp"] = sharp

                    print(f"[OK] Track#{tr['id']}  {txt}  conf={conf:.2f}  sharp={sharp:.0f}  file={out_path}")

            if SHOW_WINDOW:
                color = (0, 220, 0) if tr["ocr_done"] else (0, 200, 255)
                cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)
                if tr["text"]:
                    cv2.putText(frame, f"{tr['text']} ({tr['conf']:.2f})", (x, max(y-6, 20)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)

        if SHOW_WINDOW:
            cv2.putText(frame, f"tracks: {len(tracks)}", (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (180, 220, 180), 2)
            frame_show = cv2.resize(frame, None, fx=WINDOW_SCALE, fy=WINDOW_SCALE) if WINDOW_SCALE != 1.0 else frame
            cv2.imshow("ANPR (YOLO + Tesseract, India-only)", frame_show)
            if (cv2.waitKey(1) & 0xFF) == 27:  # ESC
                break

    cap.release()
    if SHOW_WINDOW:
        cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
