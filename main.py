"""Helix's Studio: adult-only fan membership portal.

Payments are MANUAL verification requests, not automatically charged or redeemed.
Creator media, legal age verification and production delivery must be configured separately.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import secrets
import smtplib
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from calendar import monthrange
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from starlette.datastructures import UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent
ENV = os.getenv('ENVIRONMENT', 'development').lower()
SECRET_KEY = os.getenv('SECRET_KEY', '')
if ENV == 'production' and len(SECRET_KEY) < 32:
    raise RuntimeError('Set a securely generated SECRET_KEY (32+ characters) in production.')
SECRET_KEY = SECRET_KEY or secrets.token_urlsafe(48)
DB_PATH = Path(os.getenv('DATABASE_PATH', str(ROOT / 'data' / 'helix_studio.db')))
UPLOAD_DIR = Path(os.getenv('PRIVATE_UPLOAD_DIR', str(DB_PATH.parent / 'helix_proofs')))
BTC_ADDRESS = os.getenv('BTC_ADDRESS', '').strip()
GIFT_CARD_INSTRUCTIONS = os.getenv('GIFT_CARD_INSTRUCTIONS', '').strip()
GIFT_CARD_ENABLED = os.getenv('GIFT_CARD_ENABLED', '0') == '1' and bool(GIFT_CARD_INSTRUCTIONS)
MEMBERSHIP_MONTHS = int(os.getenv('MEMBERSHIP_MONTHS', '12'))
if MEMBERSHIP_MONTHS < 1 or MEMBERSHIP_MONTHS > 36:
    raise RuntimeError('MEMBERSHIP_MONTHS must be 1 to 36')
SMTP_HOST = os.getenv('SMTP_HOST', '').strip()
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '').strip()
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '').strip()
SMTP_FROM = os.getenv('SMTP_FROM', SMTP_USER).strip()
SUPPORT_EMAIL = os.getenv('SUPPORT_EMAIL', SMTP_FROM or '').strip()
EMAIL_MODE = os.getenv('EMAIL_MODE', 'smtp').lower()
EMAIL_READY = (EMAIL_MODE == 'console' and ENV != 'production') or all((SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM))
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')

TIERS = [
    {'slug':'the-fan','name':'The Fan','price':200,'tag':'Essential','subtitle':'Your private journey begins.', 'benefits':['Age-gated fan community access','Selected mature photo collections','Creator news and exclusive announcements','Digital welcome membership card']},
    {'slug':'the-insider','name':'The Insider','price':300,'tag':'Behind the scenes','subtitle':'Beyond the public eye.', 'benefits':['Everything in The Fan','Extended behind-the-scenes galleries','Members-only creator interviews','Early previews and digital photo drops']},
    {'slug':'all-access','name':'All Access','price':500,'tag':'Popular','subtitle':'More to discover.', 'benefits':['All lower-level benefits','Early selected video and photo releases','Welcome merchandise when available','Giveaway eligibility under published rules']},
    {'slug':'vip-circle','name':'VIP Circle','price':1000,'tag':'VIP','subtitle':'A closer connection.', 'benefits':['All lower-level benefits','Group creator Q&A invitations when offered','Consideration for limited fan meet-ups','Members-only digital events and VIP merch']},
    {'slug':'backstage-society','name':'Backstage Society','price':3000,'tag':'Elite','subtitle':'Behind the velvet curtain.', 'benefits':['All lower-level benefits','Exclusive small-group virtual events if scheduled','Premium collectible packs when available','Early creative previews and recognition']},
    {'slug':'the-icon-club','name':'The Icon Club','price':5000,'tag':'Platinum','subtitle':'A distinguished fan experience.', 'benefits':['All lower-level benefits','Limited-edition collector editions','Signed merchandise when available','Priority consideration for eligible VIP events']},
    {'slug':'founders-circle','name':"Founder's Circle",'price':10000,'tag':'Signature','subtitle':'The most exclusive circle.', 'benefits':['All lower-level benefits','Signature collector package when available','Optional patron recognition','Priority consideration for select events and previews']},
]
TIER_BY_SLUG = {t['slug']: dict(t, rank=i+1) for i,t in enumerate(TIERS)}
for i,t in enumerate(TIERS):
    t['rank'] = i+1

app = FastAPI(title="Helix's Studio")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie='helix_session', max_age=60*60*24*7, same_site='lax', https_only=ENV=='production')
app.mount('/static', StaticFiles(directory=str(ROOT / 'static')), name='static')

@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.url.path.startswith(('/dashboard','/admin','/verify','/checkout')):
        response.headers['Cache-Control'] = 'private, no-store'
    return response
templates = Jinja2Templates(directory=str(ROOT / 'templates'))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).isoformat(timespec='seconds')


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys = ON')
    c.execute('PRAGMA busy_timeout = 10000')
    return c


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return 'pbkdf2_sha256$600000$' + salt.hex() + '$' + hashed.hex()


def verify_password(stored: str, password: str) -> bool:
    try:
        scheme, rounds, salt, hashed = stored.split('$')
        if scheme != 'pbkdf2_sha256':
            return False
        candidate = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(candidate, bytes.fromhex(hashed))
    except (ValueError, TypeError):
        return False


@contextmanager
def db_session():
    c = connect()
    try:
        with c:
            yield c
    finally:
        c.close()


def add_months(dt: datetime, months: int) -> datetime:
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, monthrange(year, month)[1]))


def initialize_database() -> None:
    c = connect()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
          is_verified INTEGER NOT NULL DEFAULT 0, is_admin INTEGER NOT NULL DEFAULT 0,
          age_attested INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
          joined_at TEXT NOT NULL, tier_slug TEXT, member_until TEXT
        );
        CREATE TABLE IF NOT EXISTS email_codes (
          user_id INTEGER NOT NULL, purpose TEXT NOT NULL, code_hash TEXT NOT NULL,
          expires_at TEXT NOT NULL, sent_at TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, purpose),
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS payment_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          tier_slug TEXT NOT NULL, amount INTEGER NOT NULL, method TEXT NOT NULL,
          transaction_reference TEXT NOT NULL, gift_card_brand TEXT,
          proof_filename TEXT, status TEXT NOT NULL DEFAULT 'pending',
          submitted_at TEXT NOT NULL, reviewed_at TEXT, reviewer_id INTEGER, review_note TEXT,
          FOREIGN KEY(user_id) REFERENCES users(id),
          FOREIGN KEY(reviewer_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_payment_status ON payment_requests(status, submitted_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_btc_transaction_once ON payment_requests(transaction_reference)
          WHERE method='bitcoin' AND status IN ('pending','approved');
        CREATE TABLE IF NOT EXISTS audit_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER NOT NULL,
          action TEXT NOT NULL, subject_id INTEGER NOT NULL, occurred_at TEXT NOT NULL,
          FOREIGN KEY(actor_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS support_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          message TEXT NOT NULL, reply TEXT, created_at TEXT NOT NULL, replied_at TEXT,
          FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS password_resets (
          user_id INTEGER PRIMARY KEY, password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS login_throttle (
          key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, started_at TEXT NOT NULL
        );
    ''')
    if ADMIN_EMAIL or ADMIN_PASSWORD:
        if not ADMIN_EMAIL or len(ADMIN_PASSWORD) < 12:
            c.close()
            raise RuntimeError('Set ADMIN_EMAIL and a unique ADMIN_PASSWORD (12+ characters) together.')
        u = c.execute('SELECT id FROM users WHERE email = ? COLLATE NOCASE', (ADMIN_EMAIL,)).fetchone()
        if u:
            c.execute("UPDATE users SET is_admin=1, is_verified=1, status='active', password_hash=? WHERE id=?",
                      (hash_password(ADMIN_PASSWORD),u['id']))
        else:
            c.execute('INSERT INTO users(full_name,email,password_hash,is_verified,is_admin,age_attested,joined_at) VALUES(?,?,?,?,?,?,?)',
                      ('Studio Administrator', ADMIN_EMAIL, hash_password(ADMIN_PASSWORD),1,1,1,iso()))
    c.commit()
    c.close()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

initialize_database()


def csrf_token(request: Request) -> str:
    if '_csrf' not in request.session:
        request.session['_csrf'] = secrets.token_urlsafe(32)
    return request.session['_csrf']


def valid_csrf(request: Request, form) -> bool:
    token = form.get('csrf', '')
    return isinstance(token, str) and hmac.compare_digest(token, request.session.get('_csrf', 'invalid'))


def flash(request: Request, message: str, category: str = 'info') -> None:
    notices = request.session.get('flash', [])
    request.session['flash'] = (notices + [{'text':message,'category':category}])[-4:]


def user_for(request: Request):
    uid = request.session.get('uid')
    if not isinstance(uid, int):
        return None
    with db_session() as c:
        u = c.execute('SELECT * FROM users WHERE id=? AND status=?',(uid,'active')).fetchone()
    return dict(u) if u and u['is_verified'] else None


def is_adult(request: Request) -> bool:
    return bool(request.session.get('age_confirmed'))


def render(request: Request, name: str, **context):
    user = user_for(request)
    shared = dict(user=user, plans=TIERS, csrf=csrf_token(request),
                  notices=request.session.pop('flash', []), adult=is_adult(request),
                  support_email=SUPPORT_EMAIL, membership_months=MEMBERSHIP_MONTHS,
                  email_ready=EMAIL_READY, btc_available=bool(BTC_ADDRESS),
                  gift_available=GIFT_CARD_ENABLED)
    shared.update(context)
    return templates.TemplateResponse(request, name, shared)


def go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def protected(request: Request, admin: bool=False):
    if not is_adult(request):
        return go('/age')
    u = user_for(request)
    if not u:
        return go('/login')
    if admin and not u['is_admin']:
        return go('/dashboard')
    return None


def valid_email(email: str) -> bool:
    return len(email) <= 254 and bool(re.fullmatch(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}',email))


def new_code(c, user_id: int, purpose: str) -> str:
    code = f'{secrets.randbelow(1_000_000):06d}'
    digest = hmac.new(SECRET_KEY.encode(), f'{user_id}:{purpose}:{code}'.encode(), hashlib.sha256).hexdigest()
    now = utcnow()
    c.execute('''INSERT INTO email_codes(user_id,purpose,code_hash,expires_at,sent_at,attempts)
                 VALUES(?,?,?,?,?,0) ON CONFLICT(user_id,purpose) DO UPDATE SET
                 code_hash=excluded.code_hash, expires_at=excluded.expires_at,
                 sent_at=excluded.sent_at, attempts=0''',
              (user_id,purpose,digest,iso(now+timedelta(minutes=10)),iso(now)))
    return code


def check_code(c, user_id: int, purpose: str, code: str) -> bool:
    item = c.execute('SELECT * FROM email_codes WHERE user_id=? AND purpose=?',(user_id,purpose)).fetchone()
    if not item or item['attempts'] >= 5 or iso() > item['expires_at']:
        return False
    c.execute('UPDATE email_codes SET attempts=attempts+1 WHERE user_id=? AND purpose=?',(user_id,purpose))
    digest = hmac.new(SECRET_KEY.encode(), f'{user_id}:{purpose}:{code}'.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest,item['code_hash']):
        return False
    c.execute('DELETE FROM email_codes WHERE user_id=? AND purpose=?',(user_id,purpose))
    return True


def send_email(destination: str, subject: str, body: str) -> None:
    if EMAIL_MODE == 'console' and ENV != 'production':
        print(f'[DEV EMAIL ONLY] TO: {destination} SUBJECT: {subject} BODY: {body}', flush=True)
        return
    if not EMAIL_READY:
        raise RuntimeError('SMTP settings not configured.')
    email = EmailMessage()
    email['Subject'] = subject
    email['From'] = SMTP_FROM
    email['To'] = destination
    email.set_content(body)
    if SMTP_PORT == 465:
        import ssl
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=15) as smtp:
            smtp.login(SMTP_USER,SMTP_PASSWORD)
            smtp.send_message(email)
    else:
        import ssl
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(SMTP_USER,SMTP_PASSWORD)
            smtp.send_message(email)


def rate_limited(c, email: str, ip: str, increment: bool = False) -> bool:
    key = hashlib.sha256((email + '|' + ip).encode()).hexdigest()
    row = c.execute('SELECT attempts,started_at FROM login_throttle WHERE key=?',(key,)).fetchone()
    if row and utcnow() - datetime.fromisoformat(row['started_at']) < timedelta(minutes=15):
        if row['attempts'] >= 8:
            return True
        if increment:
            c.execute('UPDATE login_throttle SET attempts=attempts+1 WHERE key=?',(key,))
    elif increment:
        c.execute('INSERT OR REPLACE INTO login_throttle(key,attempts,started_at) VALUES(?,?,?)',(key,1,iso()))
    return False


def member_rank(user) -> int:
    if not user or not user.get('tier_slug'):
        return 0
    if user.get('member_until') and user['member_until'] < iso():
        return 0
    return TIER_BY_SLUG.get(user['tier_slug'],{}).get('rank',0)


@app.get('/health')
def health():
    return {'status':'ok'}


@app.get('/age')
def age(request: Request):
    return render(request,'age.html')


@app.post('/age')
async def age_submit(request: Request):
    form = await request.form()
    if not valid_csrf(request,form):
        return Response('Invalid security token.',status_code=403)
    if form.get('confirm') != 'adult':
        return render(request,'age.html',error='This website is exclusively for adults aged 18 or older.')
    request.session['age_confirmed'] = True
    return go('/')


@app.get('/')
def home(request: Request):
    if not is_adult(request):
        return go('/age')
    return render(request, 'home.html')


@app.get('/register')
def register_page(request: Request):
    if not is_adult(request): return go('/age')
    return render(request,'register.html')


@app.post('/register')
async def register(request: Request):
    if not is_adult(request): return go('/age')
    form = await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    name = str(form.get('full_name','')).strip()
    email = str(form.get('email','')).strip().lower()
    password = str(form.get('password',''))
    if not EMAIL_READY:
        return render(request,'register.html',error='Registration is paused until secure email delivery is configured.')
    if not (2 <= len(name) <= 80 and valid_email(email) and len(password)>=12 and len(password)<=128 and form.get('age_confirm')=='on'):
        return render(request,'register.html',error='Enter your name and valid email, use a 12+ character password, and confirm that you are 18+.')
    c=connect()
    try:
        existing=c.execute('SELECT id FROM users WHERE email=? COLLATE NOCASE',(email,)).fetchone()
        if existing:
            return render(request,'register.html',error='That email already has an account. Please sign in or recover your password.')
        cur=c.execute('INSERT INTO users(full_name,email,password_hash,age_attested,joined_at) VALUES(?,?,?,?,?)',(name,email,hash_password(password),1,iso()))
        user_id=cur.lastrowid
        code=new_code(c,user_id,'signup')
        c.commit()
        try:
            send_email(email,"Your Helix's Studio verification code",f'Your verification code is {code}. It expires in 10 minutes. Do not share it.')
        except Exception:
            return render(request,'register.html',error='Your account was created but email delivery failed. Try signing in again after email settings are fixed.')
        request.session['pending_signup']=user_id
        return go('/verify/signup')
    except sqlite3.IntegrityError:
        return render(request,'register.html',error='That email is already registered.')
    finally:
        c.close()


@app.get('/login')
def login_page(request: Request):
    if not is_adult(request): return go('/age')
    return render(request,'login.html')


@app.post('/login')
async def login(request: Request):
    if not is_adult(request): return go('/age')
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    email=str(form.get('email','')).strip().lower()
    password=str(form.get('password',''))
    if not EMAIL_READY:
        return render(request,'login.html',error='Sign-in verification is temporarily unavailable. Contact support.')
    c=connect()
    try:
        ip=request.client.host if request.client else 'unknown'
        if rate_limited(c,email,ip):
            return render(request,'login.html',error='Too many attempts. Please try again later.')
        u=c.execute('SELECT * FROM users WHERE email=? COLLATE NOCASE AND status=?',(email,'active')).fetchone()
        if not u or not verify_password(u['password_hash'],password):
            rate_limited(c,email,ip,increment=True)
            c.commit()
            return render(request,'login.html',error='Invalid email or password.')
        purpose='login' if u['is_verified'] else 'signup'
        last=c.execute('SELECT sent_at FROM email_codes WHERE user_id=? AND purpose=?',(u['id'],purpose)).fetchone()
        if last and utcnow()-datetime.fromisoformat(last['sent_at']) < timedelta(seconds=60):
            return render(request,'login.html',error='A verification email was sent recently. Please wait one minute before retrying.')
        code=new_code(c,u['id'],purpose)
        c.commit()
        try:
            send_email(u['email'],"Helix's Studio sign-in code" if purpose=='login' else "Helix's Studio verification code",
                       f'Your {purpose} code is {code}. It expires in 10 minutes. Do not share it.')
        except Exception:
            return render(request,'login.html',error='We could not deliver a sign-in email. Please contact support.')
        request.session['pending_'+purpose]=u['id']
        return go('/verify/'+purpose)
    finally:
        c.close()


@app.get('/verify/{purpose}')
def verify_page(request: Request, purpose: str):
    if not is_adult(request): return go('/age')
    if purpose not in ('signup','login','reset') or not request.session.get('pending_'+purpose):
        return go('/login')
    return render(request,'verify.html',purpose=purpose)


@app.post('/verify/{purpose}')
async def verify(request: Request,purpose: str):
    if purpose not in ('signup','login','reset'): return go('/login')
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    uid=request.session.get('pending_'+purpose)
    if not uid: return go('/login')
    code=str(form.get('code','')).strip()
    if not re.fullmatch(r'\d{6}',code):
        return render(request,'verify.html',purpose=purpose,error='Enter the six-digit verification code.')
    c=connect()
    try:
        if not check_code(c,uid,purpose,code):
            c.commit()
            return render(request,'verify.html',purpose=purpose,error='Invalid, expired, or used code. You may request another.')
        if purpose=='signup':
            c.execute('UPDATE users SET is_verified=1 WHERE id=?',(uid,))
        elif purpose=='reset':
            reset=c.execute('SELECT password_hash,created_at FROM password_resets WHERE user_id=?',(uid,)).fetchone()
            if not reset or utcnow()-datetime.fromisoformat(reset['created_at'])>timedelta(minutes=15):
                c.rollback()
                return go('/forgot')
            c.execute('UPDATE users SET password_hash=? WHERE id=?',(reset['password_hash'],uid))
            c.execute('DELETE FROM password_resets WHERE user_id=?',(uid,))
        c.commit()
        request.session.pop('pending_'+purpose,None)
        if purpose=='reset':
            flash(request,'Your password was reset. Please sign in.','success')
            return go('/login')
        request.session['uid']=uid
        request.session.pop('pending_signup',None)
        request.session.pop('pending_login',None)
        return go('/dashboard')
    finally:
        c.close()


@app.post('/resend/{purpose}')
async def resend(request: Request, purpose: str):
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    uid=request.session.get('pending_'+purpose)
    if purpose not in ('login','signup','reset') or not uid: return go('/login')
    c=connect()
    try:
        last=c.execute('SELECT sent_at FROM email_codes WHERE user_id=? AND purpose=?',(uid,purpose)).fetchone()
        if last and utcnow()-datetime.fromisoformat(last['sent_at']) < timedelta(seconds=60):
            flash(request,'Wait at least 60 seconds before requesting another code.','error')
            return go('/verify/'+purpose)
        u=c.execute('SELECT email FROM users WHERE id=?',(uid,)).fetchone()
        if not u: return go('/login')
        code=new_code(c,uid,purpose)
        c.commit()
        try: send_email(u['email'],"Your Helix's Studio code",f'Your new verification code is {code}; expires in 10 minutes.')
        except Exception: flash(request,'Email delivery failed. Try again later.','error')
        else: flash(request,'A new verification code was sent.','success')
        return go('/verify/'+purpose)
    finally: c.close()


@app.get('/forgot')
def forgot_page(request: Request):
    if not is_adult(request): return go('/age')
    return render(request,'forgot.html')


@app.post('/forgot')
async def forgot(request: Request):
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    email=str(form.get('email','')).strip().lower()
    password=str(form.get('new_password',''))
    if not valid_email(email) or not 12 <= len(password) <= 128 or not EMAIL_READY:
        return render(request,'forgot.html',error='Enter a valid email and new 12+ character password. Email delivery must be configured.')
    c=connect()
    try:
        u=c.execute('SELECT id,email FROM users WHERE email=? COLLATE NOCASE AND status=?',(email,'active')).fetchone()
        if not u:
            return render(request,'forgot.html',success='If that account exists, a code will be sent to its email address.')
        last=c.execute("SELECT sent_at FROM email_codes WHERE user_id=? AND purpose='reset'",(u['id'],)).fetchone()
        if last and utcnow()-datetime.fromisoformat(last['sent_at']) < timedelta(seconds=60):
            return render(request,'forgot.html',error='Please wait a minute before requesting another reset.')
        code=new_code(c,u['id'],'reset')
        c.execute('INSERT INTO password_resets(user_id,password_hash,created_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET password_hash=excluded.password_hash,created_at=excluded.created_at',
                  (u['id'],hash_password(password),iso()))
        c.commit()
        try: send_email(u['email'],"Helix's Studio password reset",f'Your password reset code is {code}. It expires in 10 minutes. Ignore this email if you did not request it.')
        except Exception: return render(request,'forgot.html',error='Email could not be sent. Try again later.')
        request.session['pending_reset']=u['id']
        return go('/verify/reset')
    finally: c.close()


@app.post('/logout')
async def logout(request: Request):
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    request.session.clear()
    return go('/age')


@app.get('/dashboard')
def dashboard(request: Request):
    gate=protected(request)
    if gate: return gate
    u=user_for(request)
    c=connect()
    try:
        payments=[dict(p) for p in c.execute('SELECT * FROM payment_requests WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall()]
        supports=[dict(p) for p in c.execute('SELECT * FROM support_messages WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall()]
    finally: c.close()
    rank=member_rank(u)
    return render(request,'dashboard.html',payments=payments,supports=supports,member_rank=rank,active_tier=TIER_BY_SLUG.get(u.get('tier_slug')))


@app.get('/checkout/{tier_slug}')
def checkout_page(request: Request, tier_slug: str):
    gate=protected(request)
    if gate: return gate
    tier=TIER_BY_SLUG.get(tier_slug)
    if not tier: return go('/#memberships')
    return render(request,'checkout.html',tier=tier,btc_address=BTC_ADDRESS,gift_instructions=GIFT_CARD_INSTRUCTIONS)



async def save_proof_async(file: UploadFile) -> str:
    raw=await file.read(4*1024*1024+1)
    if len(raw) > 4*1024*1024:
        raise ValueError('Proof image must be 4 MB or less.')
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ('PNG','JPEG','WEBP'):
                raise ValueError('Only JPG, PNG, or WebP receipts are supported.')
            if img.width*img.height > 16_000_000:
                raise ValueError('Image dimensions are too large.')
            img.verify()
        with Image.open(io.BytesIO(raw)) as img:
            copy=img.convert('RGB')
            filename=uuid.uuid4().hex+'.jpg'
            UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
            copy.save(UPLOAD_DIR/filename,'JPEG',quality=85,exif=b'')
            return filename
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError('That file is not a valid image.') from exc


@app.post('/checkout/{tier_slug}')
async def checkout_submit(request: Request, tier_slug: str):
    gate=protected(request)
    if gate: return gate
    tier=TIER_BY_SLUG.get(tier_slug)
    if not tier: return go('/#memberships')
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    method=str(form.get('method','')).strip()
    ref=str(form.get('reference','')).strip()
    brand=str(form.get('gift_brand','')).strip()
    proof=form.get('proof')
    if method not in ('bitcoin','gift_card') or not 4 <= len(ref) <= 120:
        return render(request,'checkout.html',tier=tier,btc_address=BTC_ADDRESS,gift_instructions=GIFT_CARD_INSTRUCTIONS,error='Choose a payment method and enter a valid transaction or receipt reference.')
    if method=='bitcoin' and not BTC_ADDRESS:
        return render(request,'checkout.html',tier=tier,btc_address=BTC_ADDRESS,gift_instructions=GIFT_CARD_INSTRUCTIONS,error='Bitcoin requests are not enabled yet.')
    if method=='gift_card' and (not GIFT_CARD_ENABLED or not 2<=len(brand)<=50):
        return render(request,'checkout.html',tier=tier,btc_address=BTC_ADDRESS,gift_instructions=GIFT_CARD_INSTRUCTIONS,error='Gift-card requests are unavailable, or the issuer was not provided.')
    if method=='gift_card' and re.search(r'(?<!\d)\d{12,}(?!\d)',ref):
        return render(request,'checkout.html',tier=tier,btc_address=BTC_ADDRESS,gift_instructions=GIFT_CARD_INSTRUCTIONS,error='Do not enter gift-card numbers or PINs. Use a merchant receipt reference only.')
    filename=None
    if isinstance(proof,UploadFile) and proof.filename:
        try: filename=await save_proof_async(proof)
        except ValueError as exc:
            return render(request,'checkout.html',tier=tier,btc_address=BTC_ADDRESS,gift_instructions=GIFT_CARD_INSTRUCTIONS,error=str(exc))
        finally:
            await proof.close()
    u=user_for(request)
    c=connect()
    try:
        existing=c.execute("SELECT 1 FROM payment_requests WHERE user_id=? AND status='pending' LIMIT 1",(u['id'],)).fetchone()
        if existing:
            if filename: (UPLOAD_DIR/filename).unlink(missing_ok=True)
            flash(request,'You already have a pending payment request. Please wait for a review.','error')
            return go('/dashboard')
        try:
            c.execute('''INSERT INTO payment_requests (user_id,tier_slug,amount,method,transaction_reference,gift_card_brand,proof_filename,status,submitted_at)
                         VALUES(?,?,?,?,?,?,?,?,?)''',
                      (u['id'],tier_slug,tier['price'],method,ref,brand if method=='gift_card' else None,filename,'pending',iso()))
            c.commit()
        except sqlite3.IntegrityError:
            if filename: (UPLOAD_DIR/filename).unlink(missing_ok=True)
            return render(request,'checkout.html',tier=tier,btc_address=BTC_ADDRESS,gift_instructions=GIFT_CARD_INSTRUCTIONS,error='That Bitcoin transaction reference is already in use. Contact support if you believe this is a mistake.')
        flash(request,'Your payment request was submitted. Membership unlocks only after independent payment verification and approval.','success')
        return go('/dashboard')
    finally: c.close()


@app.get('/content')
def content_page(request: Request):
    gate=protected(request)
    if gate: return gate
    u=user_for(request)
    return render(request,'content.html',member_rank=member_rank(u))


@app.post('/support')
async def support(request: Request):
    gate=protected(request)
    if gate: return gate
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    msg=str(form.get('message','')).strip()
    if not 5 <= len(msg) <= 2000:
        flash(request,'Support messages must be between 5 and 2,000 characters.','error')
        return go('/dashboard#support')
    u=user_for(request)
    with db_session() as c:
        c.execute('INSERT INTO support_messages (user_id,message,created_at) VALUES(?,?,?)',(u['id'],msg,iso()))
    flash(request,'Your message was sent to the studio support team.','success')
    return go('/dashboard#support')


@app.get('/admin')
def admin(request: Request):
    gate=protected(request,admin=True)
    if gate: return gate
    c=connect()
    try:
        users=[dict(u) for u in c.execute('SELECT id,full_name,email,is_verified,is_admin,status,tier_slug,member_until,joined_at FROM users ORDER BY id DESC LIMIT 300')]
        payments=[dict(p) for p in c.execute('''SELECT p.*,u.full_name,u.email FROM payment_requests p
                     JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 300''')]
        messages=[dict(m) for m in c.execute('''SELECT s.*,u.full_name,u.email FROM support_messages s
                     JOIN users u ON u.id=s.user_id ORDER BY s.id DESC LIMIT 200''')]
    finally: c.close()
    stats=dict(total_users=len(users),pending=sum(p['status']=='pending' for p in payments),approved=sum(p['status']=='approved' for p in payments),messages=sum(m['reply'] is None for m in messages))
    return render(request,'admin.html',customers=users,payments=payments,messages=messages,stats=stats)


@app.get('/admin/proof/{payment_id}')
def admin_proof(request: Request,payment_id: int):
    gate=protected(request,admin=True)
    if gate: return gate
    with db_session() as c:
        p=c.execute('SELECT proof_filename FROM payment_requests WHERE id=?',(payment_id,)).fetchone()
    if not p or not p['proof_filename']:
        return Response('Not found.',status_code=404)
    filepath=UPLOAD_DIR/Path(p['proof_filename']).name
    if not filepath.is_file():
        return Response('Not found.',status_code=404)
    return FileResponse(filepath,media_type='image/jpeg',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})


@app.post('/admin/payments/{payment_id}')
async def admin_payment(request: Request, payment_id: int):
    gate=protected(request,admin=True)
    if gate: return gate
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    action=str(form.get('action',''))
    note=str(form.get('note','')).strip()[:500]
    if action not in ('approve','reject'):
        return Response('Invalid action.',status_code=400)
    if action=='approve' and form.get('verified')!='yes':
        flash(request,'You must independently verify receipt of funds before approving. A screenshot alone is insufficient.','error')
        return go('/admin#payments')
    if action=='reject' and not note:
        flash(request,'Provide a reason when rejecting a payment request.','error')
        return go('/admin#payments')
    manager=user_for(request)
    c=connect()
    try:
        c.execute('BEGIN IMMEDIATE')
        p=c.execute("SELECT * FROM payment_requests WHERE id=? AND status='pending'",(payment_id,)).fetchone()
        if not p:
            c.rollback()
            flash(request,'Payment has already been reviewed or cannot be found.','error')
            return go('/admin#payments')
        status='approved' if action=='approve' else 'rejected'
        c.execute('UPDATE payment_requests SET status=?,reviewed_at=?,reviewer_id=?,review_note=? WHERE id=?',
                  (status,iso(),manager['id'],note,payment_id))
        if status=='approved':
            # Independent verification is a prerequisite. Extend access from previous expiry if active.
            u=c.execute('SELECT member_until FROM users WHERE id=?',(p['user_id'],)).fetchone()
            start=utcnow()
            if u and u['member_until']:
                try:
                    expiry=datetime.fromisoformat(u['member_until'])
                    if expiry>start: start=expiry
                except ValueError: pass
            expiry=add_months(start, MEMBERSHIP_MONTHS)
            c.execute('UPDATE users SET tier_slug=?,member_until=? WHERE id=?',(p['tier_slug'],iso(expiry),p['user_id']))
        c.execute('INSERT INTO audit_events(actor_id,action,subject_id,occurred_at) VALUES(?,?,?,?)',
                  (manager['id'],'payment_'+status,payment_id,iso()))
        recipient=c.execute('SELECT email FROM users WHERE id=?',(p['user_id'],)).fetchone()
        c.commit()
        if recipient and EMAIL_READY:
            try:
                send_email(recipient['email'], 'Helix’s Studio payment review update',
                           f'Your membership payment request #{payment_id} for {TIER_BY_SLUG[p["tier_slug"]]["name"]} has been {status}. '
                           + ('Your member access is now active.' if status=='approved' else 'See your dashboard for the review note, or contact support.'))
            except Exception:
                pass  # Review remains recorded; customer can see status in dashboard.
        flash(request,'Payment '+status+'. Customer access has been updated.' if status=='approved' else 'Payment rejected.','success')
        return go('/admin#payments')
    finally: c.close()


@app.post('/admin/customers/{user_id}')
async def admin_customer(request: Request, user_id: int):
    gate=protected(request,admin=True)
    if gate:return gate
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    action=str(form.get('action',''))
    if action not in ('suspend','restore'): return Response('Invalid action.',status_code=400)
    manager=user_for(request)
    if user_id==manager['id']:
        flash(request,'You cannot suspend your own administrator account.','error')
        return go('/admin#customers')
    with db_session() as c:
        target=c.execute('SELECT is_admin FROM users WHERE id=?',(user_id,)).fetchone()
        if target and not target['is_admin']:
            c.execute('UPDATE users SET status=? WHERE id=?',('suspended' if action=='suspend' else 'active',user_id))
            c.execute('INSERT INTO audit_events(actor_id,action,subject_id,occurred_at) VALUES(?,?,?,?)',(manager['id'],action+'_customer',user_id,iso()))
    flash(request,'Customer status updated.','success')
    return go('/admin#customers')


@app.post('/admin/support/{message_id}')
async def admin_support(request: Request,message_id: int):
    gate=protected(request,admin=True)
    if gate: return gate
    form=await request.form()
    if not valid_csrf(request,form): return Response('Invalid security token.',status_code=403)
    reply=str(form.get('reply','')).strip()
    if not 2<=len(reply)<=2000:
        flash(request,'Reply must be between 2 and 2,000 characters.','error')
        return go('/admin#support')
    manager=user_for(request)
    with db_session() as c:
        c.execute('UPDATE support_messages SET reply=?,replied_at=? WHERE id=?',(reply,iso(),message_id))
        c.execute('INSERT INTO audit_events(actor_id,action,subject_id,occurred_at) VALUES(?,?,?,?)',(manager['id'],'support_reply',message_id,iso()))
    flash(request,'Reply saved.','success')
    return go('/admin#support')


@app.get('/terms')
def terms(request: Request):
    return render(request,'terms.html')
