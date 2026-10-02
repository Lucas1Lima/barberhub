from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from jose import jwt, JWTError
from passlib.context import CryptContext
from pydantic import BaseModel, Field

app = FastAPI(title="BarberHub API", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("BARBERHUB_ORIGIN", "http://localhost:8000")],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Frontend: aceita frontend/index.html (estrutura do repo) ou index.html na raiz
_FRONTEND_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html"),
    os.path.join(os.path.dirname(__file__), "..", "index.html"),
    os.path.join(os.path.dirname(__file__), "frontend", "index.html"),
    os.path.join(os.path.dirname(__file__), "index.html"),
]
_FRONTEND = next((p for p in _FRONTEND_CANDIDATES if os.path.exists(p)), _FRONTEND_CANDIDATES[0])
_BOOK_PAGE = _FRONTEND.replace("index.html", "book.html")
_DB_PATH = os.environ.get("BARBERHUB_DB", os.path.join(os.path.dirname(__file__), "barberhub.db"))
# Secret obrigatório: gera e persiste na 1ª execução; BARBERHUB_SECRET sobrepõe (deploy)
_SECRET_FILE = os.path.join(os.path.dirname(__file__), ".secret")
_SECRET = os.environ.get("BARBERHUB_SECRET")
if not _SECRET:
    if os.path.exists(_SECRET_FILE):
        _SECRET = open(_SECRET_FILE).read().strip()
    else:
        import secrets as _secrets
        _SECRET = _secrets.token_hex(32)
        with open(_SECRET_FILE, "w") as f:
            f.write(_SECRET)
_TOKEN_HOURS = 24 * 7

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------- database
# Suporte a Postgres (produção) OU SQLite (dev): se DATABASE_URL existir, usa Postgres.
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
_IS_PG = _DATABASE_URL.startswith(("postgres://", "postgresql://"))

if _IS_PG:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool

    _pg_pool = psycopg2.pool.SimpleConnectionPool(1, 10, _DATABASE_URL)

    @contextmanager
    def db(immediate: bool = False):
        conn = _pg_pool.getconn()
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        try:
            yield conn
            conn.commit()
        finally:
            _pg_pool.putconn(conn)

    # Postgres não usa PRAGMA nem BEGIN IMMEDIATE (MVCC já resolve a concorrência)

else:
    @contextmanager
    def db(immediate: bool = False):
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        if immediate:
            conn.execute("BEGIN IMMEDIATE")   # lock de escrita — evita race no conflito de agenda
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    token TEXT UNIQUE NOT NULL,
    shop_name TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_by INTEGER NOT NULL REFERENCES shops(id)
);
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS professionals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'barbeiro',
    chair_rental INTEGER NOT NULL DEFAULT 0,
    fixed_fee REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    name TEXT NOT NULL,
    price REAL NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    birthday TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    professional_id INTEGER NOT NULL REFERENCES professionals(id),
    service_id INTEGER NOT NULL REFERENCES services(id),
    client_id INTEGER REFERENCES clients(id),
    client_name TEXT NOT NULL,
    start TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'agendado',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commission_rules (
    shop_id INTEGER NOT NULL,
    professional_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (shop_id, professional_id, service_id)
);
CREATE TABLE IF NOT EXISTS cash_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    professional_id INTEGER NOT NULL REFERENCES professionals(id),
    day TEXT NOT NULL,
    start_time TEXT NOT NULL DEFAULT '00:00',
    end_time TEXT NOT NULL DEFAULT '23:59',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    preferred_day TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    plan_name TEXT NOT NULL DEFAULT 'Mensal',
    cuts_per_month INTEGER NOT NULL DEFAULT 2,
    price REAL NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    shop_id INTEGER PRIMARY KEY REFERENCES shops(id),
    loyalty_n INTEGER NOT NULL DEFAULT 10,          -- a cada N cortes, 1 grátis
    inactive_days INTEGER NOT NULL DEFAULT 30,      -- cliente inativo após X dias
    reminder_hours INTEGER NOT NULL DEFAULT 24      -- lembrete X horas antes
);
"""


def _migrate():
    with db() as conn:
        if _IS_PG:
            # Postgres: tipos e sintaxe diferentes — DDL adaptado
            pg_schema = SCHEMA
            pg_schema = pg_schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            pg_schema = pg_schema.replace("INTEGER PRIMARY KEY REFERENCES", "INTEGER PRIMARY KEY REFERENCES")
            pg_schema = pg_schema.replace("PRIMARY KEY (shop_id, professional_id, service_id)",
                                          "PRIMARY KEY (shop_id, professional_id, service_id)")
            conn.cursor().execute(pg_schema)
        else:
            conn.executescript(SCHEMA)
        # migração incremental: data de conclusão + flag de corte grátis (fidelidade)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()]
        if "concluded_at" not in cols:
            conn.execute("ALTER TABLE appointments ADD COLUMN concluded_at TEXT")
        if "loyalty_used" not in cols:
            conn.execute("ALTER TABLE appointments ADD COLUMN loyalty_used INTEGER NOT NULL DEFAULT 0")
        # migração v0.8: aniversário do cliente
        cli_cols = [r["name"] for r in conn.execute("PRAGMA table_info(clients)").fetchall()]
        if "birthday" not in cli_cols:
            conn.execute("ALTER TABLE clients ADD COLUMN birthday TEXT NOT NULL DEFAULT ''")
        # migração v0.8: foto do profissional
        prof_cols = [r["name"] for r in conn.execute("PRAGMA table_info(professionals)").fetchall()]
        if "photo" not in prof_cols:
            conn.execute("ALTER TABLE professionals ADD COLUMN photo TEXT NOT NULL DEFAULT ''")
        # migração v0.8: relatório semanal
        shop_cols = [r["name"] for r in conn.execute("PRAGMA table_info(shops)").fetchall()]
        if "weekly_report" not in shop_cols:
            conn.execute("ALTER TABLE shops ADD COLUMN weekly_report INTEGER NOT NULL DEFAULT 1")
        if "last_report_sent" not in shop_cols:
            conn.execute("ALTER TABLE shops ADD COLUMN last_report_sent TEXT")
        shop_cols = [r["name"] for r in conn.execute("PRAGMA table_info(shops)").fetchall()]
        if "is_admin" not in shop_cols:
            conn.execute("ALTER TABLE shops ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if "active" not in shop_cols:
            conn.execute("ALTER TABLE shops ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "last_login" not in shop_cols:
            conn.execute("ALTER TABLE shops ADD COLUMN last_login TEXT")
        # 1º usuário registrado vira admin automaticamente (se ainda não houver admin)
        if not conn.execute("SELECT 1 FROM shops WHERE is_admin=1").fetchone():
            conn.execute("UPDATE shops SET is_admin=1 WHERE id=(SELECT MIN(id) FROM shops)")
        # settings default para lojas existentes criadas antes da tabela settings
        conn.execute("INSERT OR IGNORE INTO settings (shop_id) SELECT id FROM shops")


_migrate()


def _now() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------- auth
def _make_token(shop_id: int) -> str:
    payload = {"shop_id": shop_id, "exp": datetime.utcnow() + timedelta(hours=_TOKEN_HOURS)}
    return jwt.encode(payload, _SECRET, algorithm="HS256")


def current_shop(authorization: str = Header(default="")) -> int:
    """Dependency: extrai shop_id do token Bearer e exige conta ativa."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Faça login para continuar")
    token = authorization[7:]
    try:
        data = jwt.decode(token, _SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Sessão expirada — faça login novamente")
    shop_id = int(data["shop_id"])
    with db() as conn:
        row = conn.execute("SELECT active FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not row or not row["active"]:
        raise HTTPException(403, "Conta desativada — fale com o suporte")
    return shop_id


class AuthIn(BaseModel):
    email: str = Field(min_length=5)
    password: str = Field(min_length=6)
    shop_name: str = ""     # só no registro


@app.post("/auth/register")
def register(body: AuthIn):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(422, "Email inválido")
    with db() as conn:
        if conn.execute("SELECT 1 FROM shops WHERE owner_email=?", (email,)).fetchone():
            raise HTTPException(409, "Este email já tem conta — faça login")
        try:
            cur = conn.execute(
                "INSERT INTO shops (name, owner_email, password_hash, created_at) VALUES (?,?,?,?)",
                (body.shop_name or "Minha Barbearia", email, _pwd.hash(body.password), _now().isoformat()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Este email já tem conta — faça login")
        shop_id = cur.lastrowid
        # 1º usuário da plataforma vira admin
        if not conn.execute("SELECT 1 FROM shops WHERE is_admin=1").fetchone():
            conn.execute("UPDATE shops SET is_admin=1 WHERE id=?", (shop_id,))
    return {"shop_id": shop_id, "token": _make_token(shop_id), "shop_name": body.shop_name or "Minha Barbearia"}


@app.post("/auth/login")
def login(body: AuthIn):
    email = body.email.strip().lower()
    with db() as conn:
        row = conn.execute("SELECT id, password_hash, active FROM shops WHERE owner_email=?", (email,)).fetchone()
    if not row or not _pwd.verify(body.password, row["password_hash"]):
        raise HTTPException(401, "Email ou senha incorretos")
    if not row["active"]:
        raise HTTPException(403, "Conta desativada — fale com o suporte")
    with db() as conn:
        conn.execute("UPDATE shops SET last_login=? WHERE id=?", (_now().isoformat(), row["id"]))
    return {"shop_id": row["id"], "token": _make_token(row["id"])}


@app.get("/auth/me")
def me(shop_id: int = Depends(current_shop)):
    with db() as conn:
        row = conn.execute("SELECT id, name, owner_email, is_admin FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Conta não encontrada")
    d = dict(row)
    import unicodedata
    norm = unicodedata.normalize("NFKD", d["name"]).encode("ascii", "ignore").decode()
    d["slug"] = f"b{d['id']}-" + re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return d


class ChangeEmailIn(BaseModel):
    new_email: str
    password: str


@app.patch("/auth/email")
def change_email(body: ChangeEmailIn, shop_id: int = Depends(current_shop)):
    new_email = body.new_email.strip().lower()
    if "@" not in new_email:
        raise HTTPException(422, "Email inválido")
    with db() as conn:
        row = conn.execute("SELECT password_hash FROM shops WHERE id=?", (shop_id,)).fetchone()
        if not _pwd.verify(body.password, row["password_hash"]):
            raise HTTPException(401, "Senha incorreta")
        if conn.execute("SELECT 1 FROM shops WHERE owner_email=?", (new_email,)).fetchone():
            raise HTTPException(409, "Este email já está em uso")
        try:
            conn.execute("UPDATE shops SET owner_email=? WHERE id=?", (new_email, shop_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Este email já está em uso")
    return {"ok": True, "email": new_email}


class ChangePassIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


@app.patch("/auth/password")
def change_password(body: ChangePassIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        row = conn.execute("SELECT password_hash FROM shops WHERE id=?", (shop_id,)).fetchone()
        if not _pwd.verify(body.old_password, row["password_hash"]):
            raise HTTPException(401, "Senha atual incorreta")
        conn.execute("UPDATE shops SET password_hash=? WHERE id=?", (_pwd.hash(body.new_password), shop_id))
    return {"ok": True}


# ---------------------------------------------------------------- recuperação de senha (token de 1 uso)
import secrets as _secrets_mod


def _send_email(to: str, subject: str, body_text: str) -> bool:
    """Envia email via SMTP se configurado (SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_FROM).
    Sem SMTP configurado, registra no log do servidor (modo dev)."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"[EMAIL-> {to}] {subject}: {body_text[:120]}")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        user = os.environ.get("SMTP_USER", "")
        msg = MIMEText(body_text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = os.environ.get("SMTP_FROM", user)
        msg["To"] = to
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            if user:
                s.login(user, os.environ.get("SMTP_PASS", ""))
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"[EMAIL ERRO -> {to}] {e}")
        return False


def _base_url() -> str:
    return os.environ.get("BARBERHUB_ORIGIN", "http://localhost:8000").rstrip("/")


class ResetRequestIn(BaseModel):
    email: str


@app.post("/auth/forgot-password")
def forgot_password(body: ResetRequestIn):
    email = body.email.strip().lower()
    with db() as conn:
        row = conn.execute("SELECT id FROM shops WHERE owner_email=?", (email,)).fetchone()
        if row:
            token = _secrets_mod.token_urlsafe(24)
            expires = (_now() + timedelta(hours=1)).isoformat()
            conn.execute("INSERT INTO password_resets (email, token, expires_at) VALUES (?,?,?)",
                         (email, token, expires))
            link = f"{_base_url()}/reset?token={token}"
            sent = _send_email(email, "BarberHub — redefinir senha",
                               f"Redefina sua senha em 1 hora: {link}\nSe não foi você, ignore este email.")
            return {"ok": True, "sent": sent, "link": link if not sent else None}
    # resposta igual com ou sem conta (não revela quais emails existem)
    return {"ok": True, "sent": False, "link": None}


class ResetIn(BaseModel):
    token: str
    new_password: str = Field(min_length=6)


@app.post("/auth/reset-password")
def reset_password(body: ResetIn):
    with db() as conn:
        row = conn.execute(
            "SELECT email, expires_at, used_at FROM password_resets WHERE token=?", (body.token,)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < _now().isoformat():
            raise HTTPException(422, "Token inválido ou expirado")
        conn.execute("UPDATE password_resets SET used_at=? WHERE token=?", (_now().isoformat(), body.token))
        conn.execute("UPDATE shops SET password_hash=? WHERE owner_email=?",
                     (_pwd.hash(body.new_password), row["email"]))
    return {"ok": True}


# ---------------------------------------------------------------- admin
def require_admin(shop_id: int = Depends(current_shop)) -> int:
    with db() as conn:
        row = conn.execute("SELECT is_admin FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not row or not row["is_admin"]:
        raise HTTPException(403, "Acesso restrito ao administrador")
    return shop_id


@app.get("/admin/shops")
def admin_list_shops(admin_id: int = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute("""
            SELECT s.id, s.name, s.owner_email, s.created_at, s.active, s.last_login, s.is_admin,
                   (SELECT COUNT(*) FROM appointments a WHERE a.shop_id = s.id) AS appointments,
                   (SELECT COUNT(*) FROM appointments a WHERE a.shop_id = s.id AND a.created_at >= datetime('now','-30 day')) AS appts_30d
            FROM shops s ORDER BY s.id""").fetchall()
        return [dict(r) for r in rows]


class ShopAdminPatch(BaseModel):
    active: Optional[bool] = None
    make_admin: Optional[bool] = None


@app.patch("/admin/shops/{shop_id}")
def admin_patch_shop(shop_id: int, body: ShopAdminPatch, admin_id: int = Depends(require_admin)):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
            raise HTTPException(404, f"Loja {shop_id} não encontrada")
        if body.active is not None:
            if shop_id == admin_id and not body.active:
                raise HTTPException(422, "Você não pode desativar sua própria conta")
            conn.execute("UPDATE shops SET active=? WHERE id=?", (int(body.active), shop_id))
        if body.make_admin is not None:
            conn.execute("UPDATE shops SET is_admin=? WHERE id=?", (int(body.make_admin), shop_id))
        return dict(conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone())


@app.post("/admin/shops/{shop_id}/reset-password")
def admin_reset_password(shop_id: int, admin_id: int = Depends(require_admin)):
    """Gera senha temporária e a retorna para o admin passar ao cliente."""
    import secrets as _s
    temp = _s.token_urlsafe(6)   # ex.: "aB3xY_9Q" — fácil de digitar
    with db() as conn:
        if not conn.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
            raise HTTPException(404, f"Loja {shop_id} não encontrada")
        conn.execute("UPDATE shops SET password_hash=? WHERE id=?", (_pwd.hash(temp), shop_id))
    return {"temporary_password": temp}


# ---------------------------------------------------------------- convites (infoproduto: acesso temporário por email)
class InviteIn(BaseModel):
    email: str
    shop_name: str = ""


@app.post("/admin/invites")
def admin_create_invite(body: InviteIn, admin_id: int = Depends(require_admin)):
    """Cria convite de 48h: link único que registra a conta com o email já definido."""
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(422, "Email inválido")
    token = _secrets_mod.token_urlsafe(24)
    expires = (_now() + timedelta(hours=48)).isoformat()
    with db() as conn:
        if conn.execute("SELECT 1 FROM shops WHERE owner_email=?", (email,)).fetchone():
            raise HTTPException(409, "Este email já tem conta")
        cur = conn.execute(
            "INSERT INTO invites (email, token, shop_name, expires_at, created_by) VALUES (?,?,?,?,?)",
            (email, token, body.shop_name or "Minha Barbearia", expires, admin_id))
        invite_id = cur.lastrowid
    link = f"{_base_url()}/invite?token={token}"
    sent = _send_email(email, "Seu acesso ao BarberHub",
                       f"Bem-vindo! Ative seu acesso em 48 horas: {link}\n"
                       f"Depois de ativar, defina sua senha e (se quiser) troque o email de acesso.")
    return {"id": invite_id, "email": email, "expires_at": expires,
            "link": link, "sent": sent}


@app.get("/admin/invites")
def admin_list_invites(admin_id: int = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute("SELECT * FROM invites ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


@app.get("/auth/invite/{token}")
def invite_info(token: str):
    """Valida o convite (público) e retorna o email pré-cadastrado."""
    with db() as conn:
        row = conn.execute("SELECT email, shop_name, expires_at, used_at FROM invites WHERE token=?", (token,)).fetchone()
    if not row or row["used_at"] or row["expires_at"] < _now().isoformat():
        raise HTTPException(422, "Convite inválido ou expirado")
    return {"email": row["email"], "shop_name": row["shop_name"]}


class InviteAcceptIn(BaseModel):
    token: str
    password: str = Field(min_length=6)


@app.post("/auth/invite/accept")
def invite_accept(body: InviteAcceptIn):
    """Ativa o convite: cria a conta com o email do convite e a senha escolhida."""
    with db() as conn:
        row = conn.execute("SELECT email, shop_name, expires_at, used_at FROM invites WHERE token=?", (body.token,)).fetchone()
        if not row or row["used_at"] or row["expires_at"] < _now().isoformat():
            raise HTTPException(422, "Convite inválido ou expirado")
        if conn.execute("SELECT 1 FROM shops WHERE owner_email=?", (row["email"],)).fetchone():
            raise HTTPException(409, "Este email já tem conta — faça login")
        cur = conn.execute(
            "INSERT INTO shops (name, owner_email, password_hash, created_at) VALUES (?,?,?,?)",
            (row["shop_name"], row["email"], _pwd.hash(body.password), _now().isoformat()))
        shop_id = cur.lastrowid
        conn.execute("UPDATE invites SET used_at=? WHERE token=?", (_now().isoformat(), body.token))
    return {"shop_id": shop_id, "token": _make_token(shop_id), "shop_name": row["shop_name"], "email": row["email"]}


# ---------------------------------------------------------------- models
class ProfessionalIn(BaseModel):
    name: str
    role: str = "barbeiro"
    chair_rental: bool = False
    fixed_fee: float = 0.0


class ProfessionalPatch(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    chair_rental: Optional[bool] = None
    fixed_fee: Optional[float] = None


class ServiceIn(BaseModel):
    name: str
    price: float = Field(gt=0)
    duration_min: int = Field(gt=0, default=30)


class ServicePatch(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0)
    duration_min: Optional[int] = Field(default=None, gt=0)


class ClientIn(BaseModel):
    name: str
    phone: str = ""
    birthday: str = ""          # "MM-DD" ou "YYYY-MM-DD" (opcional)


class ClientPatch(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    birthday: Optional[str] = None


class CommissionRuleIn(BaseModel):
    professional_id: int
    service_id: int
    kind: str = "percent"       # percent | fixed | chair
    value: float = Field(gt=0)


class AppointmentIn(BaseModel):
    professional_id: int
    service_id: int
    client_id: Optional[int] = None
    client_name: str = ""
    start: str
    status: str = "agendado"


class AppointmentStatusIn(BaseModel):
    status: str


class CashEntryIn(BaseModel):
    day: str
    kind: str
    description: str = ""
    amount: float


# ---------------------------------------------------------------- helpers
def _get_scoped(conn, table: str, obj_id: int, shop_id: int, label: str):
    row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND shop_id=?", (obj_id, shop_id)).fetchone()
    if not row:
        raise HTTPException(404, f"{label} {obj_id} não encontrado")
    return dict(row)


def _settings(conn, shop_id: int) -> dict:
    row = conn.execute("SELECT * FROM settings WHERE shop_id=?", (shop_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO settings (shop_id) VALUES (?)", (shop_id,))
        row = conn.execute("SELECT * FROM settings WHERE shop_id=?", (shop_id,)).fetchone()
    return dict(row)


def _wa_link(phone: str, message: str) -> str:
    """Link wa.me com encode correto e DDI validado."""
    from urllib.parse import quote
    digits = re.sub(r"\D", "", phone)
    if len(digits) in (10, 11):
        digits = "55" + digits
    elif len(digits) < 10 or len(digits) > 13:
        digits = ""
    return f"https://wa.me/{digits}?text={quote(message)}"


def _parse_start(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, "start inválido: use ISO '2026-10-01T14:00'")
    if dt.tzinfo is not None:
        # normaliza para hora local naive — senão o filtro por dia (LIKE) quebra
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _check_conflict(conn, shop_id: int, professional_id: int, start: datetime, duration_min: int) -> None:
    end = start + timedelta(minutes=duration_min)
    rows = conn.execute(
        "SELECT start, duration_min FROM appointments WHERE shop_id=? AND professional_id=? AND status!='cancelado'",
        (shop_id, professional_id)).fetchall()
    for r in rows:
        ap_start = datetime.fromisoformat(r["start"])
        ap_end = ap_start + timedelta(minutes=r["duration_min"])
        if start < ap_end and ap_start < end:
            raise HTTPException(409, "Conflito: profissional já tem agendamento nesse horário")


def _commission_for(conn, shop_id: int, professional_id: int, service_id: int,
                    service_price: float) -> dict:
    """Regra por (loja, profissional, serviço). Sem regra: cadeira alugada usa 'chair', senão 50%.
    'chair' = profissional fica 100% do serviço; a taxa de aluguel é descontada 1× no relatório do dia."""
    rule = conn.execute(
        "SELECT kind, value FROM commission_rules WHERE shop_id=? AND professional_id=? AND service_id=?",
        (shop_id, professional_id, service_id)).fetchone()
    prof = _get_scoped(conn, "professionals", professional_id, shop_id, "Profissional")
    if rule:
        kind, value = rule["kind"], rule["value"]
    elif prof.get("chair_rental"):
        kind, value = "chair", prof.get("fixed_fee", 0.0)
    else:
        kind, value = "percent", 50.0
    if kind == "percent":
        commission = round(service_price * value / 100, 2)
    elif kind == "fixed":
        commission = round(value, 2)
    else:  # chair — 100% do serviço; aluguel descontado uma vez no fechamento do dia
        commission = round(service_price, 2)
    return {"kind": kind, "value": value, "commission": commission}


def _whatsapp_link(phone: str, message: str) -> str:
    from urllib.parse import quote
    digits = re.sub(r"\D", "", phone)
    if len(digits) in (10, 11):        # DDD + número sem DDI
        digits = "55" + digits
    elif len(digits) < 10 or len(digits) > 13:
        digits = ""                    # número inválido → link sem destino
    return f"https://wa.me/{digits}?text={quote(message)}"


# ---------------------------------------------------------------- professionals
@app.post("/professionals")
def create_professional(body: ProfessionalIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO professionals (shop_id, name, role, chair_rental, fixed_fee) VALUES (?,?,?,?,?)",
            (shop_id, body.name, body.role, int(body.chair_rental), body.fixed_fee))
        return {**body.model_dump(), "id": cur.lastrowid, "shop_id": shop_id}


@app.get("/professionals")
def list_professionals(shop_id: int = Depends(current_shop)):
    with db() as conn:
        rows = conn.execute("SELECT * FROM professionals WHERE shop_id=?", (shop_id,)).fetchall()
        return [dict(r) for r in rows]


@app.patch("/professionals/{prof_id}")
def update_professional(prof_id: int, body: ProfessionalPatch, shop_id: int = Depends(current_shop)):
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(422, "Nada para atualizar")
    with db() as conn:
        _get_scoped(conn, "professionals", prof_id, shop_id, "Profissional")
        sets = ", ".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE professionals SET {sets} WHERE id=?", (*data.values(), prof_id))
        return _get_scoped(conn, "professionals", prof_id, shop_id, "Profissional")


# ---------------------------------------------------------------- services
@app.post("/services")
def create_service(body: ServiceIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO services (shop_id, name, price, duration_min) VALUES (?,?,?,?)",
            (shop_id, body.name, body.price, body.duration_min))
        return {**body.model_dump(), "id": cur.lastrowid, "shop_id": shop_id}


@app.get("/services")
def list_services(shop_id: int = Depends(current_shop)):
    with db() as conn:
        rows = conn.execute("SELECT * FROM services WHERE shop_id=?", (shop_id,)).fetchall()
        return [dict(r) for r in rows]


@app.patch("/services/{svc_id}")
def update_service(svc_id: int, body: ServicePatch, shop_id: int = Depends(current_shop)):
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(422, "Nada para atualizar")
    with db() as conn:
        _get_scoped(conn, "services", svc_id, shop_id, "Serviço")
        sets = ", ".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE services SET {sets} WHERE id=?", (*data.values(), svc_id))
        return _get_scoped(conn, "services", svc_id, shop_id, "Serviço")


# ---------------------------------------------------------------- clients
@app.post("/clients")
def create_client(body: ClientIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        cur = conn.execute("INSERT INTO clients (shop_id, name, phone, birthday) VALUES (?,?,?,?)",
                           (shop_id, body.name, body.phone, body.birthday))
        return {**body.model_dump(), "id": cur.lastrowid, "shop_id": shop_id}


@app.get("/clients")
def list_clients(shop_id: int = Depends(current_shop)):
    with db() as conn:
        rows = conn.execute("SELECT * FROM clients WHERE shop_id=?", (shop_id,)).fetchall()
        return [dict(r) for r in rows]


@app.patch("/clients/{cli_id}")
def update_client(cli_id: int, body: ClientPatch, shop_id: int = Depends(current_shop)):
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(422, "Nada para atualizar")
    with db() as conn:
        _get_scoped(conn, "clients", cli_id, shop_id, "Cliente")
        sets = ", ".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE clients SET {sets} WHERE id=?", (*data.values(), cli_id))
        return _get_scoped(conn, "clients", cli_id, shop_id, "Cliente")


@app.get("/clients/{client_id}/history")
def client_history(client_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        cli = _get_scoped(conn, "clients", client_id, shop_id, "Cliente")
        rows = conn.execute(
            "SELECT * FROM appointments WHERE client_id=? AND shop_id=? ORDER BY start DESC",
            (client_id, shop_id)).fetchall()
        return {"client": cli, "appointments": [dict(r) for r in rows]}


# ---------------------------------------------------------------- commission rules
@app.post("/commission-rules")
def set_commission_rule(body: CommissionRuleIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "professionals", body.professional_id, shop_id, "Profissional")
        _get_scoped(conn, "services", body.service_id, shop_id, "Serviço")
        conn.execute(
            "INSERT OR REPLACE INTO commission_rules (shop_id, professional_id, service_id, kind, value) VALUES (?,?,?,?,?)",
            (shop_id, body.professional_id, body.service_id, body.kind, body.value))
    return {**body.model_dump(), "shop_id": shop_id}


@app.get("/commission-rules")
def list_commission_rules(shop_id: int = Depends(current_shop)):
    with db() as conn:
        rows = conn.execute("SELECT * FROM commission_rules WHERE shop_id=?", (shop_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- appointments
@app.post("/appointments")
def create_appointment(body: AppointmentIn, shop_id: int = Depends(current_shop)):
    if body.status != "agendado":
        raise HTTPException(422, "Novo agendamento deve começar como 'agendado'")
    with db(immediate=True) as conn:
        prof = _get_scoped(conn, "professionals", body.professional_id, shop_id, "Profissional")
        svc = _get_scoped(conn, "services", body.service_id, shop_id, "Serviço")
        start = _parse_start(body.start)
        _check_conflict(conn, shop_id, body.professional_id, start, svc["duration_min"])
        # bloqueio de agenda (folga/intervalo) do profissional
        b = conn.execute(
            """SELECT 1 FROM blocks WHERE shop_id=? AND professional_id=? AND day=?
               AND (?) < end_time AND (?) > start_time""",
            (shop_id, body.professional_id, start.date().isoformat(),
             start.strftime("%H:%M"),
             (start + timedelta(minutes=svc["duration_min"])).strftime("%H:%M"))).fetchone()
        if b:
            raise HTTPException(409, "Profissional está com a agenda bloqueada nesse horário (folga/intervalo)")
        cli = conn.execute("SELECT name FROM clients WHERE id=? AND shop_id=?",
                           (body.client_id, shop_id)).fetchone() if body.client_id else None
        client_name = (cli["name"] if cli else None) or body.client_name or "Cliente avulso"
        cur = conn.execute(
            "INSERT INTO appointments (shop_id, professional_id, service_id, client_id, client_name, start, duration_min, price, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (shop_id, body.professional_id, body.service_id, body.client_id,
             client_name, start.isoformat(), svc["duration_min"], svc["price"], body.status, _now().isoformat()))
        return {"id": cur.lastrowid, "shop_id": shop_id,
                "professional_id": body.professional_id, "professional_name": prof["name"],
                "service_id": body.service_id, "service_name": svc["name"],
                "price": svc["price"], "duration_min": svc["duration_min"],
                "client_id": body.client_id, "client_name": client_name,
                "start": start.isoformat(), "status": body.status}


@app.get("/appointments")
def list_appointments(day: Optional[str] = None, shop_id: int = Depends(current_shop)):
    with db() as conn:
        sql = """SELECT a.*, p.name AS professional_name, s.name AS service_name
                 FROM appointments a
                 JOIN professionals p ON p.id = a.professional_id
                 JOIN services s ON s.id = a.service_id
                 WHERE a.shop_id=?"""
        params = [shop_id]
        if day:
            sql += " AND a.start LIKE ?"
            params.append(day + "%")
        rows = conn.execute(sql + " ORDER BY a.start", params).fetchall()
        return [dict(r) for r in rows]


@app.patch("/appointments/{ap_id}/status")
def update_status(ap_id: int, body: AppointmentStatusIn, shop_id: int = Depends(current_shop)):
    if body.status not in ("agendado", "concluido", "faltou", "cancelado"):
        raise HTTPException(422, "status inválido")
    with db() as conn:
        _get_scoped(conn, "appointments", ap_id, shop_id, "Agendamento")
        concluded_at = _now().isoformat() if body.status == "concluido" else None
        conn.execute("UPDATE appointments SET status=?, concluded_at=? WHERE id=?",
                     (body.status, concluded_at, ap_id))
        return {"id": ap_id, "status": body.status}


@app.get("/appointments/{ap_id}/reminder")
def appointment_reminder(ap_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        ap = _get_scoped(conn, "appointments", ap_id, shop_id, "Agendamento")
        svc = conn.execute("SELECT name FROM services WHERE id=?", (ap["service_id"],)).fetchone()
        cli = conn.execute("SELECT phone FROM clients WHERE id=?", (ap["client_id"],)).fetchone() if ap["client_id"] else None
        shop = conn.execute("SELECT name FROM shops WHERE id=?", (shop_id,)).fetchone()
    phone = cli["phone"] if cli else ""
    when = ap["start"][:16].replace("T", " às ")
    msg = f"Oi {ap['client_name']}! Lembrete do seu horário na {shop['name']}: {when} — {svc['name']}. Até logo!"
    return {"whatsapp_link": _whatsapp_link(phone, msg), "message": msg}


# ---------------------------------------------------------------- comissão / caixa
@app.get("/reports/commissions")
def commissions_report(day: Optional[str] = None, shop_id: int = Depends(current_shop)):
    with db() as conn:
        if day:
            rows = conn.execute(
                "SELECT professional_id, service_id, price FROM appointments WHERE shop_id=? AND status='concluido' AND start LIKE ?",
                (shop_id, day + "%")).fetchall()
        else:
            rows = conn.execute(
                "SELECT professional_id, service_id, price FROM appointments WHERE shop_id=? AND status='concluido'",
                (shop_id,)).fetchall()
        per_prof: dict[int, dict] = {}
        chair_fee: dict[int, float] = {}
        for r in rows:
            if r["professional_id"] not in per_prof:
                prof = conn.execute("SELECT name, chair_rental, fixed_fee FROM professionals WHERE id=?", (r["professional_id"],)).fetchone()
                per_prof[r["professional_id"]] = {
                    "professional_id": r["professional_id"],
                    "professional_name": prof["name"] if prof else "?",
                    "services": 0, "revenue": 0.0, "commission": 0.0}
                if prof and prof["chair_rental"]:
                    chair_fee[r["professional_id"]] = prof["fixed_fee"] or 0.0
            rule = _commission_for(conn, shop_id, r["professional_id"], r["service_id"], r["price"])
            p = per_prof[r["professional_id"]]
            p["services"] += 1
            p["revenue"] += r["price"]
            p["commission"] += rule["commission"]
        # aluguel de cadeira: desconta a taxa diária UMA vez por profissional
        for pid, fee in chair_fee.items():
            per_prof[pid]["commission"] = round(per_prof[pid]["commission"] - fee, 2)
        for p in per_prof.values():
            p["revenue"] = round(p["revenue"], 2)
            p["commission"] = round(p["commission"], 2)
        return {"day": day or "todos", "professionals": list(per_prof.values())}


@app.get("/reports/daily-cash")
def daily_cash(day: Optional[str] = None, shop_id: int = Depends(current_shop)):
    day = day or date.today().isoformat()
    with db() as conn:
        # receita fecha pela DATA DA CONCLUSÃO (concluded_at), não pela data do agendamento
        row = conn.execute(
            "SELECT COALESCE(SUM(price),0) AS rev, COUNT(*) AS n FROM appointments WHERE shop_id=? AND status='concluido' AND concluded_at LIKE ?",
            (shop_id, day + "%")).fetchone()
        entries = conn.execute(
            "SELECT * FROM cash_entries WHERE shop_id=? AND day=?", (shop_id, day)).fetchall()
        manual_income = round(sum(e["amount"] for e in entries if e["kind"] == "income"), 2)
        expenses = round(sum(e["amount"] for e in entries if e["kind"] == "expense"), 2)
        return {
            "day": day,
            "services_revenue": round(row["rev"], 2),
            "manual_income": manual_income,
            "expenses": expenses,
            "net": round(row["rev"] + manual_income - expenses, 2),
            "appointments_count": row["n"],
            "manual_entries": [dict(e) for e in entries],
        }


@app.post("/cash-entries")
def add_cash_entry(body: CashEntryIn, shop_id: int = Depends(current_shop)):
    if body.kind not in ("income", "expense"):
        raise HTTPException(422, "kind deve ser income ou expense")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO cash_entries (shop_id, day, kind, description, amount) VALUES (?,?,?,?,?)",
            (shop_id, body.day, body.kind, body.description, body.amount))
        return {**body.model_dump(), "id": cur.lastrowid, "shop_id": shop_id}


@app.delete("/cash-entries/{entry_id}")
def delete_cash_entry(entry_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "cash_entries", entry_id, shop_id, "Lançamento")
        conn.execute("DELETE FROM cash_entries WHERE id=?", (entry_id,))
    return {"ok": True}


@app.get("/reports/cash-month")
def cash_month(year: int, month: int, shop_id: int = Depends(current_shop)):
    """Mapa do mês: por dia, entradas e saídas (serviços concluídos + manuais) — para o calendário."""
    if not (1 <= month <= 12):
        raise HTTPException(422, "month deve ser 1-12")
    prefix = f"{year:04d}-{month:02d}-"
    with db() as conn:
        svc_rows = conn.execute(
            """SELECT substr(concluded_at,1,10) AS d, SUM(price) AS total
               FROM appointments WHERE shop_id=? AND status='concluido' AND concluded_at LIKE ?
               GROUP BY d""", (shop_id, prefix + "%")).fetchall()
        manual_rows = conn.execute(
            """SELECT day AS d,
                      SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) AS inc,
                      SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) AS exp
               FROM cash_entries WHERE shop_id=? AND day LIKE ?
               GROUP BY day""", (shop_id, prefix + "%")).fetchall()
        days: dict[str, dict] = {}
        for r in svc_rows:
            days.setdefault(r["d"], {"income": 0.0, "expense": 0.0})
            days[r["d"]]["income"] += round(r["total"], 2)
        for r in manual_rows:
            days.setdefault(r["d"], {"income": 0.0, "expense": 0.0})
            days[r["d"]]["income"] += round(r["inc"], 2)
            days[r["d"]]["expense"] += round(r["exp"], 2)
        return {"year": year, "month": month,
                "days": {d: {"income": round(v["income"], 2), "expense": round(v["expense"], 2)}
                         for d, v in sorted(days.items())}}


# ---------------------------------------------------------------- v0.6: configurações da loja
class SettingsIn(BaseModel):
    loyalty_n: Optional[int] = Field(default=None, ge=2, le=100)
    inactive_days: Optional[int] = Field(default=None, ge=7, le=180)
    reminder_hours: Optional[int] = Field(default=None, ge=1, le=72)
    weekly_report: Optional[bool] = None


@app.get("/settings")
def get_settings(shop_id: int = Depends(current_shop)):
    with db() as conn:
        return _settings(conn, shop_id)


@app.patch("/settings")
def update_settings(body: SettingsIn, shop_id: int = Depends(current_shop)):
    data = body.model_dump(exclude_unset=True)
    weekly = data.pop("weekly_report", None)
    with db() as conn:
        _settings(conn, shop_id)
        for k, v in data.items():
            conn.execute(f"UPDATE settings SET {k}=? WHERE shop_id=?", (v, shop_id))
        if weekly is not None:
            conn.execute("UPDATE shops SET weekly_report=? WHERE id=?", (int(weekly), shop_id))
        out = _settings(conn, shop_id)
        out["weekly_report"] = bool(conn.execute("SELECT weekly_report FROM shops WHERE id=?", (shop_id,)).fetchone()["weekly_report"])
        return out


# ---------------------------------------------------------------- v0.6: fidelidade (a cada N cortes, 1 grátis)
@app.get("/clients/{client_id}/loyalty")
def client_loyalty(client_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        cli = _get_scoped(conn, "clients", client_id, shop_id, "Cliente")
        st = _settings(conn, shop_id)
        n = st["loyalty_n"]
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE client_id=? AND shop_id=? AND status='concluido'",
            (client_id, shop_id)).fetchone()["c"]
        used = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE client_id=? AND shop_id=? AND status='concluido' AND loyalty_used=1",
            (client_id, shop_id)).fetchone()["c"]
        cycles = done // n
        progress = done % n
        return {"client": cli, "loyalty_n": n, "completed": done,
                "free_used": used, "cycles": cycles,
                "progress": progress, "to_free": n - progress,
                "has_free": progress == n - 1 and done > 0 and used < cycles}


@app.post("/appointments/{ap_id}/use-free")
def use_free_cut(ap_id: int, shop_id: int = Depends(current_shop)):
    """Marca um corte concluído como uso do benefício fidelidade (grátis)."""
    with db() as conn:
        ap = _get_scoped(conn, "appointments", ap_id, shop_id, "Agendamento")
        if ap["status"] != "concluido":
            raise HTTPException(422, "Só cortes concluídos podem usar o benefício")
        if ap["loyalty_used"]:
            raise HTTPException(422, "Este corte já usou o benefício")
        if not ap["client_id"]:
            raise HTTPException(422, "Corte de cliente avulso não participa do fidelidade")
        st = _settings(conn, shop_id)
        n = st["loyalty_n"]
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE client_id=? AND shop_id=? AND status='concluido'",
            (ap["client_id"], shop_id)).fetchone()["c"]
        used = conn.execute(
            "SELECT COUNT(*) AS c FROM appointments WHERE client_id=? AND shop_id=? AND status='concluido' AND loyalty_used=1",
            (ap["client_id"], shop_id)).fetchone()["c"]
        if used >= done // n:
            raise HTTPException(422, f"Cliente ainda não completou {n} cortes para usar o grátis")
        conn.execute("UPDATE appointments SET loyalty_used=1 WHERE id=?", (ap_id,))
        return {"ok": True, "free_cut": True}


# ---------------------------------------------------------------- v0.6: lembretes do dia (fila de amanhã)
@app.get("/reminders/tomorrow")
def reminders_tomorrow(shop_id: int = Depends(current_shop)):
    """Agendamentos de amanhã com link WhatsApp pronto para cada um."""
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT a.id, a.start, a.client_name, a.client_id, a.service_id, s.name AS service_name,
                      p.name AS professional_name
               FROM appointments a
               JOIN services s ON s.id = a.service_id
               JOIN professionals p ON p.id = a.professional_id
               WHERE a.shop_id=? AND a.status='agendado' AND a.start LIKE ?
               ORDER BY a.start""", (shop_id, tomorrow + "%")).fetchall()
        shop = conn.execute("SELECT name FROM shops WHERE id=?", (shop_id,)).fetchone()
        out = []
        for r in rows:
            phone = ""
            if r["client_id"]:
                c = conn.execute("SELECT phone FROM clients WHERE id=?", (r["client_id"],)).fetchone()
                phone = c["phone"] if c else ""
            when = r["start"][11:16]
            msg = (f"Oi {r['client_name']}! Passando pra confirmar seu horário na {shop['name']} "
                   f"amanhã ({tomorrow[8:10]}/{tomorrow[5:7]}) às {when} — {r['service_name']} com {r['professional_name']}. "
                   f"Responde SIM pra confirmar ou me avisa se precisa remarcar! 💈")
            out.append({"id": r["id"], "time": when, "client_name": r["client_name"],
                        "service_name": r["service_name"], "professional_name": r["professional_name"],
                        "phone": phone, "whatsapp_link": _wa_link(phone, msg)})
    return {"day": tomorrow, "reminders": out}


# ---------------------------------------------------------------- v0.6: reativação de inativos
@app.get("/reports/inactive")
def inactive_clients(days: Optional[int] = None, shop_id: int = Depends(current_shop)):
    """Clientes que não voltam há X dias (configurável; default do settings).
    Considera o último corte concluído — quem nunca cortou entra com 'nunca'."""
    with db() as conn:
        st = _settings(conn, shop_id)
        threshold = days or st["inactive_days"]
        cutoff = (datetime.now() - timedelta(days=threshold)).date().isoformat()
        rows = conn.execute(
            """SELECT c.id, c.name, c.phone,
                      MAX(CASE WHEN a.status='concluido' THEN a.start END) AS last_cut
               FROM clients c
               LEFT JOIN appointments a ON a.client_id = c.id
               WHERE c.shop_id=?
               GROUP BY c.id""", (shop_id,)).fetchall()
        out = []
        for r in rows:
            last = r["last_cut"]
            if last is None:
                out.append({"id": r["id"], "name": r["name"], "phone": r["phone"],
                            "last_cut": None, "days_since": None, "never": True})
                continue
            if last[:10] <= cutoff:
                d = (datetime.now().date() - datetime.fromisoformat(last[:10]).date()).days
                out.append({"id": r["id"], "name": r["name"], "phone": r["phone"],
                            "last_cut": last[:10], "days_since": d, "never": False})
        out.sort(key=lambda x: (x["days_since"] is None, -(x["days_since"] or 0)))
        shop = conn.execute("SELECT name FROM shops WHERE id=?", (shop_id,)).fetchone()
        for o in out:
            msg = (f"Oi {o['name']}! Sentimos sua falta na {shop['name']} 💈 "
                   + ("Que tal agendar um horário essa semana? " if o["never"] else
                      f"Faz {o['days_since']} dias do seu último corte — já tá na hora de renovar o visual! "))
            msg += "Responde aqui que eu te encaixo num horário bom pra você!"
            o["whatsapp_link"] = _wa_link(o["phone"], msg)
        return {"threshold_days": threshold, "count": len(out), "clients": out}


# ---------------------------------------------------------------- v0.6: bloqueios de agenda (folga/intervalo)
class BlockIn(BaseModel):
    professional_id: int
    day: str
    start_time: str = "00:00"
    end_time: str = "23:59"
    reason: str = ""


@app.post("/blocks")
def create_block(body: BlockIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "professionals", body.professional_id, shop_id, "Profissional")
        try:
            hh1, mm1 = body.start_time.split(":"); hh2, mm2 = body.end_time.split(":")
            if not (0 <= int(hh1) < 24 and 0 <= int(mm1) < 60 and 0 <= int(hh2) < 24 and 0 <= int(mm2) < 60):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Horários no formato HH:MM")
        if body.start_time >= body.end_time:
            raise HTTPException(422, "end_time deve ser depois de start_time")
        cur = conn.execute(
            "INSERT INTO blocks (shop_id, professional_id, day, start_time, end_time, reason) VALUES (?,?,?,?,?,?)",
            (shop_id, body.professional_id, body.day, body.start_time, body.end_time, body.reason))
        return {**body.model_dump(), "id": cur.lastrowid, "shop_id": shop_id}


@app.get("/blocks")
def list_blocks(day: Optional[str] = None, shop_id: int = Depends(current_shop)):
    with db() as conn:
        if day:
            rows = conn.execute("SELECT * FROM blocks WHERE shop_id=? AND day=? ORDER BY start_time", (shop_id, day)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM blocks WHERE shop_id=? AND day>=? ORDER BY day, start_time",
                                (shop_id, date.today().isoformat())).fetchall()
        return [dict(r) for r in rows]


@app.delete("/blocks/{block_id}")
def delete_block(block_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "blocks", block_id, shop_id, "Bloqueio")
        conn.execute("DELETE FROM blocks WHERE id=?", (block_id,))
    return {"ok": True}


# ---------------------------------------------------------------- v0.6: lista de espera
class WaitlistIn(BaseModel):
    client_id: int
    preferred_day: str = ""
    notes: str = ""


@app.post("/waitlist")
def add_waitlist(body: WaitlistIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "clients", body.client_id, shop_id, "Cliente")
        cur = conn.execute(
            "INSERT INTO waitlist (shop_id, client_id, preferred_day, notes, created_at) VALUES (?,?,?,?,?)",
            (shop_id, body.client_id, body.preferred_day, body.notes, _now().isoformat()))
        return {**body.model_dump(), "id": cur.lastrowid, "shop_id": shop_id, "active": 1}


@app.get("/waitlist")
def list_waitlist(shop_id: int = Depends(current_shop)):
    with db() as conn:
        rows = conn.execute(
            """SELECT w.*, c.name AS client_name, c.phone
               FROM waitlist w JOIN clients c ON c.id = w.client_id
               WHERE w.shop_id=? AND w.active=1 ORDER BY w.created_at""", (shop_id,)).fetchall()
        out = []
        for r in rows:
            o = dict(r)
            o["whatsapp_link"] = _wa_link(r["phone"],
                f"Oi {r['client_name']}! Abriu um horário na agenda — quer pegar? Responde aqui que eu reservo pra você!")
            out.append(o)
        return out


@app.delete("/waitlist/{entry_id}")
def remove_waitlist(entry_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "waitlist", entry_id, shop_id, "Entrada")
        conn.execute("UPDATE waitlist SET active=0 WHERE id=?", (entry_id,))
    return {"ok": True}


# ---------------------------------------------------------------- v0.6: clube de assinatura
class SubscriptionIn(BaseModel):
    client_id: int
    plan_name: str = "Mensal"
    cuts_per_month: int = Field(default=2, ge=1, le=31)
    price: float = Field(ge=0)


@app.post("/subscriptions")
def create_subscription(body: SubscriptionIn, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "clients", body.client_id, shop_id, "Cliente")
        cur = conn.execute(
            "INSERT INTO subscriptions (shop_id, client_id, plan_name, cuts_per_month, price, started_at) VALUES (?,?,?,?,?,?)",
            (shop_id, body.client_id, body.plan_name, body.cuts_per_month, body.price, _now().isoformat()))
        return {**body.model_dump(), "id": cur.lastrowid, "shop_id": shop_id, "active": 1}


@app.get("/subscriptions")
def list_subscriptions(shop_id: int = Depends(current_shop)):
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, c.name AS client_name FROM subscriptions s
               JOIN clients c ON c.id = s.client_id
               WHERE s.shop_id=? AND s.active=1 ORDER BY s.started_at DESC""", (shop_id,)).fetchall()
        out = []
        for r in rows:
            o = dict(r)
            month_prefix = date.today().isoformat()[:7]
            used = conn.execute(
                "SELECT COUNT(*) AS c FROM appointments WHERE client_id=? AND shop_id=? AND status='concluido' AND start LIKE ?",
                (r["client_id"], shop_id, month_prefix + "%")).fetchone()["c"]
            o["used_this_month"] = used
            o["remaining"] = max(0, r["cuts_per_month"] - used)
            o["mrr"] = r["price"]
            out.append(o)
        return out


@app.delete("/subscriptions/{sub_id}")
def cancel_subscription(sub_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        _get_scoped(conn, "subscriptions", sub_id, shop_id, "Assinatura")
        conn.execute("UPDATE subscriptions SET active=0 WHERE id=?", (sub_id,))
    return {"ok": True}


@app.get("/reports/mrr")
def mrr_report(shop_id: int = Depends(current_shop)):
    """Receita mensal recorrente das assinaturas ativas."""
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(price),0) AS mrr, COUNT(*) AS n FROM subscriptions WHERE shop_id=? AND active=1",
            (shop_id,)).fetchone()
        return {"mrr": round(row["mrr"], 2), "active_subscriptions": row["n"]}


# ---------------------------------------------------------------- agendamento online (página pública da barbearia)
@app.get("/public/{slug}")
def public_info(slug: str):
    """Dados públicos da barbearia para a página de agendamento online."""
    with db() as conn:
        shop = conn.execute("SELECT id, name FROM shops WHERE id=?", (_shop_id_from_slug(conn, slug),)).fetchone()
        if not shop:
            raise HTTPException(404, "Barbearia não encontrada")
        profs = conn.execute("SELECT id, name, role, photo FROM professionals WHERE shop_id=?", (shop["id"],)).fetchall()
        svcs = conn.execute("SELECT id, name, price, duration_min FROM services WHERE shop_id=?", (shop["id"],)).fetchall()
        return {"shop": dict(shop), "slug": slug,
                "professionals": [dict(p) for p in profs],
                "services": [dict(s) for s in svcs]}


def _shop_id_from_slug(conn, slug: str) -> int:
    """Slug = 'b{id}-{nome}' (ex.: b3-barbearia-do-ze) — imutável e sem tabela extra."""
    m = re.match(r"b(\d+)-", slug)
    if not m:
        raise HTTPException(404, "Barbearia não encontrada")
    return int(m.group(1))


class PublicBookIn(BaseModel):
    slug: str
    professional_id: int
    service_id: int
    client_name: str = Field(min_length=2)
    client_phone: str = Field(min_length=10)
    start: str


@app.post("/public/book")
def public_book(body: PublicBookIn):
    """Cliente agenda sozinho pela página pública (sem login)."""
    with db(immediate=True) as conn:
        shop_id = _shop_id_from_slug(conn, body.slug)
        shop = conn.execute("SELECT name FROM shops WHERE id=?", (shop_id,)).fetchone()
        if not shop:
            raise HTTPException(404, "Barbearia não encontrada")
        prof = conn.execute("SELECT * FROM professionals WHERE id=? AND shop_id=?", (body.professional_id, shop_id)).fetchone()
        svc = conn.execute("SELECT * FROM services WHERE id=? AND shop_id=?", (body.service_id, shop_id)).fetchone()
        if not prof or not svc:
            raise HTTPException(404, "Profissional ou serviço não encontrado")
        start = _parse_start(body.start)
        if start < datetime.now():
            raise HTTPException(422, "Escolha um horário futuro")
        _check_conflict(conn, shop_id, body.professional_id, start, svc["duration_min"])
        b = conn.execute(
            """SELECT 1 FROM blocks WHERE shop_id=? AND professional_id=? AND day=?
               AND (?) < end_time AND (?) > start_time""",
            (shop_id, body.professional_id, start.date().isoformat(),
             start.strftime("%H:%M"), (start + timedelta(minutes=svc["duration_min"])).strftime("%H:%M"))).fetchone()
        if b:
            raise HTTPException(409, "Horário indisponível")
        # cliente recorrente: acha pelo telefone, senão cria
        cli = conn.execute("SELECT id FROM clients WHERE shop_id=? AND phone=?",
                           (shop_id, body.client_phone)).fetchone()
        client_id = cli["id"] if cli else None
        if not client_id:
            cur = conn.execute("INSERT INTO clients (shop_id, name, phone) VALUES (?,?,?)",
                               (shop_id, body.client_name.strip(), body.client_phone))
            client_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO appointments (shop_id, professional_id, service_id, client_id, client_name, start, duration_min, price, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (shop_id, body.professional_id, body.service_id, client_id, body.client_name.strip(),
             start.isoformat(), svc["duration_min"], svc["price"], "agendado", _now().isoformat()))
        return {"ok": True, "appointment_id": cur.lastrowid,
                "shop_name": shop["name"], "when": start.strftime("%d/%m às %H:%M"),
                "service_name": svc["name"]}


@app.get("/public/{slug}/slots")
def public_slots(slug: str, day: str, professional_id: int):
    """Horários livres do profissional no dia (para o cliente escolher)."""
    with db() as conn:
        shop_id = _shop_id_from_slug(conn, slug)
        prof = conn.execute("SELECT id FROM professionals WHERE id=? AND shop_id=?", (professional_id, shop_id)).fetchone()
        if not prof:
            raise HTTPException(404, "Profissional não encontrado")
        busy = []
        rows = conn.execute(
            "SELECT start, duration_min FROM appointments WHERE shop_id=? AND professional_id=? AND start LIKE ? AND status!='cancelado'",
            (shop_id, professional_id, day + "%")).fetchall()
        for r in rows:
            s = datetime.fromisoformat(r["start"])
            busy.append((s, s + timedelta(minutes=r["duration_min"])))
        blocks = conn.execute(
            "SELECT start_time, end_time FROM blocks WHERE shop_id=? AND professional_id=? AND day=?",
            (shop_id, professional_id, day)).fetchall()
        # grade de 30 min, 9h às 20h (padrão do nicho)
        slots = []
        base = datetime.fromisoformat(day + "T09:00")
        for i in range(22):   # 9h → 19h30
            t = base + timedelta(minutes=30 * i)
            t_end = t + timedelta(minutes=30)
            if t < datetime.now():
                continue
            t_str = t.strftime("%H:%M")
            conflict = any(s < t_end and e > t for s, e in busy)
            blocked = any(b["start_time"] < t_end.strftime("%H:%M") and b["end_time"] > t_str for b in blocks)
            slots.append({"time": t_str, "free": not conflict and not blocked})
        return {"day": day, "slots": slots}


# ---------------------------------------------------------------- v0.8: aniversariantes do mês
@app.get("/reports/birthdays")
def birthdays(month: Optional[int] = None, shop_id: int = Depends(current_shop)):
    """Aniversariantes do mês (default: mês atual) com Zap pronto."""
    m = month or date.today().month
    if not (1 <= m <= 12):
        raise HTTPException(422, "month deve ser 1-12")
    with db() as conn:
        rows = conn.execute(
            """SELECT id, name, phone, birthday FROM clients
               WHERE shop_id=? AND birthday LIKE ? ORDER BY birthday""",
            (shop_id, f"____-{m:02d}-%")).fetchall()
        shop = conn.execute("SELECT name FROM shops WHERE id=?", (shop_id,)).fetchone()
        out = []
        for r in rows:
            b = r["birthday"]
            day = b[8:10] if len(b) == 10 else b[3:5]
            msg = (f"Oi {r['name']}! 🎉 A equipe da {shop['name']} deseja um FELIZ ANIVERSÁRIO! "
                   f"Passe aqui essa semana pra comemorar — seu corte tem 20% OFF de presente. 💈🎂")
            out.append({"id": r["id"], "name": r["name"], "phone": r["phone"],
                        "birthday": b, "day": day, "whatsapp_link": _wa_link(r["phone"], msg)})
        return {"month": m, "count": len(out), "clients": out}


# ---------------------------------------------------------------- v0.8: foto do profissional
class PhotoIn(BaseModel):
    photo: str = ""     # data URL (data:image/jpeg;base64,...) — pequena, comprimida no frontend


@app.patch("/professionals/{prof_id}/photo")
def set_professional_photo(prof_id: int, body: PhotoIn, shop_id: int = Depends(current_shop)):
    if len(body.photo) > 300_000:   # ~300KB — protege o banco
        raise HTTPException(422, "Foto muito grande (máx. ~300KB)")
    if body.photo and not body.photo.startswith("data:image/"):
        raise HTTPException(422, "Formato inválido — envie uma imagem")
    with db() as conn:
        _get_scoped(conn, "professionals", prof_id, shop_id, "Profissional")
        conn.execute("UPDATE professionals SET photo=? WHERE id=?", (body.photo, prof_id))
        return {"ok": True, "photo": body.photo[:50] + ("..." if len(body.photo) > 50 else "")}


@app.get("/professionals/{prof_id}/photo")
def get_professional_photo(prof_id: int, shop_id: int = Depends(current_shop)):
    with db() as conn:
        row = conn.execute("SELECT photo FROM professionals WHERE id=? AND shop_id=?", (prof_id, shop_id)).fetchone()
    if not row:
        raise HTTPException(404, "Profissional não encontrado")
    return {"photo": row["photo"]}


# ---------------------------------------------------------------- v0.8: relatório semanal
@app.get("/reports/weekly")
def weekly_report(shop_id: int = Depends(current_shop)):
    """Resumo dos últimos 7 dias: receita, cortes, faltas, ticket médio, top profissional."""
    with db() as conn:
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN status='concluido' THEN price END),0) AS revenue,
                 SUM(CASE WHEN status='concluido' THEN 1 END) AS cuts,
                 SUM(CASE WHEN status='faltou' THEN 1 END) AS no_shows,
                 SUM(CASE WHEN status='agendado' THEN 1 END) AS upcoming,
                 COUNT(*) AS total
               FROM appointments WHERE shop_id=? AND start>=?""",
            (shop_id, week_ago)).fetchone()
        top = conn.execute(
            """SELECT p.name, COUNT(*) AS cuts, COALESCE(SUM(a.price),0) AS revenue
               FROM appointments a JOIN professionals p ON p.id=a.professional_id
               WHERE a.shop_id=? AND a.status='concluido' AND a.start>=?
               GROUP BY p.id ORDER BY cuts DESC LIMIT 1""",
            (shop_id, week_ago)).fetchone()
        new_clients = conn.execute(
            "SELECT COUNT(*) AS n FROM clients WHERE shop_id=? AND created_at>=?",
            (shop_id, week_ago)).fetchone()["n"] if _col_exists(conn, "clients", "created_at") else 0
        revenue = round(row["revenue"], 2)
        cuts = row["cuts"] or 0
        return {
            "period": "últimos 7 dias",
            "revenue": revenue,
            "cuts": cuts,
            "no_shows": row["no_shows"] or 0,
            "upcoming": row["upcoming"] or 0,
            "avg_ticket": round(revenue / cuts, 2) if cuts else 0,
            "top_professional": dict(top) if top else None,
            "new_clients": new_clients,
        }


def _col_exists(conn, table: str, col: str) -> bool:
    return col in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


@app.get("/reports/weekly/message")
def weekly_report_message(shop_id: int = Depends(current_shop)):
    """Mensagem pronta do relatório semanal para enviar no WhatsApp do dono."""
    rep = weekly_report(shop_id)
    lines = [f"📊 *Resumo da semana* ({rep['period']})", ""]
    lines.append(f"💰 Receita: *{fmt_brl(rep['revenue'])}*")
    lines.append(f"✂️ Cortes concluídos: *{rep['cuts']}*")
    if rep["no_shows"]:
        lines.append(f"⚠️ Faltas: *{rep['no_shows']}*")
    if rep["avg_ticket"]:
        lines.append(f"📈 Ticket médio: *{fmt_brl(rep['avg_ticket'])}*")
    if rep["top_professional"]:
        t = rep["top_professional"]
        lines.append(f"🏆 Destaque: *{t['name']}* — {t['cuts']} cortes, {fmt_brl(t['revenue'])}")
    if rep["new_clients"]:
        lines.append(f"🆕 Clientes novos: *{rep['new_clients']}*")
    if rep["upcoming"]:
        lines.append(f"📅 Agendados futuros: *{rep['upcoming']}*")
    return {"message": "\n".join(lines), "report": rep}


def fmt_brl(n: float) -> str:
    s = f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


# ---------------------------------------------------------------- v0.8: envio automático do relatório (cron interno)
import threading
import time as _time

_report_lock = threading.Lock()


def _weekly_report_worker():
    """Todo minuto verifica se é segunda 8h (horário local) e envia o relatório das lojas ativas."""
    while True:
        try:
            now = datetime.now()
            if now.weekday() == 0 and now.hour == 8 and _report_lock.acquire(blocking=False):
                try:
                    with db() as conn:
                        shops = conn.execute(
                            "SELECT id, name, owner_email, weekly_report FROM shops WHERE active=1 AND weekly_report=1").fetchall()
                    for s in shops:
                        with db() as conn:
                            last = conn.execute("SELECT last_report_sent FROM shops WHERE id=?", (s["id"],)).fetchone()
                            if last and last["last_report_sent"] and last["last_report_sent"][:10] == now.date().isoformat():
                                continue   # já enviou hoje
                        rep_msg = _report_message_for(s["id"])
                        sent = _send_email(s["owner_email"], f"BarberHub — resumo da semana ({s['name']})", rep_msg)
                        with db() as conn:
                            conn.execute("UPDATE shops SET last_report_sent=? WHERE id=?", (now.isoformat(), s["id"]))
                        print(f"[RELATÓRIO] loja {s['id']} ({s['name']}): {'email enviado' if sent else 'email indisponível (SMTP não configurado)'}")
                finally:
                    _report_lock.release()
        except Exception as e:
            print(f"[RELATÓRIO ERRO] {e}")
        _time.sleep(60)


def _report_message_for(shop_id: int) -> str:
    with db() as conn:
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        row = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='concluido' THEN price END),0) AS revenue,
                      SUM(CASE WHEN status='concluido' THEN 1 END) AS cuts,
                      SUM(CASE WHEN status='faltou' THEN 1 END) AS no_shows
               FROM appointments WHERE shop_id=? AND start>=?""", (shop_id, week_ago)).fetchone()
        top = conn.execute(
            """SELECT p.name, COUNT(*) AS cuts FROM appointments a JOIN professionals p ON p.id=a.professional_id
               WHERE a.shop_id=? AND a.status='concluido' AND a.start>=?
               GROUP BY p.id ORDER BY cuts DESC LIMIT 1""", (shop_id, week_ago)).fetchone()
    lines = [f"📊 Resumo da semana — {row['cuts'] or 0} cortes concluídos",
             f"💰 Receita: {fmt_brl(round(row['revenue'], 2))}"]
    if row["no_shows"]:
        lines.append(f"⚠️ Faltas: {row['no_shows']}")
    if top:
        lines.append(f"🏆 Destaque: {top['name']} ({top['cuts']} cortes)")
    return "\n".join(lines)


_report_thread = threading.Thread(target=_weekly_report_worker, daemon=True)
_report_thread.start()


# ---------------------------------------------------------------- v0.8: comanda/OS em PDF (opcional)
@app.get("/appointments/{ap_id}/receipt")
def appointment_receipt(ap_id: int, shop_id: int = Depends(current_shop)):
    """Gera comanda/ordem de serviço em PDF do agendamento concluído.
    Uso: dono imprime ou envia ao cliente como comprovante formal do serviço,
    com itens, valores, profissional e regras de comissão aplicadas.
    Desligada por padrão — ativar com env var RECEIPTS_ENABLED=1."""
    if os.environ.get("RECEIPTS_ENABLED", "0") != "1":
        raise HTTPException(404, "Comandas desativadas — ative com RECEIPTS_ENABLED=1 nas variáveis de ambiente")
    from reportlab.lib.pagesizes import A6
    from reportlab.pdfgen import canvas as _canvas
    import io
    with db() as conn:
        ap = _get_scoped(conn, "appointments", ap_id, shop_id, "Agendamento")
        prof = conn.execute("SELECT name FROM professionals WHERE id=?", (ap["professional_id"],)).fetchone()
        svc = conn.execute("SELECT name FROM services WHERE id=?", (ap["service_id"],)).fetchone()
        shop = conn.execute("SELECT name FROM shops WHERE id=?", (shop_id,)).fetchone()
        cli = conn.execute("SELECT phone FROM clients WHERE id=?", (ap["client_id"],)).fetchone() if ap["client_id"] else None
        rule = _commission_for(conn, shop_id, ap["professional_id"], ap["service_id"], ap["price"])
    buf = io.BytesIO()
    c = _canvas.Canvas(buf, pagesize=A6)
    w, h = A6
    y = h - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, y, shop["name"][:40]); y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(30, y, f"Comanda #{ap['id']:06d}"); y -= 20
    c.line(30, y, w-30, y); y -= 16
    c.setFont("Helvetica", 10)
    c.drawString(30, y, f"Cliente: {ap['client_name'][:30]}"); y -= 14
    if cli and cli["phone"]:
        c.drawString(30, y, f"WhatsApp: {cli['phone']}"); y -= 14
    c.drawString(30, y, f"Profissional: {prof['name']}"); y -= 14
    c.drawString(30, y, f"Data: {ap['start'][:16].replace('T', ' ')}"); y -= 20
    c.line(30, y, w-30, y); y -= 16
    c.setFont("Helvetica-Bold", 10)
    c.drawString(30, y, "Serviço"); c.drawString(w-90, y, "Valor"); y -= 14
    c.setFont("Helvetica", 10)
    c.drawString(30, y, svc["name"][:30])
    c.drawRightString(w-30, y, f"R$ {ap['price']:.2f}"); y -= 20
    c.line(30, y, w-30, y); y -= 16
    c.setFont("Helvetica-Bold", 12)
    c.drawString(30, y, "TOTAL")
    c.drawRightString(w-30, y, f"R$ {ap['price']:.2f}"); y -= 24
    c.setFont("Helvetica", 8)
    c.drawString(30, y, f"Comissão do profissional: {fmt_brl(rule['commission'])} ({rule['kind']})"); y -= 12
    c.drawString(30, y, "Obrigado pela preferência!")
    c.showPage()
    c.save()
    buf.seek(0)
    from fastapi.responses import Response
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=comanda-{ap_id}.pdf"})


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(_FRONTEND)


@app.get("/book")
def book_page():
    return FileResponse(_BOOK_PAGE)


@app.get("/reset")
def reset_page():
    return FileResponse(_FRONTEND)
