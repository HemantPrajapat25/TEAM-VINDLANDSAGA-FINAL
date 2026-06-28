import os
import uuid
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt

logger = logging.getLogger(__name__)
router = APIRouter()

SECRET_KEY = os.getenv("SECRET_KEY", "dev_secret_key_change_me_in_prod")
ALGORITHM = "HS256"


class LoginRequest(BaseModel):
    email: str
    password: str
    role: str


class Token(BaseModel):
    access_token: str
    token_type: str


class UserInfo(BaseModel):
    id: str
    email: str
    role: str
    name: str


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


@router.post("/login", response_model=Token)
async def login(req: LoginRequest):
    """
    Demo login endpoint.
    In a real app, this would verify against Supabase Auth.
    For this hackathon, we allow any login and issue a JWT.
    """
    logger.info(f"Login attempt for {req.email} as {req.role}")
    
    # Mock user creation
    user_id = str(uuid.uuid4())
    access_token_expires = timedelta(hours=2)
    access_token = create_access_token(
        data={"sub": req.email, "role": req.role, "id": user_id},
        expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=UserInfo)
async def get_me(token: str):
    """Decode JWT and return user info."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        role: str = payload.get("role")
        user_id: str = payload.get("id")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return UserInfo(
            id=user_id,
            email=email,
            role=role,
            name=email.split("@")[0].capitalize()
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
