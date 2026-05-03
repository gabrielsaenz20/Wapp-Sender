import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from dotenv import load_dotenv

import openpyxl

from database import engine, get_db, Base, SessionLocal
import models
from auth import (
    hash_password,
    verify_password,
    create_session_token,
    get_current_user_id,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
)
from waha_client import WAHAClient

load_dotenv()

logger = logging.getLogger(__name__)

# Quito, Ecuador timezone (UTC-5, no DST)
QUITO_TZ = ZoneInfo("America/Guayaquil")

# ---------------------------------------------------------------------------
# Human-simulation settings for outgoing messages
# ---------------------------------------------------------------------------

# When HUMAN_SIMULATE=true (default), each message is preceded by a read
# receipt, a typing indicator whose duration matches the message length, and
# randomised inter-message delays – making the traffic pattern indistinguishable
# from a real user typing on their phone.
HUMAN_SIMULATE = os.getenv("HUMAN_SIMULATE", "true").lower() in ("1", "true", "yes")

# Random delay range (seconds) between consecutive messages.
MESSAGE_MIN_DELAY = float(os.getenv("MESSAGE_MIN_DELAY", "5.0"))
MESSAGE_MAX_DELAY = float(os.getenv("MESSAGE_MAX_DELAY", "20.0"))

# Typing speed used to compute how long the "typing…" indicator is shown.
# 200 CPM (≈ 40 WPM) is a comfortable, realistic average for mobile typing.
TYPING_CPM = float(os.getenv("TYPING_CPM", "200.0"))

# Probability (0.0–1.0) that a "seen" read receipt is sent before typing.
SEEN_PROBABILITY = float(os.getenv("SEEN_PROBABILITY", "0.85"))

# Legacy single-value delay kept for backward-compat when HUMAN_SIMULATE=false.
MESSAGE_SEND_DELAY = float(os.getenv("MESSAGE_SEND_DELAY", "1.0"))

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Wapp Sender")

# Serve static files if the directory exists
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")


def _quito_fmt(dt, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Jinja2 filter: converts a naive-UTC datetime to a Quito-local time string."""
    if dt is None:
        return "–"
    return dt.replace(tzinfo=timezone.utc).astimezone(QUITO_TZ).strftime(fmt)


templates.env.filters["quito_fmt"] = _quito_fmt

# Create all tables on startup
Base.metadata.create_all(bind=engine)


_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_identifier(name: str) -> str:
    """Return *name* double-quoted for use in DDL, after validating it is a
    plain alphanumeric/underscore identifier (no spaces, no special chars).
    This prevents accidental SQL injection from unexpected metadata values.
    """
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier rejected: {name!r}")
    return f'"{name}"'


def _sql_default(value) -> str:
    """Convert a Python scalar ORM default value to its SQL literal form."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    # For strings, use single-quoted SQL literals with internal single-quotes escaped.
    return "'" + str(value).replace("'", "''") + "'"


def _migrate_db() -> None:
    """Add any columns that exist in the ORM models but are missing from the DB.

    SQLAlchemy's create_all only creates new tables; it never alters existing
    ones.  This function handles forward-only schema evolution for SQLite by
    introspecting each table with PRAGMA table_info and issuing
    ALTER TABLE … ADD COLUMN for any missing column.
    """
    from sqlalchemy import inspect, text as sa_text

    with engine.connect() as conn:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                # Table doesn't exist yet; create_all will handle it.
                continue
            tbl = _safe_identifier(table.name)
            rows = conn.execute(sa_text(f"PRAGMA table_info({tbl})")).fetchall()
            existing_columns = {row[1] for row in rows}  # column name is index 1
            for column in table.columns:
                if column.primary_key or column.name in existing_columns:
                    continue
                col = _safe_identifier(column.name)
                col_type = column.type.compile(engine.dialect)
                # SQLite only allows adding nullable columns (or columns with a default)
                # via ALTER TABLE.  Use NULL default for nullable columns with no default.
                if column.default is not None and column.default.is_scalar:
                    default_clause = f" DEFAULT {_sql_default(column.default.arg)}"
                elif column.nullable:
                    default_clause = " DEFAULT NULL"
                else:
                    default_clause = ""
                ddl = f"ALTER TABLE {tbl} ADD COLUMN {col} {col_type}{default_clause}"
                conn.execute(sa_text(ddl))
                logger.info("DB migration: added column %s.%s", table.name, column.name)
        conn.commit()


_migrate_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now_quito_input_str() -> str:
    """Return the current Quito time formatted for a datetime-local HTML input."""
    return datetime.now(QUITO_TZ).strftime("%Y-%m-%dT%H:%M")


def _parse_quito_dt(dt_str: str) -> Optional[datetime]:
    """Parse a datetime-local input string (Quito local time) → naive UTC datetime."""
    if not dt_str:
        return None
    try:
        local_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M").replace(tzinfo=QUITO_TZ)
        return local_dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _get_user(request: Request, db: Session) -> Optional[models.User]:
    user_id = get_current_user_id(request)
    if not user_id:
        return None
    return db.query(models.User).filter(models.User.id == user_id).first()


def _require_user(request: Request, db: Session) -> models.User:
    user = _get_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def _get_waha_client(settings: Optional[models.WAHASettings]) -> Optional[WAHAClient]:
    if not settings:
        return None
    return WAHAClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        session_name=settings.session_name,
    )


def _normalize_wa_status(wa_status: Optional[dict]) -> Optional[dict]:
    """Normalise WAHA session-status response so templates can safely call
    .get() on every nested field regardless of WAHA version quirks.

    Known variations:
    - ``me`` may be a plain string (phone number) instead of a dict.
    - ``me.id`` may be a plain string like "5219981234567@c.us" instead of
      the nested dict ``{"user": "5219981234567"}``.
    """
    if not isinstance(wa_status, dict):
        return wa_status
    me = wa_status.get("me")
    if isinstance(me, str):
        wa_status["me"] = {"pushName": me, "id": {}}
    elif isinstance(me, dict):
        raw_id = me.get("id")
        if isinstance(raw_id, str):
            me["id"] = {"user": raw_id.split("@")[0]}
    return wa_status


def _flash(request: Request) -> dict:
    """Pull flash messages from cookie and return as dict."""
    flash = {}
    for key in ("flash_success", "flash_error", "flash_info"):
        val = request.cookies.get(key)
        if val:
            flash[key] = unquote(val)
    return flash


import re

_SAFE_REDIRECT_RE = re.compile(r"^/[a-zA-Z0-9_\-/]*$")
_ALLOWED_FLASH_KEYS = frozenset(("flash_success", "flash_error", "flash_info"))


def _redirect_with_flash(url: str, key: str, message: str) -> RedirectResponse:
    # Ensure url is an internal path only – prevents open redirect attacks.
    # Only allows paths made of alphanumerics, slashes, hyphens, and underscores.
    if not _SAFE_REDIRECT_RE.match(url):
        url = "/"
    if key not in _ALLOWED_FLASH_KEYS:
        key = "flash_info"
    response = RedirectResponse(url=url, status_code=302)
    # URL-quote the message value to prevent cookie injection (e.g. newlines, semicolons)
    response.set_cookie(key, quote(message), max_age=5, httponly=True, samesite="lax")
    return response


def _base_ctx(request: Request, db: Session, active_page: str = "") -> dict:
    user = _get_user(request, db)
    ctx = {"request": request, "current_user": user, "active_page": active_page}
    ctx.update(_flash(request))
    return ctx


def _ensure_admin(db: Session):
    """Create default admin user if no users exist."""
    if db.query(models.User).count() == 0:
        admin_username = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
        user = models.User(
            username=admin_username,
            password_hash=hash_password(admin_password),
        )
        db.add(user)
        db.commit()


# Ensure default admin user exists at import time (reliable in both tests and production)
_startup_db = SessionLocal()
try:
    _ensure_admin(_startup_db)
finally:
    _startup_db.close()


# ---------------------------------------------------------------------------
# Scheduler loop – fires scheduled campaigns automatically
# ---------------------------------------------------------------------------

SCHEDULER_INTERVAL = float(os.getenv("SCHEDULER_INTERVAL", "30"))


async def _scheduler_loop():
    """Background loop that checks for scheduled campaigns every SCHEDULER_INTERVAL seconds."""
    while True:
        try:
            db = SessionLocal()
            try:
                now = _utcnow()
                due_campaigns = (
                    db.query(models.Campaign)
                    .filter(
                        models.Campaign.status == "scheduled",
                        models.Campaign.scheduled_at <= now,
                    )
                    .all()
                )
                for campaign in due_campaigns:
                    settings = (
                        db.query(models.WAHASettings)
                        .filter_by(user_id=campaign.user_id)
                        .first()
                    )
                    if not settings:
                        logger.warning(
                            "Scheduled campaign %d has no WAHA settings – skipping.", campaign.id
                        )
                        continue

                    client = _get_waha_client(settings)
                    campaign.status = "sending"
                    campaign.sent_at = _utcnow()

                    # Collect unique contacts (by phone) across all campaign lists
                    seen_phones: set[str] = set()
                    contacts_to_send: list[models.Contact] = []
                    for cl_link in campaign.lists:
                        for contact in cl_link.contact_list.contacts:
                            if contact.phone not in seen_phones:
                                seen_phones.add(contact.phone)
                                contacts_to_send.append(contact)

                    campaign.total_contacts = len(contacts_to_send)
                    db.commit()

                    # Create pending log entries
                    log_ids = []
                    for contact in contacts_to_send:
                        log = models.MessageLog(
                            campaign_id=campaign.id,
                            contact_id=contact.id,
                            phone=contact.phone,
                            contact_name=contact.name,
                            message=_render_message(campaign.message_template, contact),
                            status="pending",
                        )
                        db.add(log)
                        db.flush()
                        log_ids.append(log.id)
                    db.commit()

                    # Fire off message sending as a background task
                    asyncio.create_task(
                        _send_campaign_messages(campaign.id, log_ids, client)
                    )
                    logger.info(
                        "Scheduler: fired campaign %d ('%s') with %d contacts.",
                        campaign.id,
                        campaign.name,
                        len(contacts_to_send),
                    )
            finally:
                db.close()
        except Exception:
            logger.exception("Error in scheduler loop")
        await asyncio.sleep(SCHEDULER_INTERVAL)


@app.on_event("startup")
async def on_startup():
    """Called by uvicorn on startup – ensure admin user exists and start scheduler."""
    db = SessionLocal()
    try:
        _ensure_admin(db)
    finally:
        db.close()
    asyncio.create_task(_scheduler_loop())


# ---------------------------------------------------------------------------
# Excel import helper
# ---------------------------------------------------------------------------

PHONE_ALIASES = {"phone", "telefono", "teléfono", "celular", "movil", "móvil", "tel", "whatsapp"}
NAME_ALIASES = {"name", "nombre", "names", "full_name", "fullname", "contacto"}


def _parse_excel(file_bytes: bytes) -> list[dict]:
    wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    headers = [str(h).strip().lower() if h else "" for h in rows[0]]

    # Find name and phone column indices
    name_idx = next((i for i, h in enumerate(headers) if h in NAME_ALIASES), None)
    phone_idx = next((i for i, h in enumerate(headers) if h in PHONE_ALIASES), None)

    if name_idx is None or phone_idx is None:
        raise ValueError(
            "El archivo Excel debe tener columnas 'name' (o nombre) y 'phone' (o telefono/celular)."
        )

    contacts = []
    for row in rows[1:]:
        if not any(row):
            continue
        name_val = row[name_idx] if name_idx < len(row) else None
        phone_val = row[phone_idx] if phone_idx < len(row) else None
        if not name_val or not phone_val:
            continue
        extra = {}
        for i, h in enumerate(headers):
            if i not in (name_idx, phone_idx) and h and i < len(row) and row[i] is not None:
                extra[h] = str(row[i])
        contacts.append({
            "name": str(name_val).strip(),
            "phone": str(phone_val).strip(),
            "extra_data": extra or None,
        })
    return contacts


# ---------------------------------------------------------------------------
# Message templating
# ---------------------------------------------------------------------------

def _render_message(template: str, contact: models.Contact) -> str:
    msg = template
    msg = msg.replace("{{name}}", contact.name)
    msg = msg.replace("{{phone}}", contact.phone)
    if contact.extra_data:
        for k, v in contact.extra_data.items():
            msg = msg.replace(f"{{{{{k}}}}}", str(v))
    return msg


# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user(request, db)
    if user:
        return RedirectResponse("/dashboard")
    return templates.TemplateResponse("login.html", {"request": request, "current_user": None})


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "current_user": None, "error": "Usuario o contraseña incorrectos."},
            status_code=401,
        )
    token = create_session_token(user.id)
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ctx = _base_ctx(request, db, active_page="change_password")
    return templates.TemplateResponse("change_password.html", ctx)


@app.post("/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)

    if not verify_password(current_password, user.password_hash):
        return _redirect_with_flash(
            "/change-password", "flash_error", "La contraseña actual es incorrecta."
        )

    if new_password != confirm_password:
        return _redirect_with_flash(
            "/change-password", "flash_error", "Las contraseñas nuevas no coinciden."
        )

    if len(new_password) < 6:
        return _redirect_with_flash(
            "/change-password", "flash_error", "La contraseña debe tener al menos 6 caracteres."
        )

    user.password_hash = hash_password(new_password)
    db.commit()

    return _redirect_with_flash(
        "/dashboard", "flash_success", "Contraseña actualizada correctamente 🔑"
    )




@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ctx = _base_ctx(request, db, active_page="dashboard")

    # Stats
    total_lists = db.query(models.ContactList).filter_by(user_id=user.id).count()
    total_contacts = (
        db.query(models.Contact)
        .join(models.ContactList)
        .filter(models.ContactList.user_id == user.id)
        .count()
    )
    total_campaigns = db.query(models.Campaign).filter_by(user_id=user.id).count()
    messages_sent = (
        db.query(models.MessageLog)
        .join(models.Campaign)
        .filter(models.Campaign.user_id == user.id, models.MessageLog.status == "sent")
        .count()
    )

    recent_campaigns = (
        db.query(models.Campaign)
        .filter_by(user_id=user.id)
        .order_by(models.Campaign.created_at.desc())
        .limit(5)
        .all()
    )

    # WhatsApp status
    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    wa_status = None
    if settings:
        client = _get_waha_client(settings)
        try:
            wa_status = _normalize_wa_status(await client.get_session_status())
        except Exception:
            wa_status = None

    ctx.update(
        stats={
            "total_lists": total_lists,
            "total_contacts": total_contacts,
            "total_campaigns": total_campaigns,
            "messages_sent": messages_sent,
        },
        recent_campaigns=recent_campaigns,
        wa_status=wa_status,
    )
    return templates.TemplateResponse("dashboard.html", ctx)


# ---------------------------------------------------------------------------
# Routes: Contacts
# ---------------------------------------------------------------------------

@app.get("/contacts", response_class=HTMLResponse)
async def contacts_list(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ctx = _base_ctx(request, db, active_page="contacts")
    contact_lists = (
        db.query(models.ContactList)
        .filter_by(user_id=user.id)
        .order_by(models.ContactList.created_at.desc())
        .all()
    )
    ctx["contact_lists"] = contact_lists
    return templates.TemplateResponse("contacts.html", ctx)


@app.post("/contacts")
async def create_contact_list(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    cl = models.ContactList(name=name, description=description or None, user_id=user.id)
    db.add(cl)
    db.commit()
    return _redirect_with_flash("/contacts", "flash_success", f"Lista '{name}' creada exitosamente 🎉")


@app.get("/contacts/{list_id}", response_class=HTMLResponse)
async def contact_list_detail(list_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    cl = db.query(models.ContactList).filter_by(id=list_id, user_id=user.id).first()
    if not cl:
        raise HTTPException(status_code=404, detail="Lista no encontrada")
    ctx = _base_ctx(request, db, active_page="contacts")
    contacts = cl.contacts

    # Gather extra column names
    extra_cols: set[str] = set()
    for c in contacts:
        if c.extra_data:
            extra_cols.update(c.extra_data.keys())

    ctx.update(contact_list=cl, contacts=contacts, extra_columns=sorted(extra_cols))
    return templates.TemplateResponse("contact_detail.html", ctx)


@app.post("/contacts/{list_id}/delete")
async def delete_contact_list(list_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    cl = db.query(models.ContactList).filter_by(id=list_id, user_id=user.id).first()
    if not cl:
        raise HTTPException(status_code=404)
    db.delete(cl)
    db.commit()
    return _redirect_with_flash("/contacts", "flash_success", "Lista eliminada correctamente 🗑️")


@app.post("/contacts/{list_id}/contacts")
async def add_contact(
    list_id: int,
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    cl = db.query(models.ContactList).filter_by(id=list_id, user_id=user.id).first()
    if not cl:
        raise HTTPException(status_code=404)
    contact = models.Contact(list_id=list_id, name=name, phone=phone)
    db.add(contact)
    db.commit()
    return _redirect_with_flash(f"/contacts/{list_id}", "flash_success", f"Contacto '{name}' agregado ✅")


@app.post("/contacts/{list_id}/contacts/{contact_id}/delete")
async def delete_contact(
    list_id: int,
    contact_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    cl = db.query(models.ContactList).filter_by(id=list_id, user_id=user.id).first()
    if not cl:
        raise HTTPException(status_code=404)
    contact = db.query(models.Contact).filter_by(id=contact_id, list_id=list_id).first()
    if not contact:
        raise HTTPException(status_code=404)
    db.delete(contact)
    db.commit()
    return RedirectResponse(f"/contacts/{list_id}", status_code=302)


@app.post("/contacts/{list_id}/import")
async def import_contacts(
    list_id: int,
    request: Request,
    file: UploadFile = File(...),
    replace: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    cl = db.query(models.ContactList).filter_by(id=list_id, user_id=user.id).first()
    if not cl:
        raise HTTPException(status_code=404)

    try:
        contents = await file.read()
        parsed = _parse_excel(contents)
    except ValueError as e:
        return _redirect_with_flash(f"/contacts/{list_id}", "flash_error", str(e))
    except Exception:
        return _redirect_with_flash(f"/contacts/{list_id}", "flash_error", "Error al procesar el archivo Excel.")

    if replace == "1":
        db.query(models.Contact).filter_by(list_id=list_id).delete()
        db.commit()

    for c in parsed:
        contact = models.Contact(
            list_id=list_id,
            name=c["name"],
            phone=c["phone"],
            extra_data=c["extra_data"],
        )
        db.add(contact)
    db.commit()

    return _redirect_with_flash(
        f"/contacts/{list_id}",
        "flash_success",
        f"✅ Se importaron {len(parsed)} contactos correctamente.",
    )


# ---------------------------------------------------------------------------
# Routes: Campaigns
# ---------------------------------------------------------------------------

@app.get("/campaigns", response_class=HTMLResponse)
async def campaigns_list(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ctx = _base_ctx(request, db, active_page="campaigns")
    campaigns = (
        db.query(models.Campaign)
        .filter_by(user_id=user.id)
        .order_by(models.Campaign.created_at.desc())
        .all()
    )
    ctx["campaigns"] = campaigns
    return templates.TemplateResponse("campaigns.html", ctx)


@app.get("/campaigns/new", response_class=HTMLResponse)
async def new_campaign_page(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ctx = _base_ctx(request, db, active_page="new_campaign")
    contact_lists = (
        db.query(models.ContactList)
        .filter_by(user_id=user.id)
        .order_by(models.ContactList.name)
        .all()
    )
    # Gather all available extra columns across all lists
    extra_cols: set[str] = set()
    for cl in contact_lists:
        for c in cl.contacts:
            if c.extra_data:
                extra_cols.update(c.extra_data.keys())

    ctx.update(contact_lists=contact_lists, available_columns=sorted(extra_cols),
               now_quito_str=_now_quito_input_str())
    return templates.TemplateResponse("campaign_new.html", ctx)


@app.post("/campaigns")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    message_template: str = Form(...),
    action: str = Form("save_draft"),
    scheduled_at: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    form_data = await request.form()
    list_ids = form_data.getlist("list_ids")

    if not list_ids:
        return _redirect_with_flash(
            "/campaigns/new", "flash_error", "Selecciona al menos una lista de contactos."
        )

    # Validate scheduled datetime before creating the campaign
    sched_utc = None
    if action == "schedule":
        sched_utc = _parse_quito_dt(scheduled_at)
        if not sched_utc:
            return _redirect_with_flash(
                "/campaigns/new", "flash_error", "Fecha de programación inválida."
            )
        if sched_utc <= _utcnow():
            return _redirect_with_flash(
                "/campaigns/new", "flash_error", "La fecha programada debe ser en el futuro."
            )

    # Count total contacts (unique by phone across lists)
    phones_seen: set[str] = set()
    total = 0
    for lid in list_ids:
        cl = db.query(models.ContactList).filter_by(id=int(lid), user_id=user.id).first()
        if cl:
            for c in cl.contacts:
                if c.phone not in phones_seen:
                    phones_seen.add(c.phone)
                    total += 1

    campaign = models.Campaign(
        name=name,
        message_template=message_template,
        status="scheduled" if action == "schedule" else "draft",
        user_id=user.id,
        total_contacts=total,
        scheduled_at=sched_utc,
    )
    db.add(campaign)
    db.flush()

    for lid in list_ids:
        cl_link = models.CampaignList(campaign_id=campaign.id, list_id=int(lid))
        db.add(cl_link)
    db.commit()

    if action == "send":
        return RedirectResponse(f"/campaigns/{campaign.id}/send", status_code=302)

    if action == "schedule":
        return _redirect_with_flash(
            f"/campaigns/{campaign.id}",
            "flash_success",
            f"Campaña programada para el {scheduled_at.replace('T', ' ')} (hora Quito) 📅",
        )

    return _redirect_with_flash(
        f"/campaigns/{campaign.id}",
        "flash_success",
        "Campaña guardada como borrador 📝",
    )


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, user_id=user.id).first()
    if not campaign:
        raise HTTPException(status_code=404)
    ctx = _base_ctx(request, db, active_page="campaigns")
    logs = (
        db.query(models.MessageLog)
        .filter_by(campaign_id=campaign_id)
        .order_by(models.MessageLog.id)
        .all()
    )
    ctx.update(campaign=campaign, logs=logs)
    return templates.TemplateResponse("campaign_detail.html", ctx)


@app.get("/campaigns/{campaign_id}/edit", response_class=HTMLResponse)
async def edit_campaign_page(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, user_id=user.id).first()
    if not campaign:
        raise HTTPException(status_code=404)
    if campaign.status not in ("draft", "scheduled"):
        return _redirect_with_flash(
            f"/campaigns/{campaign_id}",
            "flash_error",
            "Solo se pueden editar campañas en borrador o programadas.",
        )
    ctx = _base_ctx(request, db, active_page="campaigns")
    contact_lists = (
        db.query(models.ContactList)
        .filter_by(user_id=user.id)
        .order_by(models.ContactList.name)
        .all()
    )
    extra_cols: set[str] = set()
    for cl in contact_lists:
        for c in cl.contacts:
            if c.extra_data:
                extra_cols.update(c.extra_data.keys())

    selected_list_ids = {cl_link.list_id for cl_link in campaign.lists}
    # Pre-fill scheduled_at in Quito time for the datetime-local input
    scheduled_at_quito_str = ""
    if campaign.scheduled_at:
        scheduled_at_quito_str = (
            campaign.scheduled_at.replace(tzinfo=timezone.utc)
            .astimezone(QUITO_TZ)
            .strftime("%Y-%m-%dT%H:%M")
        )
    ctx.update(
        contact_lists=contact_lists,
        available_columns=sorted(extra_cols),
        campaign=campaign,
        selected_list_ids=selected_list_ids,
        editing=True,
        now_quito_str=_now_quito_input_str(),
        scheduled_at_quito_str=scheduled_at_quito_str,
    )
    return templates.TemplateResponse("campaign_new.html", ctx)


@app.post("/campaigns/{campaign_id}/edit")
async def update_campaign(
    campaign_id: int,
    request: Request,
    name: str = Form(...),
    message_template: str = Form(...),
    action: str = Form("save_draft"),
    scheduled_at: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, user_id=user.id).first()
    if not campaign:
        raise HTTPException(status_code=404)
    if campaign.status not in ("draft", "scheduled"):
        return _redirect_with_flash(
            f"/campaigns/{campaign_id}",
            "flash_error",
            "Solo se pueden editar campañas en borrador o programadas.",
        )

    form_data = await request.form()
    list_ids = form_data.getlist("list_ids")

    if not list_ids:
        return _redirect_with_flash(
            f"/campaigns/{campaign_id}/edit",
            "flash_error",
            "Selecciona al menos una lista de contactos.",
        )

    # Update campaign fields
    campaign.name = name
    campaign.message_template = message_template

    # Replace contact list associations
    for cl_link in list(campaign.lists):
        db.delete(cl_link)
    db.flush()

    phones_seen: set[str] = set()
    total = 0
    for lid in list_ids:
        cl = db.query(models.ContactList).filter_by(id=int(lid), user_id=user.id).first()
        if cl:
            db.add(models.CampaignList(campaign_id=campaign.id, list_id=int(lid)))
            for c in cl.contacts:
                if c.phone not in phones_seen:
                    phones_seen.add(c.phone)
                    total += 1

    campaign.total_contacts = total

    if action == "schedule":
        sched_utc = _parse_quito_dt(scheduled_at)
        if not sched_utc:
            db.rollback()
            return _redirect_with_flash(
                f"/campaigns/{campaign_id}/edit",
                "flash_error",
                "Fecha de programación inválida.",
            )
        if sched_utc <= _utcnow():
            db.rollback()
            return _redirect_with_flash(
                f"/campaigns/{campaign_id}/edit",
                "flash_error",
                "La fecha programada debe ser en el futuro.",
            )
        campaign.scheduled_at = sched_utc
        campaign.status = "scheduled"
        db.commit()
        return _redirect_with_flash(
            f"/campaigns/{campaign.id}",
            "flash_success",
            f"Campaña programada para el {scheduled_at.replace('T', ' ')} (hora Quito) 📅",
        )

    # save_draft or send
    campaign.scheduled_at = None
    campaign.status = "draft"
    db.commit()

    if action == "send":
        return RedirectResponse(f"/campaigns/{campaign.id}/send", status_code=302)

    return _redirect_with_flash(
        f"/campaigns/{campaign.id}",
        "flash_success",
        "Campaña actualizada ✏️",
    )


@app.post("/campaigns/{campaign_id}/delete")
async def delete_campaign(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, user_id=user.id).first()
    if not campaign:
        raise HTTPException(status_code=404)
    db.delete(campaign)
    db.commit()
    return _redirect_with_flash("/campaigns", "flash_success", "Campaña eliminada 🗑️")


@app.post("/campaigns/{campaign_id}/unschedule")
async def unschedule_campaign(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, user_id=user.id).first()
    if not campaign:
        raise HTTPException(status_code=404)
    if campaign.status != "scheduled":
        return _redirect_with_flash(
            f"/campaigns/{campaign_id}", "flash_error", "La campaña no está programada."
        )
    campaign.status = "draft"
    campaign.scheduled_at = None
    db.commit()
    return _redirect_with_flash(
        f"/campaigns/{campaign_id}",
        "flash_success",
        "Programación cancelada. La campaña volvió a borrador 📝",
    )

@app.post("/campaigns/{campaign_id}/send")
async def send_campaign(
    campaign_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, user_id=user.id).first()
    if not campaign:
        raise HTTPException(status_code=404)
    if campaign.status == "sending":
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)
    if campaign.status not in ("draft", "scheduled"):
        return _redirect_with_flash(
            f"/campaigns/{campaign_id}",
            "flash_error",
            "Solo se pueden enviar campañas en borrador o programadas.",
        )

    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    if not settings:
        return _redirect_with_flash(
            f"/campaigns/{campaign_id}",
            "flash_error",
            "Configura WAHA antes de enviar mensajes.",
        )

    client = _get_waha_client(settings)
    campaign.status = "sending"
    campaign.sent_at = _utcnow()
    db.commit()

    # Collect unique contacts (by phone) across all campaign lists
    seen_phones: set[str] = set()
    contacts_to_send: list[models.Contact] = []
    for cl_link in campaign.lists:
        for contact in cl_link.contact_list.contacts:
            if contact.phone not in seen_phones:
                seen_phones.add(contact.phone)
                contacts_to_send.append(contact)

    campaign.total_contacts = len(contacts_to_send)
    db.commit()

    # Create pending log entries
    log_ids = []
    for contact in contacts_to_send:
        log = models.MessageLog(
            campaign_id=campaign.id,
            contact_id=contact.id,
            phone=contact.phone,
            contact_name=contact.name,
            message=_render_message(campaign.message_template, contact),
            status="pending",
        )
        db.add(log)
        db.flush()
        log_ids.append(log.id)
    db.commit()

    # Send messages in background using FastAPI's BackgroundTasks
    background_tasks.add_task(_send_campaign_messages, campaign.id, log_ids, client)

    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=302)


async def _send_campaign_messages(campaign_id: int, log_ids: list[int], client: WAHAClient):
    """Background task to send campaign messages one by one.

    When HUMAN_SIMULATE is enabled (default), each message is sent via
    ``WAHAClient.send_text_humanized`` which mimics real human behaviour:
    read receipt → thinking pause → typing indicator → send.  Inter-message
    delays are randomised within [MESSAGE_MIN_DELAY, MESSAGE_MAX_DELAY].

    When HUMAN_SIMULATE is disabled the legacy fixed MESSAGE_SEND_DELAY is
    used with a plain ``send_text`` call.
    """
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter_by(id=campaign_id).first()
        if not campaign:
            return

        sent = 0
        failed = 0
        for log_id in log_ids:
            log = db.query(models.MessageLog).filter_by(id=log_id).first()
            if not log:
                continue
            try:
                if HUMAN_SIMULATE:
                    # send_text_humanized handles all delays internally
                    # (pre-message pause, seen, typing, post-typing pause).
                    await client.send_text_humanized(
                        log.phone,
                        log.message,
                        min_delay=MESSAGE_MIN_DELAY,
                        max_delay=MESSAGE_MAX_DELAY,
                        typing_cpm=TYPING_CPM,
                        seen_probability=SEEN_PROBABILITY,
                    )
                else:
                    await client.send_text(log.phone, log.message)
                log.status = "sent"
                log.sent_at = _utcnow()
                sent += 1
            except Exception as e:
                log.status = "failed"
                log.error_message = str(e)[:500]
                failed += 1
            db.commit()
            if not HUMAN_SIMULATE:
                # Human-simulate mode already includes inter-message delay;
                # in legacy mode apply the fixed delay here.
                await asyncio.sleep(MESSAGE_SEND_DELAY)

        campaign.sent_count = sent
        campaign.failed_count = failed
        campaign.status = "completed" if failed == 0 else ("failed" if sent == 0 else "completed")
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Routes: Settings
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    ctx = _base_ctx(request, db, active_page="settings")
    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    ctx["settings"] = settings

    wa_status = None
    wa_error = None
    qr_image = None

    if settings:
        client = _get_waha_client(settings)
        try:
            wa_status = _normalize_wa_status(await client.get_session_status())
            if wa_status and wa_status.get("status") == "SCAN_QR_CODE":
                qr_data = await client.get_qr()
                if qr_data:
                    qr_image = qr_data["data_url"]
        except Exception as e:
            wa_error = str(e)

    ctx.update(wa_status=wa_status, wa_error=wa_error, qr_image=qr_image)
    return templates.TemplateResponse("settings.html", ctx)


@app.post("/settings/waha")
async def save_waha_settings(
    request: Request,
    base_url: str = Form(...),
    api_key: str = Form(""),
    session_name: str = Form("default"),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    if settings:
        settings.base_url = base_url.rstrip("/")
        # Keep the stored key when the field is left blank (security: key is never echoed back)
        if api_key:
            settings.api_key = api_key
        settings.session_name = session_name or "default"
    else:
        settings = models.WAHASettings(
            user_id=user.id,
            base_url=base_url.rstrip("/"),
            api_key=api_key or None,
            session_name=session_name or "default",
        )
        db.add(settings)
    db.commit()
    return _redirect_with_flash("/settings", "flash_success", "Configuración guardada ✅")


@app.post("/settings/session/start")
async def start_session(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    if not settings:
        return _redirect_with_flash("/settings", "flash_error", "Primero guarda la configuración de WAHA.")
    client = _get_waha_client(settings)
    try:
        await client.start_session()
        return _redirect_with_flash("/settings", "flash_success", "Sesión iniciada ▶️")
    except Exception as e:
        return _redirect_with_flash("/settings", "flash_error", f"Error al iniciar sesión: {e}")


@app.post("/settings/session/stop")
async def stop_session(request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    if not settings:
        return _redirect_with_flash("/settings", "flash_error", "Primero guarda la configuración de WAHA.")
    client = _get_waha_client(settings)
    try:
        await client.stop_session()
        return _redirect_with_flash("/settings", "flash_success", "Sesión detenida ⏹️")
    except Exception as e:
        return _redirect_with_flash("/settings", "flash_error", f"Error al detener sesión: {e}")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
