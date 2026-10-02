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
    created_at TEXT NOT NULL
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
    phone TEXT NOT NULL DEFAULT ''
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
"""


def _migrate():
    with db() as conn:
        conn.executescript(SCHEMA)
        # migração incremental: data de conclusão (caixa fecha pelo dia da conclusão)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(appointments)").fetchall()]
        if "concluded_at" not in cols:
            conn.execute("ALTER TABLE appointments ADD COLUMN concluded_at TEXT")


_migrate()


def _now() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------- auth
def _make_token(shop_id: int) -> str:
    payload = {"shop_id": shop_id, "exp": datetime.utcnow() + timedelta(hours=_TOKEN_HOURS)}
    return jwt.encode(payload, _SECRET, algorithm="HS256")


def current_shop(authorization: str = Header(default="")) -> int:
    """Dependency: extrai shop_id do token Bearer."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Faça login para continuar")
    token = authorization[7:]
    try:
        data = jwt.decode(token, _SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Sessão expirada — faça login novamente")
    return int(data["shop_id"])


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
    return {"shop_id": shop_id, "token": _make_token(shop_id), "shop_name": body.shop_name or "Minha Barbearia"}


@app.post("/auth/login")
def login(body: AuthIn):
    email = body.email.strip().lower()
    with db() as conn:
        row = conn.execute("SELECT id, password_hash FROM shops WHERE owner_email=?", (email,)).fetchone()
    if not row or not _pwd.verify(body.password, row["password_hash"]):
        raise HTTPException(401, "Email ou senha incorretos")
    return {"shop_id": row["id"], "token": _make_token(row["id"])}


@app.get("/auth/me")
def me(shop_id: int = Depends(current_shop)):
    with db() as conn:
        row = conn.execute("SELECT id, name, owner_email FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Conta não encontrada")
    return dict(row)


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


class ClientPatch(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None


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
        cur = conn.execute("INSERT INTO clients (shop_id, name, phone) VALUES (?,?,?)",
                           (shop_id, body.name, body.phone))
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


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(_FRONTEND)
