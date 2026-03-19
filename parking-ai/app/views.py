from functools import wraps
from datetime import datetime, timedelta
import math
from sqlalchemy import func

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    abort,
    jsonify,
)
from flask_login import login_required, current_user

from .models import ParkingLot, ParkingSlot, ParkingEvent, ParkingSession, db

# ------------------------------------------------------------------
# Blueprint
# ------------------------------------------------------------------
views_bp = Blueprint("views", __name__)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_authenticated", False) or current_user.role != "admin":
            return abort(403)
        return fn(*args, **kwargs)
    return wrapper


def _get_json_or_form(name: str, default: str = "") -> str:
    """Safely read a value from JSON body or form data."""
    data = request.get_json(silent=True) or {}
    if name in data:
        return (data.get(name) or "").strip()
    return (request.form.get(name, default) or "").strip()


def _log_event(action: str, slot: ParkingSlot, *, user_id=None, note=None, prev_status=None, new_status=None):
    """Append a row to parking_events. Commit happens in caller."""
    ev = ParkingEvent(
        action=action,
        lot_id=slot.lot_id,
        slot_id=slot.id,
        user_id=user_id,
        car_number=slot.car_number,
        prev_status=prev_status,
        new_status=new_status,
        note=note,
        ts=datetime.utcnow(),
    )
    db.session.add(ev)


def _release_expired_reservations():
    """Free any reserved slots whose reserved_until has passed, and log them."""
    now = datetime.utcnow()
    expired = ParkingSlot.query.filter(
        ParkingSlot.status == "reserved",
        ParkingSlot.reserved_until.isnot(None),
        ParkingSlot.reserved_until < now,
    ).all()
    changed = 0
    for s in expired:
        prev = s.status
        s.status = "empty"
        s.reserved_by_user_id = None
        s.car_number = None
        s.reserved_until = None
        _log_event("expire", s, user_id=None, note="Reservation expired", prev_status=prev, new_status=s.status)
        changed += 1
    if changed:
        db.session.commit()


def _calc_charge(started_at: datetime, ended_at: datetime):
    """
    Returns (minutes_total, amount_in_inr).
    New pricing:
      - ₹20 per hour (first hour included).
      - After 1 hour, every additional started hour adds ₹20.
    """
    seconds = max(0, int((ended_at - started_at).total_seconds()))
    minutes = (seconds + 59) // 60  # ceil to full minutes

    # ceil to full hours
    hours = (minutes + 59) // 60
    amount = hours * 20

    return minutes, amount



# ------------------------------------------------------------------
# Dashboards
# ------------------------------------------------------------------
@views_bp.route("/")
@login_required
def home():
    if getattr(current_user, "role", "user") == "admin":
        return render_template("dashboard_admin.html")
    return render_template("dashboard_user.html")


@views_bp.route("/dashboard")
@login_required
def user_dashboard():
    return render_template("dashboard_user.html")


@views_bp.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    _release_expired_reservations()

    # --- quick counts ---
    lots_count = db.session.query(func.count(ParkingLot.id)).scalar() or 0
    total_slots = db.session.query(func.count(ParkingSlot.id)).scalar() or 0
    occupied_count = (
        db.session.query(func.count(ParkingSlot.id))
        .filter(ParkingSlot.status == "parked")
        .scalar()
        or 0
    )

    # --- revenue today (IST day window) ---
    ist_offset = timedelta(hours=5, minutes=30)
    now_utc = datetime.utcnow()
    now_ist = now_utc + ist_offset
    ist_midnight = datetime(now_ist.year, now_ist.month, now_ist.day)  # 00:00 IST
    utc_start = ist_midnight - ist_offset
    utc_end = utc_start + timedelta(days=1)

    revenue_today_num = (
        db.session.query(func.coalesce(func.sum(ParkingSession.charge_amount), 0))
        .filter(
            ParkingSession.ended_at.isnot(None),
            ParkingSession.ended_at >= utc_start,
            ParkingSession.ended_at < utc_end,
        )
        .scalar()
        or 0
    )
    revenue_today = f"₹{revenue_today_num}"

    return render_template(
        "dashboard_admin.html",
        lots_count=lots_count,
        total_slots=total_slots,
        occupied_count=occupied_count,
        revenue_today=revenue_today,
    )
# ------------------------------------------------------------------
# Admin: Parking Lots
# ------------------------------------------------------------------
@views_bp.route("/admin/parking")
@login_required
@admin_required
def admin_parking_list():
    _release_expired_reservations()
    lots = ParkingLot.query.order_by(ParkingLot.id.desc()).all()
    return render_template("admin_parking_list.html", lots=lots)


@views_bp.route("/admin/parking/new", methods=["GET", "POST"])
@login_required
@admin_required
def admin_parking_new():
    if request.method == "POST":
        name = _get_json_or_form("name")
        total_slots_raw = _get_json_or_form("total_slots", "0")
        lat_raw = _get_json_or_form("latitude", "0")
        lng_raw = _get_json_or_form("longitude", "0")

        try:
            total_slots = int(total_slots_raw)
            lat = float(lat_raw)
            lng = float(lng_raw)
        except ValueError:
            flash("Invalid number/coordinates.", "warning")
            return redirect(url_for("views.admin_parking_new"))

        if not name or total_slots <= 0:
            flash("Please provide a name and a valid number of slots.", "warning")
            return redirect(url_for("views.admin_parking_new"))

        lot = ParkingLot(name=name, total_slots=total_slots, latitude=lat, longitude=lng)
        db.session.add(lot)
        db.session.flush()  # get lot.id before commit

        # Create N empty slots (1..N)
        slots = [ParkingSlot(lot_id=lot.id, number=i, status="empty") for i in range(1, total_slots + 1)]
        db.session.add_all(slots)
        db.session.commit()

        flash("Parking lot created!", "success")
        return redirect(url_for("views.admin_parking_list"))

    return render_template("admin_parking_form.html")


@views_bp.route("/admin/parking/<int:lot_id>")
@login_required
@admin_required
def admin_parking_detail(lot_id: int):
    _release_expired_reservations()
    lot = ParkingLot.query.get_or_404(lot_id)
    slots = ParkingSlot.query.filter_by(lot_id=lot.id).order_by(ParkingSlot.number.asc()).all()
    return render_template("admin_parking_detail.html", lot=lot, slots=slots)

# ------------------------------------------------------------------
# User: Find Parking (sorted by distance if lat/lng provided)
# ------------------------------------------------------------------
@views_bp.route("/parking")
@login_required
def user_parking_list():
    _release_expired_reservations()
    lots = ParkingLot.query.all()

    lat_q = request.args.get("lat")
    lng_q = request.args.get("lng")

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371.0  # km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    items = []
    if lat_q and lng_q:
        try:
            u_lat = float(lat_q)
            u_lng = float(lng_q)
            for lot in lots:
                dist = haversine(u_lat, u_lng, lot.latitude, lot.longitude)
                items.append({"lot": lot, "distance_km": dist})
            items.sort(key=lambda x: x["distance_km"])
        except ValueError:
            items = [{"lot": lot, "distance_km": None} for lot in sorted(lots, key=lambda l: l.name.lower())]
    else:
        items = [{"lot": lot, "distance_km": None} for lot in sorted(lots, key=lambda l: l.name.lower())]

    return render_template("user_parking_list.html", items=items)


@views_bp.route("/parking/<int:lot_id>")
@login_required
def user_parking_detail(lot_id: int):
    _release_expired_reservations()
    lot = ParkingLot.query.get_or_404(lot_id)
    slots = ParkingSlot.query.filter_by(lot_id=lot.id).order_by(ParkingSlot.number.asc()).all()
    return render_template("user_parking_detail.html", lot=lot, slots=slots)

# ------------------------------------------------------------------
# APIs: Live status for pages
# ------------------------------------------------------------------
@views_bp.route("/api/lot/<int:lot_id>/slots")
@login_required
def api_lot_slots(lot_id: int):
    _release_expired_reservations()
    lot = ParkingLot.query.get_or_404(lot_id)
    slots = ParkingSlot.query.filter_by(lot_id=lot.id).order_by(ParkingSlot.number.asc()).all()

    def serialize(s: ParkingSlot):
        return {
            "id": s.id,
            "number": s.number,
            "status": s.status,
            "car_number": s.car_number,
            "reserved_until": s.reserved_until.isoformat() + "Z" if s.reserved_until else None,
        }

    return jsonify({"ok": True, "slots": [serialize(s) for s in slots]})

# ------------------------------------------------------------------
# APIs: Reserve / Cancel / Check-in (with car number + 15-min hold)
# ------------------------------------------------------------------
@views_bp.route("/api/slot/<int:slot_id>/reserve", methods=["POST"])
@login_required
def api_slot_reserve(slot_id: int):
    _release_expired_reservations()
    slot = ParkingSlot.query.get_or_404(slot_id)
    if slot.status != "empty":
        return jsonify({"ok": False, "msg": "Slot not available"}), 400

    car_number = _get_json_or_form("car_number").upper()
    if not car_number:
        return jsonify({"ok": False, "msg": "Car number required"}), 400

    prev = slot.status
    slot.status = "reserved"
    slot.reserved_by_user_id = current_user.id
    slot.car_number = car_number
    slot.reserved_until = datetime.utcnow() + timedelta(minutes=15)  # 15-minute hold

    _log_event("reserve", slot, user_id=current_user.id, prev_status=prev, new_status=slot.status)
    db.session.commit()

    return jsonify({"ok": True, "status": slot.status, "reserved_until": slot.reserved_until.isoformat() + "Z"})


@views_bp.route("/api/slot/<int:slot_id>/cancel", methods=["POST"])
@login_required
def api_slot_cancel(slot_id: int):
    _release_expired_reservations()
    slot = ParkingSlot.query.get_or_404(slot_id)

    if slot.status != "reserved" or (
        slot.reserved_by_user_id not in [current_user.id] and getattr(current_user, "role", "user") != "admin"
    ):
        return jsonify({"ok": False, "msg": "Not allowed"}), 403

    prev = slot.status
    slot.status = "empty"
    slot.reserved_by_user_id = None
    slot.car_number = None
    slot.reserved_until = None

    _log_event("cancel", slot, user_id=current_user.id, prev_status=prev, new_status=slot.status)
    db.session.commit()

    return jsonify({"ok": True, "status": slot.status})


@views_bp.route("/api/slot/<int:slot_id>/checkin", methods=["POST"])
@login_required
def api_slot_checkin(slot_id: int):
    _release_expired_reservations()
    slot = ParkingSlot.query.get_or_404(slot_id)

    if slot.status == "parked":
        return jsonify({"ok": False, "msg": "Already parked"}), 400

    car_number = _get_json_or_form("car_number").upper()

    if slot.status == "reserved":
        # Only reserver or admin may check in
        if slot.reserved_by_user_id not in [current_user.id] and getattr(current_user, "role", "user") != "admin":
            return jsonify({"ok": False, "msg": "Not allowed"}), 403
        # If user provides a new car number, override
        if car_number:
            slot.car_number = car_number
    else:
        # Empty -> direct park requires car number
        if not car_number:
            return jsonify({"ok": False, "msg": "Car number required"}), 400
        slot.car_number = car_number
        slot.reserved_by_user_id = current_user.id

    prev = slot.status
    slot.status = "parked"
    slot.reserved_until = None

    _log_event("checkin", slot, user_id=current_user.id, prev_status=prev, new_status=slot.status)
    db.session.commit()

    return jsonify({"ok": True, "status": slot.status})

# ------------------------------------------------------------------
# Camera Webhooks (ANPR)
# ------------------------------------------------------------------
@views_bp.route("/api/camera/entry", methods=["POST"])
def api_camera_entry():
    """
    Accepts either:
      Manual: { "slot_id": 123, "lot_id": 7, "car_number": "KA01AB1234" }
      Auto:   { "car_number": "KA01AB1234" }  -> server finds reserved or first empty slot
    """
    data = request.get_json(silent=True) or {}
    car = (data.get("car_number") or "").strip().upper()
    lot_id  = data.get("lot_id")   # may be None in auto mode
    slot_id = data.get("slot_id")  # may be None in auto mode

    if not car:
        return jsonify({"ok": False, "msg": "car_number required"}), 400

    # ----- duplicate guard -----
    existing_slot = ParkingSlot.query.filter(
        ParkingSlot.status == "parked",
        ParkingSlot.car_number == car
    ).first()
    if existing_slot:
        return jsonify({
            "ok": False,
            "msg": f"Vehicle {car} is already parked in Lot {existing_slot.lot_id}, Slot #{existing_slot.number}.",
            "lot_id": existing_slot.lot_id,
            "slot_id": existing_slot.id
        }), 409

    existing_session = ParkingSession.query.filter_by(
        car_number=car, ended_at=None
    ).first()
    if existing_session:
        return jsonify({
            "ok": False,
            "msg": f"Vehicle {car} already has an active session (ID {existing_session.id}).",
            "session_id": existing_session.id,
            "lot_id": existing_session.lot_id,
            "slot_id": existing_session.slot_id
        }), 409
    # ---------------------------

    slot = None
    if slot_id and lot_id:
        # manual mode
        slot = ParkingSlot.query.get_or_404(int(slot_id))
        if slot.lot_id != int(lot_id):
            return jsonify({"ok": False, "msg": "slot/lot mismatch"}), 400
        if slot.status == "reserved" and slot.car_number and slot.car_number != car:
            return jsonify({"ok": False, "msg": "reserved for different vehicle"}), 409
        if slot.status == "parked" and slot.car_number == car:
            return jsonify({"ok": True, "msg": "already parked"})
    else:
        # auto-assign mode
        # 1) reserved for this car?
        slot = ParkingSlot.query.filter(
            ParkingSlot.status == "reserved",
            ParkingSlot.car_number == car
        ).order_by(ParkingSlot.id.asc()).first()
        # 2) else any empty
        if not slot:
            slot = ParkingSlot.query.filter_by(status="empty").order_by(ParkingSlot.id.asc()).first()
        if not slot:
            return jsonify({"ok": False, "msg": "Parking full. No slots available."}), 409

    prev = slot.status
    slot.status = "parked"
    slot.car_number = car
    slot.reserved_until = None

    active = ParkingSession.query.filter_by(slot_id=slot.id, ended_at=None).first()
    if not active:
        ps = ParkingSession(
            lot_id=slot.lot_id,
            slot_id=slot.id,
            car_number=car,
            started_at=datetime.utcnow(),
            source="camera",
        )
        db.session.add(ps)

    _log_event("checkin", slot, user_id=None, note="camera entry", prev_status=prev, new_status=slot.status)
    db.session.commit()

    return jsonify({"ok": True, "msg": "Checked-in", "lot_id": slot.lot_id, "slot_id": slot.id, "car_number": car})

@views_bp.route("/api/camera/exit", methods=["POST"])
def api_camera_exit():
    """
    Payload (JSON) accepts either:
      { "lot_id": 7, "slot_id": 123, "car_number": "KA01AB1234" }
      or
      { "lot_id": 7, "car_number": "KA01AB1234" }

    - Finds the active session by slot_id if provided else by (lot_id, car_number).
    - Ends the session, computes bill, frees slot.
    """
    data = request.get_json(silent=True) or {}
    lot_id  = int(data.get("lot_id") or 0)
    car     = (data.get("car_number") or "").strip().upper()
    slot_id = data.get("slot_id")
    slot_id = int(slot_id) if str(slot_id or "").isdigit() else None

    if not lot_id or (not slot_id and not car):
        return jsonify({"ok": False, "msg": "lot_id and (slot_id or car_number) required"}), 400

    # Find active session
    if slot_id:
        ps = (ParkingSession.query
              .filter_by(slot_id=slot_id, ended_at=None)
              .order_by(ParkingSession.started_at.desc())
              .first())
    else:
        ps = (ParkingSession.query
              .filter_by(lot_id=lot_id, car_number=car, ended_at=None)
              .order_by(ParkingSession.started_at.desc())
              .first())

    if not ps:
        # No open session; if slot_id is given, free the slot anyway
        if slot_id:
            slot = ParkingSlot.query.get_or_404(slot_id)
            prev = slot.status
            slot.status = "empty"
            slot.reserved_by_user_id = None
            slot.car_number = None
            slot.reserved_until = None
            _log_event("cancel", slot, user_id=None, note="camera exit (no session)", prev_status=prev, new_status=slot.status)
            db.session.commit()
        return jsonify({"ok": True, "bill": None, "msg": "no active session"})

    # Close session
    ps.ended_at = datetime.utcnow()
    minutes, amount = _calc_charge(ps.started_at, ps.ended_at)
    ps.minutes_total = minutes
    ps.charge_amount = amount
    ps.acknowledged = False

    # Free slot
    slot = ParkingSlot.query.get(ps.slot_id)
    if slot:
        prev = slot.status
        slot.status = "empty"
        slot.reserved_by_user_id = None
        slot.car_number = None
        slot.reserved_until = None
        _log_event("cancel", slot, user_id=None, note="camera exit", prev_status=prev, new_status=slot.status)

    db.session.commit()

    bill = {
        "session_id": ps.id,
        "car_number": ps.car_number,
        "minutes_total": ps.minutes_total,
        "amount_inr": ps.charge_amount,
        "started_at": ps.started_at.isoformat() + "Z",
        "ended_at": ps.ended_at.isoformat() + "Z",
        "slot_id": ps.slot_id,
        "lot_id": ps.lot_id,
    }
    return jsonify({"ok": True, "bill": bill})


# ------------------------------------------------------------------
# Admin: Billing model polling & ack
# ------------------------------------------------------------------
@views_bp.route("/api/lot/<int:lot_id>/pending_charges")
@login_required
@admin_required
def api_pending_charges(lot_id: int):
    cutoff = datetime.utcnow() - timedelta(hours=6)  # recent only
    rows = (ParkingSession.query
            .filter_by(lot_id=lot_id, acknowledged=False)
            .filter(ParkingSession.ended_at.isnot(None))
            .filter(ParkingSession.ended_at >= cutoff)
            .order_by(ParkingSession.ended_at.desc())
            .all())
    out = [{
        "session_id": r.id,
        "car_number": r.car_number,
        "minutes_total": r.minutes_total,
        "amount_inr": r.charge_amount,
        "started_at": r.started_at.isoformat() + "Z",
        "ended_at": r.ended_at.isoformat() + "Z",
        "slot_id": r.slot_id
    } for r in rows]
    return jsonify({"ok": True, "items": out})


@views_bp.route("/api/session/<int:session_id>/ack", methods=["POST"])
@login_required
@admin_required
def api_session_ack(session_id: int):
    ps = ParkingSession.query.get_or_404(session_id)
    ps.acknowledged = True
    db.session.commit()
    return jsonify({"ok": True})

# ------------------------------------------------------------------
# Admin: History
# ------------------------------------------------------------------
@views_bp.route("/admin/history")
@login_required
@admin_required
def admin_history():
    # Query params
    q = (request.args.get("q") or "").strip().upper()   # vehicle number search
    date_from = (request.args.get("from") or "").strip()
    date_to   = (request.args.get("to") or "").strip()
    lot_id    = request.args.get("lot_id", type=int)
    action    = (request.args.get("action") or "").strip().lower()  # reserve|cancel|checkin|expire

    # Base query
    evq = ParkingEvent.query

    # Filters
    if q:
        evq = evq.filter(ParkingEvent.car_number.ilike(f"%{q}%"))
    if lot_id:
        evq = evq.filter(ParkingEvent.lot_id == lot_id)
    if action in {"reserve", "cancel", "checkin", "expire"}:
        evq = evq.filter(ParkingEvent.action == action)

    # Date range (YYYY-MM-DD)
    try:
        if date_from:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            evq = evq.filter(ParkingEvent.ts >= dt_from)
        if date_to:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d")
            evq = evq.filter(ParkingEvent.ts < dt_to + timedelta(days=1))  # inclusive end-day
    except ValueError:
        flash("Invalid date format. Use YYYY-MM-DD.", "warning")

    # Order newest first
    evq = evq.order_by(ParkingEvent.ts.desc())

    # Pagination
    page = request.args.get("page", default=1, type=int)
    per_page = 25
    events = evq.paginate(page=page, per_page=per_page, error_out=False)

    lots = ParkingLot.query.order_by(ParkingLot.name.asc()).all()

    # >>> ADD THESE TWO LINES <<<
    lot_names = {l.id: l.name for l in lots}
    slot_numbers = {s.id: s.number for s in ParkingSlot.query.all()}

    return render_template(
        "admin_history.html",
        events=events,
        lots=lots,
        q=q,
        date_from=date_from,
        date_to=date_to,
        action=action,
        lot_id=lot_id,
        # >>> PASS THEM INTO THE TEMPLATE <<<
        lot_names=lot_names,
        slot_numbers=slot_numbers,
    )




# -------------------------
# Admin: Bills (completed sessions)
# -------------------------
from sqlalchemy import and_
from flask import Response
import csv
from io import StringIO

@views_bp.route("/admin/bills")
@login_required
@admin_required
def admin_bills():
    """
    Server-rendered list with filters & pagination.
    Shows only completed sessions (ended_at not null).
    """
    # filters
    q_car      = (request.args.get("car") or "").strip().upper()
    lot_id     = request.args.get("lot_id", type=int)
    only_open  = request.args.get("open", type=int)  # 1=only unacknowledged
    date_from  = (request.args.get("from") or "").strip()
    date_to    = (request.args.get("to") or "").strip()

    rows = ParkingSession.query.filter(ParkingSession.ended_at.isnot(None))

    if lot_id:
        rows = rows.filter(ParkingSession.lot_id == lot_id)
    if q_car:
        rows = rows.filter(ParkingSession.car_number.ilike(f"%{q_car}%"))
    if only_open == 1:
        rows = rows.filter(ParkingSession.acknowledged == False)  # noqa: E712

    # date range (YYYY-MM-DD on ended_at)
    try:
        if date_from:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            rows = rows.filter(ParkingSession.ended_at >= dt_from)
        if date_to:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d")
            rows = rows.filter(ParkingSession.ended_at < dt_to + timedelta(days=1))
    except ValueError:
        flash("Invalid date format. Use YYYY-MM-DD.", "warning")

    rows = rows.order_by(ParkingSession.ended_at.desc())

    # pagination
    page = request.args.get("page", default=1, type=int)
    per_page = 25
    bills = rows.paginate(page=page, per_page=per_page, error_out=False)

    lots = ParkingLot.query.order_by(ParkingLot.name.asc()).all()

    return render_template(
        "admin_bills.html",
        bills=bills,
        lots=lots,
        q_car=q_car,
        lot_id=lot_id,
        only_open=only_open or 0,
        date_from=date_from,
        date_to=date_to,
    )


@views_bp.route("/api/admin/bills/ack", methods=["POST"])
@login_required
@admin_required
def api_admin_bills_ack():
    """
    Body: { session_id: <int> }
    Marks the bill as acknowledged (admin saw/closed it).
    """
    data = request.get_json(silent=True) or {}
    sid = int(data.get("session_id") or 0)
    if not sid:
        return jsonify({"ok": False, "msg": "session_id required"}), 400
    ps = ParkingSession.query.get_or_404(sid)
    if ps.ended_at is None:
        return jsonify({"ok": False, "msg": "Session still active"}), 400
    ps.acknowledged = True
    db.session.commit()
    return jsonify({"ok": True})


@views_bp.route("/admin/bills.csv")
@login_required
@admin_required
def admin_bills_csv():
    """
    CSV export respecting the same filters as /admin/bills.
    """
    q_car      = (request.args.get("car") or "").strip().upper()
    lot_id     = request.args.get("lot_id", type=int)
    only_open  = request.args.get("open", type=int)
    date_from  = (request.args.get("from") or "").strip()
    date_to    = (request.args.get("to") or "").strip()

    rows = ParkingSession.query.filter(ParkingSession.ended_at.isnot(None))
    if lot_id:
        rows = rows.filter(ParkingSession.lot_id == lot_id)
    if q_car:
        rows = rows.filter(ParkingSession.car_number.ilike(f"%{q_car}%"))
    if only_open == 1:
        rows = rows.filter(ParkingSession.acknowledged == False)  # noqa: E712

    try:
        if date_from:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            rows = rows.filter(ParkingSession.ended_at >= dt_from)
        if date_to:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d")
            rows = rows.filter(ParkingSession.ended_at < dt_to + timedelta(days=1))
    except ValueError:
        pass

    rows = rows.order_by(ParkingSession.ended_at.desc()).all()

    # build CSV
    sio = StringIO()
    writer = csv.writer(sio)
    writer.writerow(["SessionID", "LotID", "SlotID", "Car", "StartedAt", "EndedAt", "Minutes", "AmountINR", "Ack"])
    for r in rows:
        writer.writerow([
            r.id,
            r.lot_id,
            r.slot_id,
            r.car_number,
            r.started_at.isoformat() + "Z" if r.started_at else "",
            r.ended_at.isoformat() + "Z" if r.ended_at else "",
            r.minutes_total or "",
            r.charge_amount or "",
            "yes" if r.acknowledged else "no",
        ])
    out = sio.getvalue()
    return Response(
        out,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=bills.csv"}
    )


@views_bp.route("/admin/parking/<int:lot_id>/delete", methods=["POST"])
@login_required
@admin_required
def admin_parking_delete(lot_id: int):
    from sqlalchemy import func
    lot = ParkingLot.query.get_or_404(lot_id)

    parked_count = (
        db.session.query(func.count(ParkingSlot.id))
        .filter(ParkingSlot.lot_id == lot.id, ParkingSlot.status == "parked")
        .scalar() or 0
    )
    if parked_count > 0:
        flash(f"Cannot delete: {parked_count} slot(s) are currently parked.", "warning")
        return redirect(url_for("views.admin_parking_detail", lot_id=lot.id))

    # Remove history/sessions if you want a hard delete of everything:
    db.session.query(ParkingEvent).filter(ParkingEvent.lot_id == lot.id).delete(synchronize_session=False)
    db.session.query(ParkingSession).filter(ParkingSession.lot_id == lot.id).delete(synchronize_session=False)

    db.session.delete(lot)  # slots cascade delete
    db.session.commit()
    flash("Parking lot deleted.", "success")
    return redirect(url_for("views.admin_parking_list"))
