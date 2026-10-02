"""Testes v0.6 — fidelidade, lembretes, reativação, bloqueios, lista de espera, assinatura, settings."""
from __future__ import annotations
import os, sys, tempfile
from datetime import datetime, timedelta

os.environ["BARBERHUB_DB"] = os.path.join(tempfile.mkdtemp(), "v06.db")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
failures = []

def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond: failures.append(name)

def H(t): return {"Authorization": f"Bearer {t}"}

TOK = client.post("/auth/register", json={"email":"a@b.com","password":"senha123","shop_name":"T"}).json()["token"]
prof = client.post("/professionals", headers=H(TOK), json={"name":"João"}).json()
svc = client.post("/services", headers=H(TOK), json={"name":"Corte","price":50.0,"duration_min":30}).json()
cli = client.post("/clients", headers=H(TOK), json={"name":"Carlos","phone":"13991234567"}).json()
cli2 = client.post("/clients", headers=H(TOK), json={"name":"Duda","phone":"13998887777"}).json()

# ---------------- settings
r = client.get("/settings", headers=H(TOK))
check("settings default (10, 30, 24)", r.json()["loyalty_n"]==10 and r.json()["inactive_days"]==30 and r.json()["reminder_hours"]==24, str(r.json()))
r = client.patch("/settings", headers=H(TOK), json={"loyalty_n":3, "inactive_days":21})
check("settings atualizável", r.json()["loyalty_n"]==3 and r.json()["inactive_days"]==21, str(r.json()))

# ---------------- fidelidade (a cada 3 cortes, 1 grátis)
TODAY = datetime.now().date().isoformat()
aps = []
for h in ("09:00","10:00","11:00","12:00","13:00","14:00"):
    aps.append(client.post("/appointments", headers=H(TOK), json={
        "professional_id":prof["id"],"service_id":svc["id"],"client_id":cli["id"],"start":f"{TODAY}T{h}"}).json())
for ap in aps[:3]:
    client.patch(f"/appointments/{ap['id']}/status", headers=H(TOK), json={"status":"concluido"})
r = client.get(f"/clients/{cli['id']}/loyalty", headers=H(TOK)).json()
check("fidelidade: 3 cortes = 1 ciclo completo", r["completed"]==3 and r["cycles"]==1 and r["progress"]==0, str(r))
r = client.post(f"/appointments/{aps[0]['id']}/use-free", headers=H(TOK))
check("usa corte grátis", r.status_code==200 and r.json()["free_cut"], str(r.json()))
r = client.post(f"/appointments/{aps[1]['id']}/use-free", headers=H(TOK))
check("não pode usar 2 grátis no mesmo ciclo (422)", r.status_code==422, f"got {r.status_code}")
# mais 3 cortes → novo ciclo
for ap in aps[3:6]:
    client.patch(f"/appointments/{ap['id']}/status", headers=H(TOK), json={"status":"concluido"})
r = client.post(f"/appointments/{aps[3]['id']}/use-free", headers=H(TOK))
check("novo ciclo libera novo grátis", r.status_code==200, str(r.json()))

# ---------------- lembretes de amanhã
TOM = (datetime.now()+timedelta(days=1)).date().isoformat()
ap_tom = client.post("/appointments", headers=H(TOK), json={
    "professional_id":prof["id"],"service_id":svc["id"],"client_id":cli["id"],"start":f"{TOM}T15:00"}).json()
r = client.get("/reminders/tomorrow", headers=H(TOK)).json()
check("fila de lembretes de amanhã", r["day"]==TOM and len(r["reminders"])==1, str(r)[:120])
rem = r["reminders"][0]
check("lembrete com link wa.me e mensagem", "wa.me/5513991234567" in rem["whatsapp_link"] and "confirmar" in rem["whatsapp_link"], rem["whatsapp_link"][:80])

# ---------------- reativação de inativos
# Carlos cortou hoje (não inativo); Duda nunca cortou
r = client.get("/reports/inactive", headers=H(TOK)).json()
check("inativos: threshold do settings (21)", r["threshold_days"]==21)
check("Duda (nunca cortou) aparece como inativa", any(c["id"]==cli2["id"] and c["never"] for c in r["clients"]), str(r["clients"])[:150])
check("Carlos (cortou hoje) NÃO aparece", not any(c["id"]==cli["id"] for c in r["clients"]))
# cliente com corte há 40 dias → inativo (Carlos cortou hoje, então usa cliente novo)
OLD = (datetime.now()-timedelta(days=40)).date().isoformat()
cli3 = client.post("/clients", headers=H(TOK), json={"name":"Rafa","phone":"13995556666"}).json()
ap_old = client.post("/appointments", headers=H(TOK), json={
    "professional_id":prof["id"],"service_id":svc["id"],"client_id":cli3["id"],"start":f"{OLD}T08:00"}).json()
client.patch(f"/appointments/{ap_old['id']}/status", headers=H(TOK), json={"status":"concluido"})
r = client.get("/reports/inactive", headers=H(TOK)).json()
carlos = next((c for c in r["clients"] if c["id"]==cli3["id"]), None)
check("Carlos com 40d sem cortar aparece", carlos is not None and carlos["days_since"]==40, str(carlos))
check("link de reativação com mensagem personalizada", carlos and "40%20dias" in carlos["whatsapp_link"], carlos["whatsapp_link"][:100] if carlos else "")
r = client.get("/reports/inactive?days=60", headers=H(TOK)).json()
check("override de days funciona (60d: Rafa some)", not any(c["id"]==cli3["id"] for c in r["clients"]))

# ---------------- bloqueios
r = client.post("/blocks", headers=H(TOK), json={"professional_id":prof["id"],"day":TOM,"start_time":"12:00","end_time":"14:00","reason":"Almoço"})
check("cria bloqueio", r.status_code==200, str(r.json()))
r = client.post("/appointments", headers=H(TOK), json={
    "professional_id":prof["id"],"service_id":svc["id"],"client_name":"X","start":f"{TOM}T13:00"})
check("agendamento em horário bloqueado (409)", r.status_code==409, f"got {r.status_code}")
r = client.post("/appointments", headers=H(TOK), json={
    "professional_id":prof["id"],"service_id":svc["id"],"client_name":"X","start":f"{TOM}T11:00"})
check("agendamento fora do bloqueio ok", r.status_code==200, f"got {r.status_code}")
r = client.post("/blocks", headers=H(TOK), json={"professional_id":prof["id"],"day":TOM,"start_time":"14:00","end_time":"12:00"})
check("bloqueio invertido rejeitado (422)", r.status_code==422, f"got {r.status_code}")
blocks = client.get(f"/blocks?day={TOM}", headers=H(TOK)).json()
r = client.delete(f"/blocks/{blocks[0]['id']}", headers=H(TOK))
check("remove bloqueio", r.status_code==200)
r = client.post("/appointments", headers=H(TOK), json={
    "professional_id":prof["id"],"service_id":svc["id"],"client_name":"X","start":f"{TOM}T13:00"})
check("após remover bloqueio, horário livre", r.status_code==200, f"got {r.status_code}")

# ---------------- lista de espera
r = client.post("/waitlist", headers=H(TOK), json={"client_id":cli2["id"],"preferred_day":TOM,"notes":"qualquer hora"})
check("adiciona na lista de espera", r.status_code==200, str(r.json()))
wl = client.get("/waitlist", headers=H(TOK)).json()
check("lista de espera com link wa.me", len(wl)==1 and "wa.me/5513998887777" in wl[0]["whatsapp_link"], str(wl)[:120])
r = client.delete(f"/waitlist/{wl[0]['id']}", headers=H(TOK))
check("remove da lista de espera", r.status_code==200)
check("lista vazia após remoção", client.get("/waitlist", headers=H(TOK)).json()==[])

# ---------------- assinatura
r = client.post("/subscriptions", headers=H(TOK), json={"client_id":cli["id"],"plan_name":"Corte+Barba","cuts_per_month":4,"price":89.90})
check("cria assinatura", r.status_code==200, str(r.json()))
subs = client.get("/subscriptions", headers=H(TOK)).json()
check("assinatura com uso do mês", subs[0]["cuts_per_month"]==4 and subs[0]["used_this_month"]>=1 and "remaining" in subs[0], str(subs)[:150])
r = client.get("/reports/mrr", headers=H(TOK)).json()
check("MRR = 89.90", r["mrr"]==89.90 and r["active_subscriptions"]==1, str(r))
r = client.delete(f"/subscriptions/{subs[0]['id']}", headers=H(TOK))
check("cancela assinatura", r.status_code==200)
check("MRR 0 após cancelar", client.get("/reports/mrr", headers=H(TOK)).json()["mrr"]==0)

# ---------------- isolamento
TOK2 = client.post("/auth/register", json={"email":"z@w.com","password":"senha123"}).json()["token"]
r = client.get("/reports/inactive", headers=H(TOK2))
check("outra loja não vê inativos da loja 1", r.json()["count"]==0, str(r.json()))
r = client.get("/reminders/tomorrow", headers=H(TOK2))
check("outra loja não vê lembretes da loja 1", r.json()["reminders"]==[])

print()
if failures:
    print(f"❌ {len(failures)} falha(s): {failures}"); sys.exit(1)
print("✅ v0.6 OK")
