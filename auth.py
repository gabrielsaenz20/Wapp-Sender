import bcrypt as _bcrypt
from itsdangerous import URLSafeTimedSerializer
from fastapi import Request, HTTPException
import os
import logging

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = "insecure-default-key-please-set-SECRET_KEY-env-var"
    logger.warning(
        "SECRET_KEY environment variable is not set! "
        "Using an insecure default. Set SECRET_KEY to a random 32+ character string."
    )

serializer = URLSafeTimedSerializer(SECRET_KEY)

SESSION_COOKIE = "wapp_session"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours


def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode(), hashed.encode())


def create_session_token(user_id: int) -> str:
    return serializer.dumps({"user_id": user_id})


def decode_session_token(token: str) -> dict | None:
    try:
        return serializer.loads(token, max_age=SESSION_MAX_AGE)
    except Exception:
        return None


def get_current_user_id(request: Request) -> int | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = decode_session_token(token)
    if not data:
        return None
    return data.get("user_id")


def require_auth(request: Request) -> int:
    user_id = get_current_user_id(request)
    if not user_id:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user_id
