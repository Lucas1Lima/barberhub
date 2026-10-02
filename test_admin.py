"""Testes v0.5 — admin, convites, troca de email/senha, reset.
Roda com: python test_admin.py"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["BARBERHUB_DB"] = os.path.join(tempfile.mkdtemp(), "admin.db")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# 1º registro = admin automático
r = client.post("/auth/register", json={"email": "lucas@admin.com", "password": "senha123", "shop_name": "HQ"})
TOK_A = r.json()["token"]
r = client.get("/auth/me", headers=H(TOK_A))
check("1º registro é admin", r.json().get("is_admin") == 1, str(r.json()))

# 2º registro = comum
TOK_B = client.post("/auth/register", json={"email": "joao@bar.com", "password": "senha123", "shop_name": "Bar do João"}).json()["token"]
r = client.get("/auth/me", headers=H(TOK_B))
check("2º registro não é admin", r.json().get("is_admin") == 0)

# não-admin bloqueado no admin
r = client.get("/admin/shops", headers=H(TOK_B))
check("não-admin bloqueado (403)", r.status_code == 403, f"got {r.status_code}")

# admin lista lojas com métricas
r = client.get("/admin/shops", headers=H(TOK_A))
shops = r.json()
check("admin lista 2 lojas", r.status_code == 200 and len(shops) == 2, str(shops)[:100])
check("métricas presentes (appointments, appts_30d, last_login)", all(k in shops[0] for k in ("appointments", "appts_30d", "last_login")))

# desativar conta: login bloqueado
shop_b = next(s for s in shops if s["owner_email"] == "joao@bar.com")
r = client.patch(f"/admin/shops/{shop_b['id']}", headers=H(TOK_A), json={"active": False})
check("admin desativa loja", r.status_code == 200 and r.json()["active"] == 0)
r = client.post("/auth/login", json={"email": "joao@bar.com", "password": "senha123"})
check("login de conta desativada bloqueado (403)", r.status_code == 403, f"got {r.status_code}")
# dados da loja desativada inacessíveis via token antigo
r = client.get("/professionals", headers=H(TOK_B))
check("token de conta desativada bloqueado", r.status_code == 403, f"got {r.status_code}")
r = client.patch(f"/admin/shops/{shop_b['id']}", headers=H(TOK_A), json={"active": True})
check("reativação ok", r.json()["active"] == 1)
r = client.post("/auth/login", json={"email": "joao@bar.com", "password": "senha123"})
check("login volta a funcionar", r.status_code == 200)

# admin não desativa a si mesmo
r = client.get("/auth/me", headers=H(TOK_A))
me_id = r.json()["id"]
r = client.patch(f"/admin/shops/{me_id}", headers=H(TOK_A), json={"active": False})
check("admin não desativa a própria conta (422)", r.status_code == 422, f"got {r.status_code}")

# reset de senha pelo admin
r = client.post(f"/admin/shops/{shop_b['id']}/reset-password", headers=H(TOK_A))
temp = r.json().get("temporary_password", "")
check("admin gera senha temporária", r.status_code == 200 and len(temp) >= 8, str(r.json()))
r = client.post("/auth/login", json={"email": "joao@bar.com", "password": temp})
check("login com senha temporária ok", r.status_code == 200)

# troca de email (com senha)
TOK_B = client.post("/auth/login", json={"email": "joao@bar.com", "password": temp}).json()["token"]
r = client.patch("/auth/email", headers=H(TOK_B), json={"new_email": "joao.novo@bar.com", "password": temp})
check("troca de email ok", r.status_code == 200 and r.json()["email"] == "joao.novo@bar.com", str(r.json()))
r = client.post("/auth/login", json={"email": "joao.novo@bar.com", "password": temp})
check("login com email novo ok", r.status_code == 200)
r = client.patch("/auth/email", headers=H(TOK_B), json={"new_email": "lucas@admin.com", "password": temp})
check("troca para email em uso bloqueada (409)", r.status_code == 409, f"got {r.status_code}")
r = client.patch("/auth/email", headers=H(TOK_B), json={"new_email": "x@y.com", "password": "errada"})
check("troca de email exige senha correta (401)", r.status_code == 401, f"got {r.status_code}")

# troca de senha própria
r = client.patch("/auth/password", headers=H(TOK_B), json={"old_password": temp, "new_password": "novaSenha1"})
check("troca de senha própria ok", r.status_code == 200)
r = client.post("/auth/login", json={"email": "joao.novo@bar.com", "password": "novaSenha1"})
check("login com senha nova ok", r.status_code == 200)

# convite: cria → valida → aceita
r = client.post("/admin/invites", headers=H(TOK_A), json={"email": "comprador@inf.com", "shop_name": "Salão Comprado"})
inv = r.json()
check("convite criado com link", r.status_code == 200 and "/invite?token=" in inv["link"], str(inv))
check("convite sem SMTP volta com link (sent=false)", inv["sent"] is False and inv["link"])
token_inv = inv["link"].split("token=")[1]

r = client.get(f"/auth/invite/{token_inv}")
check("convite público valida email pré-cadastrado", r.status_code == 200 and r.json()["email"] == "comprador@inf.com", str(r.json()))

r = client.post("/auth/invite/accept", json={"token": token_inv, "password": "minhaSenha1"})
check("aceite do convite cria conta + token", r.status_code == 200 and "token" in r.json(), str(r.json()))
TOK_C = r.json()["token"]

r = client.post("/auth/invite/accept", json={"token": token_inv, "password": "outra1"})
check("convite não reutilizável (422)", r.status_code == 422, f"got {r.status_code}")

r = client.post("/admin/invites", headers=H(TOK_A), json={"email": "comprador@inf.com"})
check("convite para email com conta bloqueado (409)", r.status_code == 409, f"got {r.status_code}")

r = client.get("/admin/invites", headers=H(TOK_A))
invs = r.json()
check("lista de convites mostra usado", any(i["used_at"] for i in invs), str(invs)[:150])

# não-admin não cria convite
r = client.post("/admin/invites", headers=H(TOK_B), json={"email": "x@y.com"})
check("não-admin não cria convite (403)", r.status_code == 403, f"got {r.status_code}")

# esqueci senha → reset
r = client.post("/auth/forgot-password", json={"email": "joao.novo@bar.com"})
fp = r.json()
check("forgot-password gera link", r.status_code == 200 and fp["link"], str(fp))
token_r = fp["link"].split("token=")[1]
r = client.post("/auth/reset-password", json={"token": token_r, "new_password": "outraSenha2"})
check("reset com token ok", r.status_code == 200)
r = client.post("/auth/login", json={"email": "joao.novo@bar.com", "password": "outraSenha2"})
check("login após reset ok", r.status_code == 200)
r = client.post("/auth/reset-password", json={"token": token_r, "new_password": "hack123"})
check("token de reset é de 1 uso (422)", r.status_code == 422, f"got {r.status_code}")
r = client.post("/auth/forgot-password", json={"email": "naoexiste@x.com"})
check("forgot para email inexistente não revela (ok genérico)", r.status_code == 200 and not r.json()["link"])

print()
if failures:
    print(f"❌ {len(failures)} falha(s): {failures}")
    sys.exit(1)
print("✅ Todos os testes de admin/convites passaram")
