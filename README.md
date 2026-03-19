Prompt for Codex (Smart Parking System — Flask + MySQL)

Goal: Generate a production‑ready Flask application called smart_parking with a MySQL database using SQLAlchemy ORM and Alembic/Flask‑Migrate for migrations. Build both Admin and User flows. Implement 15‑minute booking holds, camera number‑plate check‑in, walk‑in assignment, and complete audit/history.

Tech stack & standards

Python 3.11+, Flask 3.x, SQLAlchemy 2.x, Flask‑Migrate (Alembic), Marshmallow (schemas), Flask‑JWT‑Extended (JWT auth).

MySQL 8, using mysqlclient or pymysql.

Blueprints for modular routing: admin, user, public, webhooks.

Services layer (business logic), Repositories (DB access), Schemas (serialize/validate).

Background scheduler for expiration checks (APScheduler) OR DB queries on demand to enforce expiry.

Env config via .env: DB URL, JWT secret, ALLOWED_ORIGINS.

Logging (structured), error handlers, request validation, pagination, sorting, filtering.

Seed scripts for demo data.

Dockerfile + docker‑compose for local run (app + MySQL).

Unit tests (pytest) for core services (bookings, check‑in logic).

Entities & schema (SQLAlchemy models + migrations)

Create models and migrations for:

User

id (PK), email (unique), password_hash, full_name, phone, is_active, created_at, updated_at

Role

id (PK), name in {admin, user}

M2M user_roles join table

Vehicle

id (PK), user_id (FK users.id), plate_number (unique), make_model (nullable), color (nullable), created_at

ParkingLot

id (PK), name, location_address, latitude (nullable), longitude (nullable), timezone (default), is_active

ParkingSlot

id (PK), lot_id (FK lots.id), label (e.g., “A‑12”), is_active, is_occupied (derived), level (nullable), type (standard/EV/accessible), unique (lot_id, label)

Booking

id (PK), user_id (FK), vehicle_id (FK), lot_id (FK), slot_id (FK, nullable until assigned), status in {HOLD,ACTIVE,COMPLETED,CANCELLED,EXPIRED}, reserved_from (datetime), reserved_to (nullable), hold_expires_at (datetime = created_at + 15 minutes), checkin_time (nullable), checkout_time (nullable), indexes on (vehicle_id, status), (lot_id, status), (hold_expires_at)

CameraEvent

id, lot_id (FK), plate_number, captured_at, confidence (float), direction in {ENTRY,EXIT}, raw_payload (JSON)

GateEvent

id, lot_id (FK), action in {OPEN,DENY}, reason (text), triggered_at, booking_id (nullable)

AuditLog

id, actor_user_id (nullable), action, entity, entity_id, meta (JSON), created_at

Important constraints

A HOLD booking means “reserved, not yet arrived.” It is valid only until hold_expires_at (15 minutes after creation). If no check‑in before that time, auto‑set to EXPIRED and free the slot.

On check‑in (camera entry match), if a HOLD exists for that plate and lot within the valid window, change to ACTIVE, assign a slot (if not already) and open gate.

If user arrives without booking (walk‑in) and a slot is available, create a walk‑in booking with status ACTIVE, and open the gate.

When leaving (camera exit), set booking to COMPLETED and free the slot.

Business rules & services

Implement service functions with transactions:

BookingService.create_hold(user_id, vehicle_id, lot_id):

Find a free slot; lock row for update; create booking with status HOLD, set hold_expires_at = now() + 15min, set slot_id.

BookingService.cancel(booking_id, actor_user_id):

Allowed for owner or admin. Set status CANCELLED, free slot.

BookingService.expire_overdue_holds():

Find HOLD where hold_expires_at < now(); set EXPIRED, free slots; write AuditLog.

CheckinService.process_plate_entry(lot_id, plate_number, confidence, raw_payload):

If HOLD booking for that plate & lot and still valid → set ACTIVE, checkin_time=now(), open gate (GateService.open(lot_id, booking_id, reason="valid_hold")).

Else if available slot → create walk‑in ACTIVE booking (user is unknown → option A: guest record; option B: deny if policy). For now: allow guest with user_id = null and vehicle by plate.

Else → deny gate, create GateEvent with reason "no slots".

CheckoutService.process_plate_exit(lot_id, plate_number):

Find ACTIVE booking by vehicle plate; set COMPLETED, checkout_time=now(), free slot; log gate open for exit.

SlotService.find_free_slot(lot_id, type=None) with FOR UPDATE SKIP LOCKED pattern.

Write everything idempotent where possible.

API endpoints (JWT; role‑based)

Base URL /api.

Auth

POST /auth/register (user): email, password, name, phone

POST /auth/login → JWT tokens

GET /auth/me → profile + vehicles

User panel

GET /lots → list lots with counts: total slots, free slots, occupied

GET /lots/{lot_id}/slots/availability → free vs total

POST /vehicles → add vehicle (plate)

POST /bookings → create a 15‑min HOLD (requires vehicle_id, lot_id)

POST /bookings/{id}/cancel

GET /bookings/my → current & history

(Optional) WebSocket/SSE for live updates

Admin panel

POST /admin/lots (name, location, coords)

POST /admin/lots/{lot_id}/slots (bulk create: count, level, type; or list of labels)

GET /admin/dashboard:

metrics: active bookings, holds, expired, cancelled, completed (time range filters)

per‑lot utilization and occupancy

GET /admin/bookings with filters (status, lot_id, plate, date range, user)

GET /admin/slots?lot_id=... (status)

GET /admin/audit (latest actions)

Camera/Gate integration (webhooks)

POST /webhooks/camera Payload: { lot_id, plate_number, direction: "ENTRY"|"EXIT", captured_at, confidence, raw_payload } Behavior: call CheckinService or CheckoutService accordingly and return { action: "OPEN"|"DENY", booking_id, reason }.

POST /webhooks/gate/open (optional stub to external hardware)

Walk‑in policy

If ENTRY with no valid hold, try assign free slot:

If found → create ACTIVE walk‑in booking with vehicle.plate_number, user_id = null (guest), return OPEN.

If none → return DENY.

Expiration of holds (15 minutes)

Implement both:

A scheduled job every minute to call expire_overdue_holds().

A DB filter everywhere that treats holds as expired if now() > hold_expires_at.

Ensure slot freeing is atomic and safe with transactions.

Admin & User UI (Flask templates or simple React stub)

For this task, at least provide Flask Jinja pages:

Admin: create lot, bulk add slots, dashboard with charts (can be simple tables), bookings list with filters, audit stream.

User: login/register, my vehicles, make a booking (select lot & plate), my bookings.

Keep UI minimal but functional; API is primary.

Directory layout smart_parking/ app.py config.py extensions.py # db, migrate, jwt, scheduler models/ # user.py, role.py, vehicle.py, lot.py, slot.py, booking.py, camera.py, gate.py, audit.py, init.py schemas/ # marshmallow schemas repositories/ services/ # booking_service.py, checkin_service.py, slot_service.py, etc. blueprints/ auth/ user/ admin/ webhooks/ templates/ # simple admin & user pages static/ migrations/ # Alembic seeds/ tests/ Dockerfile docker-compose.yml requirements.txt

Security & correctness

Password hashing with werkzeug.security.

JWT with access/refresh tokens, role check decorator.

Input validation via Marshmallow; return 400 with messages.

Use DB transactions for booking/check‑in/slot assignment.

Unique constraints: plate_number, (lot_id, label), only one ACTIVE booking per vehicle.

Indexes on status/time columns for dashboards.

Protection against double‑open/duplicate camera events (idempotency keys by timestamp+plate).

Seed data

Create admin user (admin@demo.dev / Admin@123).

Create sample lot “City Center Lot” with 50 slots (A‑1…A‑50).

Create two normal users and vehicles (KA01AB1234, MH12CD5678).

Create some sample history (cancelled/expired/completed) for dashboard.

Commands & migrations

Provide Makefile or docs for:

flask db init, flask db migrate -m "initial", flask db upgrade

flask seed run

flask run

Docker compose up/down.

Include .env.example with DB URL and JWT secret.

Tests (pytest)

Test: create hold → auto‑expire after 15 min mock → cannot check‑in.

Test: create hold → camera entry within window → becomes ACTIVE, gate opens.

Test: walk‑in with free slot → ACTIVE booking created.

Test: exit → booking completes, slot freed.

Test: only one active booking per vehicle enforced.

Minimal ERD (for docs/readme) User 1—* Vehicle ParkingLot 1—* ParkingSlot Vehicle 1—* Booking ParkingLot 1—* Booking ParkingSlot 1—* Booking Booking 1—0..1 GateEvent (via events)

Deliverables: Fully working Flask app with the above models, migrations, endpoints, seeders, and minimal Jinja templates. Return the full project source tree with key files, plus README explaining setup.
