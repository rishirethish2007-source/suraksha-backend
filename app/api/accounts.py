"""Password sign-in with short-lived access JWTs and non-expiring revocable refresh secrets.
Email is an account identifier, not a verified address. Deployment must use HTTPS.
"""
import asyncio
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ConfigDict, field_validator
from jose import jwt
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.db.database import get_db
from app.models.accounts import Account, AccountSession

router = APIRouter(prefix='/api/v1/accounts', tags=['Accounts'])
_attempts = {}
_password_workers = asyncio.Semaphore(2)

async def password_work(function, *args):
    async with _password_workers:
        return await run_in_threadpool(function, *args)

class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(min_length=3,max_length=254,pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    password: str = Field(min_length=12,max_length=128)
    @field_validator('email', mode='before')
    @classmethod
    def normalize(cls, value): return value.strip().lower() if isinstance(value,str) else value

class Registration(Login):
    name: str = Field(min_length=1,max_length=200)
    phone: str = Field(min_length=3,max_length=32,pattern=r'^\+?[0-9 ()-]+$')
    @field_validator('name')
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip(): raise ValueError('Name must not be blank')
        return value.strip()

class Refresh(BaseModel):
    refresh_token: str = Field(min_length=32,max_length=200)

def signing_ready():
    if settings.JWT_PUBLIC_KEY or settings.JWT_ALGORITHM != 'HS256' or len(settings.JWT_SECRET_KEY)<32:
        raise HTTPException(503,'Local account signing is unavailable. Configure local authentication or use your platform sign-in provider.')

async def throttle(request: Request):
    key = hashlib.sha256((request.client.host if request.client else 'unknown').encode()).hexdigest()
    if settings.REDIS_ENABLED:
        from app.realtime import redis_client
        try:
            count = await redis_client.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",1,'accounts:limit:'+key)
        except Exception: raise HTTPException(503,'Sign-in temporarily unavailable') from None
    else:
        now=time.monotonic()
        for old in list(_attempts):
            if now-_attempts[old][0]>=60: del _attempts[old]
        if key not in _attempts:
            if len(_attempts)>=10000: raise HTTPException(503,'Sign-in temporarily unavailable')
            _attempts[key]=[now,0]
        _attempts[key][1]+=1
        count=_attempts[key][1]
    if count>20: raise HTTPException(429,'Too many sign-in attempts. Wait one minute.',headers={'Retry-After':'60'})

def password_hash(password, salt=None):
    salt=salt or secrets.token_hex(16)
    digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=131072,r=8,p=1,dklen=32,maxmem=256*1024*1024).hex()
    return salt+':'+digest

def verify_password(password, stored):
    return secrets.compare_digest(password_hash(password,stored.split(':')[0]),stored)

DUMMY_HASH=password_hash('unavailable-account-password')

def token_pair(account, secret):
    now=datetime.now(timezone.utc)
    access=jwt.encode({'sub':account.id,'name':account.name,'phone_number':account.phone,'role':account.role,
        'iss':settings.JWT_ISSUER,'aud':settings.JWT_AUDIENCE,'iat':now,'exp':now+timedelta(minutes=15)},settings.JWT_SECRET_KEY,algorithm='HS256')
    return {'access_token':access,'refresh_token':secret,'expires_in':900,'token_type':'bearer'}

async def new_session(db,account):
    secret=secrets.token_urlsafe(48)
    db.add(AccountSession(token_hash=hashlib.sha256(secret.encode()).hexdigest(),account_id=account.id))
    await db.commit()
    return token_pair(account,secret)

@router.post('/register',status_code=201,dependencies=[Depends(throttle)])
async def register(body: Registration,response: Response,db: AsyncSession=Depends(get_db)):
    signing_ready();response.headers['Cache-Control']='no-store'
    account=Account(id=str(uuid.uuid4()),email=body.email,name=body.name,phone=body.phone,
                    password_hash=await password_work(password_hash,body.password),role='user',enabled=True)
    db.add(account)
    try:
        await db.flush()
        return await new_session(db,account)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409,'An account already exists for this email. Sign in instead.') from None

@router.post('/login',dependencies=[Depends(throttle)])
async def login(body: Login,response: Response,db: AsyncSession=Depends(get_db)):
    signing_ready();response.headers['Cache-Control']='no-store'
    account=(await db.execute(select(Account).where(Account.email==body.email))).scalar_one_or_none()
    valid=await password_work(verify_password,body.password,account.password_hash if account else DUMMY_HASH)
    if not valid or not account or not account.enabled: raise HTTPException(401,'Invalid email or password')
    return await new_session(db,account)

@router.post('/refresh',dependencies=[Depends(throttle)])
async def refresh(body: Refresh,response: Response,db: AsyncSession=Depends(get_db)):
    signing_ready();response.headers['Cache-Control']='no-store'
    session=await db.get(AccountSession,hashlib.sha256(body.refresh_token.encode()).hexdigest())
    account=await db.get(Account,session.account_id) if session else None
    if not account or not account.enabled: raise HTTPException(401,'Session revoked. Sign in again.')
    return token_pair(account,body.refresh_token)

@router.post('/logout')
async def logout(body: Refresh,db: AsyncSession=Depends(get_db)):
    await db.execute(delete(AccountSession).where(AccountSession.token_hash==hashlib.sha256(body.refresh_token.encode()).hexdigest()))
    await db.commit()
    return {'success':True}
