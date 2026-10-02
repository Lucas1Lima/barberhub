"""Testes v0.8 — aniversariantes, foto do profissional, relatório semanal."""
from __future__ import annotations
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["BARBERHUB_DB"] = os.path.join(tempfile.mkdtemp(), "v08.db")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
failures = []

def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond: failures.append(name)

def H(t): return {"Authorization": f"Bearer {t}"}

TOK = client.post("/auth/register", json={"email":"a@b.com","password":"senha123","shop_name":"Barbearia Top"}).json()["token"]
prof = client.post("/professionals", headers=H(TOK), json={"name":"João"}).json()
prof2 = client.post("/professionals", headers=H(TOK), json={"name":"Pedro"}).json()
svc = client.post("/services", headers=H(TOK), json={"name":"Corte","price":50.0,"duration_min":30}).json()

# aniversário: mês atual e outro mês
THIS_M = datetime.now().month
b_this = f"1990-{THIS_M:02d}-15"
other_m = 1 if THIS_M != 1 else 2
cli1 = client.post("/clients", headers=H(TOK), json={"name":"Carlos","phone":"13991234567","birthday":b_this}).json()
cli2 = client.post("/clients", headers=H(TOK), json={"name":"Duda","phone":"13998887777","birthday":f"1995-{other_m:02d}-10"}).json()

r = client.get("/reports/birthdays", headers=H(TOK)).json()
check("aniversariantes do mês atual", r["month"]==THIS_M and r["count"]==1 and r["clients"][0]["name"]=="Carlos", str(r)[:120])
check("Zap de parabéns com 20% off", "20%25%20OFF" in r["clients"][0]["whatsapp_link"] or "20% OFF" in r["clients"][0]["whatsapp_link"], r["clients"][0]["whatsapp_link"][:80])
r = client.get(f"/reports/birthdays?month={other_m}", headers=H(TOK)).json()
check("outro mês via query", r["count"]==1 and r["clients"][0]["name"]=="Duda")
r = client.get("/reports/birthdays?month=13", headers=H(TOK))
check("mês inválido 422", r.status_code==422, f"got {r.status_code}")

# PATCH birthday (edição)
r = client.patch(f"/clients/{cli1['id']}", headers=H(TOK), json={"birthday": f"1990-{THIS_M:02d}-20"})
check("editar aniversário", r.status_code==200 and r.json()["birthday"]==f"1990-{THIS_M:02d}-20")

# foto do profissional
tiny_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
r = client.patch(f"/professionals/{prof['id']}/photo", headers=H(TOK), json={"photo": tiny_png})
check("salva foto (data URL)", r.status_code==200, str(r.json()))
r = client.get(f"/professionals/{prof['id']}/photo", headers=H(TOK)).json()
check("lê foto de volta", r["photo"]==tiny_png)
r = client.patch(f"/professionals/{prof['id']}/photo", headers=H(TOK), json={"photo": "javascript:alert(1)"})
check("formato não-imagem rejeitado (422)", r.status_code==422, f"got {r.status_code}")
r = client.patch(f"/professionals/{prof['id']}/photo", headers=H(TOK), json={"photo": "data:image/png;base64," + "A"*400000})
check("foto gigante rejeitada (422)", r.status_code==422, f"got {r.status_code}")
TOK2 = client.post("/auth/register", json={"email":"z@w.com","password":"senha123"}).json()["token"]
r = client.get(f"/professionals/{prof['id']}/photo", headers=H(TOK2))
check("outra loja não lê foto (404)", r.status_code==404, f"got {r.status_code}")

# relatório semanal
TODAY = datetime.now().date().isoformat()
ap1 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc["id"],"client_id":cli1["id"],"start":f"{TODAY}T09:00"}).json()
ap2 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof2["id"],"service_id":svc["id"],"client_id":cli2["id"],"start":f"{TODAY}T10:00"}).json()
ap3 = client.post("/appointments", headers=H(TOK), json={"professional_id":prof["id"],"service_id":svc["id"],"client_name":"Fujão","start":f"{TODAY}T11:00"}).json()
client.patch(f"/appointments/{ap1['id']}/status", headers=H(TOK), json={"status":"concluido"})
client.patch(f"/appointments/{ap2['id']}/status", headers=H(TOK), json={"status":"concluido"})
client.patch(f"/appointments/{ap3['id']}/status", headers=H(TOK), json={"status":"faltou"})

r = client.get("/reports/weekly", headers=H(TOK)).json()
check("relatório: receita 100", r["revenue"]==100.0, str(r))
check("relatório: 2 cortes, 1 falta", r["cuts"]==2 and r["no_shows"]==1, str(r))
check("ticket médio 50", r["avg_ticket"]==50.0)
check("destaque: um profissional com 1 corte", r["top_professional"]["cuts"]==1, str(r["top_professional"]))

r = client.get("/reports/weekly/message", headers=H(TOK)).json()
check("mensagem pronta com números", "R$ 100,00" in r["message"] and "2" in r["message"] and "Faltas" in r["message"], r["message"][:150])

# settings: weekly_report on/off
r = client.patch("/settings", headers=H(TOK), json={"weekly_report": False}).json()
check("desliga relatório semanal", r["weekly_report"]==False, str(r))
r = client.patch("/settings", headers=H(TOK), json={"weekly_report": True}).json()
check("liga relatório semanal", r["weekly_report"]==True)

print()
if failures:
    print(f"❌ {len(failures)} falha(s): {failures}"); sys.exit(1)
print("✅ v0.8 OK")

# ---------------- comanda PDF (opcional via env)
r = client.get(f"/appointments/{ap1['id']}/receipt", headers=H(TOK))
check("comanda desativada por padrão (404)", r.status_code==404, f"got {r.status_code}")
os.environ["RECEIPTS_ENABLED"] = "1"
import importlib, main
importlib.reload(main)
client2 = TestClient(main.app)
r = client2.get(f"/appointments/{ap1['id']}/receipt", headers=H(TOK))
check("comanda PDF quando ativada", r.status_code==200 and r.headers.get("content-type")=="application/pdf" and len(r.content)>1000, f"got {r.status_code}")
os.environ["RECEIPTS_ENABLED"] = "0"
