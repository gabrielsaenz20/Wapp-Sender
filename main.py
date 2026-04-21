import asyncio
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import quote, unquote

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

# Delay in seconds between messages to respect WAHA / WhatsApp rate limits.
# Increase if messages fail due to rate limiting. WAHA recommends at least 1s.
MESSAGE_SEND_DELAY = float(os.getenv("MESSAGE_SEND_DELAY", "1.0"))

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Wapp Sender")

# Serve static files if the directory exists
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

# Create all tables on startup
Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


@app.on_event("startup")
async def on_startup():
    """Called by uvicorn on startup – ensure admin user exists."""
    db = SessionLocal()
    try:
        _ensure_admin(db)
    finally:
        db.close()


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


# ---------------------------------------------------------------------------
# Routes: Dashboard
# ---------------------------------------------------------------------------

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
            wa_status = await client.get_session_status()
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

    ctx.update(contact_lists=contact_lists, available_columns=sorted(extra_cols))
    return templates.TemplateResponse("campaign_new.html", ctx)


@app.post("/campaigns")
async def create_campaign(
    request: Request,
    name: str = Form(...),
    message_template: str = Form(...),
    action: str = Form("save_draft"),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    form_data = await request.form()
    list_ids = form_data.getlist("list_ids")

    if not list_ids:
        return _redirect_with_flash(
            "/campaigns/new", "flash_error", "Selecciona al menos una lista de contactos."
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
        status="draft",
        user_id=user.id,
        total_contacts=total,
    )
    db.add(campaign)
    db.flush()

    for lid in list_ids:
        cl_link = models.CampaignList(campaign_id=campaign.id, list_id=int(lid))
        db.add(cl_link)
    db.commit()

    if action == "send":
        return RedirectResponse(f"/campaigns/{campaign.id}/send", status_code=302)

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


@app.post("/campaigns/{campaign_id}/delete")
async def delete_campaign(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    campaign = db.query(models.Campaign).filter_by(id=campaign_id, user_id=user.id).first()
    if not campaign:
        raise HTTPException(status_code=404)
    db.delete(campaign)
    db.commit()
    return _redirect_with_flash("/campaigns", "flash_success", "Campaña eliminada 🗑️")


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
    """Background task to send campaign messages one by one."""
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
                await client.send_text(log.phone, log.message)
                log.status = "sent"
                log.sent_at = _utcnow()
                sent += 1
            except Exception as e:
                log.status = "failed"
                log.error_message = str(e)[:500]
                failed += 1
            db.commit()
            # Rate limiting: WAHA recommends >= 1s between messages to avoid bans.
            # Configurable via MESSAGE_SEND_DELAY environment variable.
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
    all_sessions: list = []

    if settings:
        client = _get_waha_client(settings)
        try:
            wa_status = await client.get_session_status()
            # Normalize 'me' field: some WAHA versions return it as a plain string
            # (e.g. the phone number) instead of a dict with 'pushName'/'id' keys.
            # Converting it to a dict ensures the template can call .get() safely.
            me = wa_status.get("me")
            if isinstance(me, str):
                wa_status["me"] = {"pushName": me, "id": {}}
            elif isinstance(me, dict):
                # Some WAHA versions return 'id' as a plain string like
                # "5219981234567@c.us" rather than {"user": "5219981234567"}.
                raw_id = me.get("id")
                if isinstance(raw_id, str):
                    me["id"] = {"user": raw_id.split("@")[0]}
            if wa_status.get("status") == "SCAN_QR_CODE":
                qr_data = await client.get_qr()
                if qr_data:
                    qr_image = qr_data["data_url"]
        except Exception as e:
            wa_error = str(e)
        try:
            all_sessions = await client.list_sessions()
            if not isinstance(all_sessions, list):
                all_sessions = []
        except Exception:
            all_sessions = []

    ctx.update(wa_status=wa_status, wa_error=wa_error, qr_image=qr_image, all_sessions=all_sessions)
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
        settings.api_key = api_key or None
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


@app.post("/settings/session/create")
async def create_session(
    request: Request,
    session_name: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    if not settings:
        return _redirect_with_flash("/settings", "flash_error", "Primero guarda la configuración de WAHA.")
    name = session_name.strip() or "default"
    client = _get_waha_client(settings)
    try:
        await client.create_session(name)
        return _redirect_with_flash("/settings", "flash_success", f"Sesión '{name}' creada ✅")
    except Exception as e:
        return _redirect_with_flash("/settings", "flash_error", f"Error al crear sesión: {e}")


@app.post("/settings/session/stop-by-name")
async def stop_session_by_name(
    request: Request,
    session_name: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    settings = db.query(models.WAHASettings).filter_by(user_id=user.id).first()
    if not settings:
        return _redirect_with_flash("/settings", "flash_error", "Primero guarda la configuración de WAHA.")
    client = _get_waha_client(settings)
    try:
        await client.stop_session_by_name(session_name)
        return _redirect_with_flash("/settings", "flash_success", f"Sesión '{session_name}' detenida ⏹️")
    except Exception as e:
        return _redirect_with_flash("/settings", "flash_error", f"Error al detener sesión: {e}")


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
