"""Testes v0.7 — agendamento online público + migração Postgres (estrutura)."""
from __future__ import annotations
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["BARBERHUB_DB"] = os.path.join(tempfile.mkdtemp(), "v07.db")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
failures = []

def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond: failures.append(name)

def H(t): return {"Authorization": f"Bearer {t}"}

TOK = client.post("/auth/register", json={"email":"a@b.com","password":"senha123","shop_name":"Barbearia do Zé"}).json()["token"]
me = client.get("/auth/me", headers=H(TOK)).json()
SLUG = me["slug"]
check("slug gerado do nome", SLUG.startswith("b1-") and "ze" in SLUG, SLUG)

prof = client.post("/professionals", headers=H(TOK), json={"name":"João"}).json()
svc = client.post("/services", headers=H(TOK), json={"name":"Corte","price":50.0,"duration_min":30}).json()

# página pública acessível sem login
r = client.get(f"/public/{SLUG}")
check("página pública sem login", r.status_code==200 and r.json()["shop"]["name"]=="Barbearia do Zé", str(r.json())[:100])
check("pública lista serviços e profissionais", len(r.json()["services"])==1 and len(r.json()["professionals"])==1)
r = client.get("/public/b999-x")
check("slug inexistente 404", r.status_code==404, f"got {r.status_code}")

# slots
TOM = (datetime.now()+timedelta(days=1)).date().isoformat()
r = client.get(f"/public/{SLUG}/slots?day={TOM}&professional_id={prof['id']}")
slots = r.json()["slots"]
check("slots gerados (22 de 30min)", len(slots)==22, f"got {len(slots)}")
check("todos livres no dia vazio", all(s["free"] for s in slots))

# agendamento público pelo cliente
r = client.post("/public/book", json={"slug":SLUG,"professional_id":prof["id"],"service_id":svc["id"],
                                       "client_name":"Maria Cliente","client_phone":"13998765432","start":f"{TOM}T14:00"})
check("cliente agenda sozinho", r.status_code==200 and r.json()["ok"], str(r.json()))
r = client.post("/public/book", json={"slug":SLUG,"professional_id":prof["id"],"service_id":svc["id"],
                                       "client_name":"Outro","client_phone":"13990000000","start":f"{TOM}T14:00"})
check("conflito bloqueado na página pública (409)", r.status_code==409, f"got {r.status_code}")
r = client.post("/public/book", json={"slug":SLUG,"professional_id":prof["id"],"service_id":svc["id"],
                                       "client_name":"X","client_phone":"13990000000","start":"2020-01-01T10:00"})
check("data passada rejeitada (422)", r.status_code==422, f"got {r.status_code}")

# cliente recorrente: mesmo telefone = mesmo cadastro
r = client.post("/public/book", json={"slug":SLUG,"professional_id":prof["id"],"service_id":svc["id"],
                                       "client_name":"Maria Cliente","client_phone":"13998765432","start":f"{TOM}T16:00"})
check("2º agendamento mesmo telefone ok", r.status_code==200)
import sqlite3
conn = sqlite3.connect(os.environ["BARBERHUB_DB"])
n_maria = conn.execute("SELECT COUNT(*) FROM clients WHERE phone='13998765432'").fetchone()[0]
check("cliente recorrente não duplica cadastro", n_maria==1, f"got {n_maria}")

# slots refletem ocupação
r = client.get(f"/public/{SLUG}/slots?day={TOM}&professional_id={prof['id']}")
s14 = next(s for s in r.json()["slots"] if s["time"]=="14:00")
s15 = next(s for s in r.json()["slots"] if s["time"]=="15:00")
check("slot 14:00 ocupado", not s14["free"])
s1430 = next(s for s in r.json()["slots"] if s["time"]=="14:30")
check("slot 14:30 livre (termina exatamente quando anterior acaba)", s1430["free"], str(s1430))
s16 = next(s for s in r.json()["slots"] if s["time"]=="16:00")
check("slot 16:00 ocupado (2º agendamento)", not s16["free"])

# bloqueio reflete na página pública
client.post("/blocks", headers=H(TOK), json={"professional_id":prof["id"],"day":TOM,"start_time":"10:00","end_time":"11:00"})
r = client.get(f"/public/{SLUG}/slots?day={TOM}&professional_id={prof['id']}")
s10 = next(s for s in r.json()["slots"] if s["time"]=="10:00")
check("bloqueio aparece como indisponível", not s10["free"])

# agendamento aparece na agenda do dono
aps = client.get(f"/appointments?day={TOM}", headers=H(TOK)).json()
check("dono vê agendamentos feitos online", len(aps)==2 and aps[0]["client_name"]=="Maria Cliente", str(aps)[:120])

# /book serve a página
r = client.get("/book")
check("rota /book serve página pública", r.status_code==200 and b"Agende seu hor" in r.content)

print()
if failures:
    print(f"❌ {len(failures)} falha(s): {failures}"); sys.exit(1)
print("✅ v0.7 OK")
