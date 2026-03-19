from datetime import datetime
from . import db, login_manager
from flask_login import UserMixin
from sqlalchemy.orm import relationship
# -------------------------
# Users
# -------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_id(self):
        return str(self.id)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# -------------------------
# Parking Lot & Slot
# -------------------------
class ParkingLot(db.Model):
    __tablename__ = "parking_lots"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    total_slots = db.Column(db.Integer, nullable=False)

    slots = db.relationship(
        "ParkingSlot",
        backref="lot",
        lazy=True,
        cascade="all, delete-orphan"
    )


class ParkingSlot(db.Model):
    __tablename__ = "parking_slots"
    id = db.Column(db.Integer, primary_key=True)
    lot_id = db.Column(db.Integer, db.ForeignKey("parking_lots.id"), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="empty")  # empty|reserved|parked
    reserved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    car_number = db.Column(db.String(32), nullable=True)       # e.g., KA01AB1234
    reserved_until = db.Column(db.DateTime, nullable=True)     # UTC expiry for reservation

    __table_args__ = (
        db.UniqueConstraint("lot_id", "number", name="uq_slot_per_lot"),
    )


# -------------------------
# Audit Events
# -------------------------
# class ParkingEvent(db.Model):
#     __tablename__ = "parking_events"

#     id = db.Column(db.Integer, primary_key=True)
#     ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)  # UTC timestamp
#     action = db.Column(db.String(32), nullable=False)  # reserve|cancel|checkin|expire
#     lot_id = db.Column(db.Integer, db.ForeignKey("parking_lots.id"), nullable=False, index=True)
#     slot_id = db.Column(db.Integer, db.ForeignKey("parking_slots.id"), nullable=False, index=True)
#     user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

#     car_number = db.Column(db.String(32), nullable=True, index=True)
#     prev_status = db.Column(db.String(20), nullable=True)  # empty|reserved|parked
#     new_status  = db.Column(db.String(20), nullable=True)

#     note = db.Column(db.String(255), nullable=True)

class ParkingEvent(db.Model):
    __tablename__ = "parking_events"

    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    action = db.Column(db.String(32), nullable=False)  # reserve|cancel|checkin|expire
    lot_id = db.Column(db.Integer, db.ForeignKey("parking_lots.id"), nullable=False, index=True)
    slot_id = db.Column(db.Integer, db.ForeignKey("parking_slots.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    car_number = db.Column(db.String(32), nullable=True, index=True)
    prev_status = db.Column(db.String(20), nullable=True)  # empty|reserved|parked
    new_status  = db.Column(db.String(20), nullable=True)
    note = db.Column(db.String(255), nullable=True)

    # NEW: relationships (no migration needed)
    lot  = relationship("ParkingLot", lazy="joined")
    slot = relationship("ParkingSlot", lazy="joined")
    user = relationship("User",       lazy="joined")

# -------------------------
# Parking Sessions (ANPR / Billing)
# -------------------------
class ParkingSession(db.Model):
    __tablename__ = "parking_sessions"

    id = db.Column(db.Integer, primary_key=True)

    lot_id = db.Column(db.Integer, db.ForeignKey("parking_lots.id"), nullable=False, index=True)
    slot_id = db.Column(db.Integer, db.ForeignKey("parking_slots.id"), nullable=False, index=True)

    car_number = db.Column(db.String(32), nullable=False, index=True)

    # default ensures automatic start time when created from code without explicit timestamp
    started_at = db.Column(db.DateTime, nullable=False, index=True, default=datetime.utcnow)
    ended_at   = db.Column(db.DateTime, nullable=True, index=True)

    minutes_total = db.Column(db.Integer, nullable=True)
    charge_amount = db.Column(db.Integer, nullable=True)  # INR (integer)

    source = db.Column(db.String(16), nullable=False, default="camera")  # camera|manual
    acknowledged = db.Column(db.Boolean, nullable=False, default=False)  # admin saw/closed the bill
