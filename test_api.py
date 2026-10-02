"""Testes core do BarberHub — agenda, comissão, caixa, isolamento.
Roda com: python test_api.py"""
from __future__ import annotations
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["BARBERHUB_DB"] = os.path.join(tempfile.mkdtemp(), "core.db")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
failures = []

def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond: failures.append(name)

def H(t): return {"Authorization": f"Bearer {t}"}

TOK = client.post("/auth/register", json={"email":"a@b.com","password":"senha123","shop_name":"T"}).json()["token"]
TOK2 = client.post("/auth/register", json={"email":"c@d.com","password":"senha123","shop_name":"T2"}).json()["token"]

prof = client.post("/professionals", headers=H(TOK), json={"name":"João"}).json()
prof2 = client.post("/professionals", headers=H(TOK), json={"name":"Maria","chair_rental":True,"fixed_fee":20.0}).json()
svc = client.post("/services", headers=H(TOK), json={"name":"Corte","price":50.0,"duration_min":30}).json()
svc2 = client.post("/services", headers=H(TOK), json={"name":"Barba","price":35.0,"duration_min":20}).json()
cli = client.post("/clients", headers=H(TOK), json={"name":"Carlos","phone":"13991234567"}).json()

TODAY = datetime.now().date().isoformat()
ap1 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc["id"],"client_id":cli["id"],"start":f"{TODAY}T09:00"}).json()
check("cria agendamento", ap1.get("id") is not None)
check("client_name do cadastro", ap1["client_name"]=="Carlos")
r = client.post("/appointments", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc["id"],"start":f"{TODAY}T09:00"})
check("conflito bloqueado (409)", r.status_code==409, f"got {r.status_code}")
r = client.post("/appointments", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc["id"],"start":f"{TODAY}T14:00","status":"concluido"})
check("POST força 'agendado' (422)", r.status_code==422, f"got {r.status_code}")
r = client.get(f"/appointments/{ap1['id']}/reminder", headers=H(TOK)).json()
check("WhatsApp com DDI e encode", "wa.me/5513991234567" in r["whatsapp_link"] and "%20" in r["whatsapp_link"])

lst = client.get("/appointments", headers=H(TOK)).json()
row = next(a for a in lst if a["id"]==ap1["id"])
check("listagem com JOIN (nomes)", row["service_name"]=="Corte" and row["professional_name"]=="João")

client.post("/commission-rules", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc["id"],"kind":"percent","value":40})
client.post("/commission-rules", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc2["id"],"kind":"fixed","value":15})
ap2 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc2["id"],"client_id":cli["id"],"start":f"{TODAY}T10:00"}).json()
ap3 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof2["id"],"service_id":svc["id"],"client_name":"Avulso","start":f"{TODAY}T11:00"}).json()
ap4 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof2["id"],"service_id":svc["id"],"client_name":"A2","start":f"{TODAY}T12:00"}).json()
client.patch(f"/appointments/{ap1['id']}/status", headers=H(TOK), json={"status":"concluido"})
client.patch(f"/appointments/{ap2['id']}/status", headers=H(TOK), json={"status":"concluido"})
client.patch(f"/appointments/{ap3['id']}/status", headers=H(TOK), json={"status":"faltou"})
client.patch(f"/appointments/{ap4['id']}/status", headers=H(TOK), json={"status":"concluido"})

rep = client.get(f"/reports/commissions?day={TODAY}", headers=H(TOK)).json()
joao = next(p for p in rep["professionals"] if p["professional_name"]=="João")
maria = next(p for p in rep["professionals"] if p["professional_name"]=="Maria")
check("comissão João = 35 (40% + fixo)", abs(joao["commission"]-35.0)<0.01, str(joao))
check("comissão Maria = 30 (cadeira 1×/dia)", abs(maria["commission"]-30.0)<0.01, str(maria))

client.post("/cash-entries", headers=H(TOK), json={"day":TODAY,"kind":"expense","description":"Café","amount":10.0})
cash = client.get(f"/reports/daily-cash?day={TODAY}", headers=H(TOK)).json()
check("caixa receita 135 / net 125", cash["services_revenue"]==135.0 and cash["net"]==125.0, str(cash))

YDAY = (datetime.now().date()-timedelta(days=1)).isoformat()
ap5 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc["id"],"client_name":"Ontem","start":f"{YDAY}T09:00"}).json()
client.patch(f"/appointments/{ap5['id']}/status", headers=H(TOK), json={"status":"concluido"})
cash = client.get(f"/reports/daily-cash?day={TODAY}", headers=H(TOK)).json()
cash_y = client.get(f"/reports/daily-cash?day={YDAY}", headers=H(TOK)).json()
check("caixa por data de conclusão", cash_y["services_revenue"]==0.0 and cash["services_revenue"]==185.0, f"ontem={cash_y[chr(39)+chr(39)] if False else cash_y}")

r = client.get("/professionals", headers=H(TOK2))
check("isolamento entre lojas", r.json()==[])
r = client.patch("/appointments/1/status", headers=H(TOK2), json={"status":"cancelado"})
check("loja 2 não altera loja 1 (404)", r.status_code==404, f"got {r.status_code}")
r = client.get("/professionals")
check("sem token bloqueado (401)", r.status_code==401, f"got {r.status_code}")

hist = client.get(f"/clients/{cli['id']}/history", headers=H(TOK)).json()
check("histórico do cliente = 2", len(hist["appointments"])==2, str(hist))

print()
if failures:
    print(f"❌ {len(failures)} falha(s): {failures}"); sys.exit(1)
print("✅ Core OK")
