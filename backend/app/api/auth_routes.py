from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.auth import create_access_token, hash_password, verify_password
from app.models import User, UserProfile

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


def _user_response(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.post("/register", status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if len(body.password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")
    user = User(
        email=body.email.lower().strip(),
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered")
    # Auto-create empty profile
    profile = UserProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(user)
    return _user_response(user)


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalars(select(User).where(User.email == body.email.lower().strip())).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    token = create_access_token(user_id=user.id, role=user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_response(user)}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    return _user_response(current_user)


@router.put("/me")
def update_me(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if "display_name" in body:
        current_user.display_name = body["display_name"]
    db.commit()
    db.refresh(current_user)
    return _user_response(current_user)
