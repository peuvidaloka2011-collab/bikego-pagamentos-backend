import os
import sys
import httpx

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()

BASE_URL = (
    "https://api.asaas.com/v3"
    if ASAAS_ENV == "production"
    else "https://api-sandbox.asaas.com/v3"
)

WEBHOOK_NAME = "BIKEGO CHECKOUT"
WEBHOOK_URL = "https://bikego-pagamentos-backend.onrender.com/webhooks/asaas"
WEBHOOK_EMAIL = "bikegomanutencao@gmail.com"
EVENTS = [
    "CHECKOUT_CREATED",
    "CHECKOUT_PAID",
    "CHECKOUT_CANCELED",
    "CHECKOUT_EXPIRED",
]


def fail(message: str) -> None:
    print(f"ERRO: {message}")
    sys.exit(1)


if not ASAAS_API_KEY:
    fail("ASAAS_API_KEY não configurada no Render.")

if not ASAAS_WEBHOOK_TOKEN:
    fail("ASAAS_WEBHOOK_TOKEN não configurado no Render.")

if not (32 <= len(ASAAS_WEBHOOK_TOKEN) <= 255):
    fail(
        "ASAAS_WEBHOOK_TOKEN precisa ter entre 32 e 255 caracteres "
        "para ser usado como authToken do webhook."
    )

headers = {
    "access_token": ASAAS_API_KEY,
    "accept": "application/json",
    "content-type": "application/json",
    "User-Agent": "BIKEGO-Pagamentos/2.0",
}

payload = {
    "name": WEBHOOK_NAME,
    "url": WEBHOOK_URL,
    "email": WEBHOOK_EMAIL,
    "enabled": True,
    "interrupted": False,
    "apiVersion": 3,
    "authToken": ASAAS_WEBHOOK_TOKEN,
    "sendType": "SEQUENTIALLY",
    "events": EVENTS,
}

with httpx.Client(timeout=60.0) as client:
    response = client.get(
        f"{BASE_URL}/webhooks",
        headers=headers,
        params={"limit": 100, "offset": 0},
    )

    if response.status_code >= 400:
        fail(
            f"Falha ao listar webhooks ({response.status_code}): "
            f"{response.text}"
        )

    data = response.json()
    existing = None

    for item in data.get("data", []):
        if str(item.get("name") or "").strip().upper() == WEBHOOK_NAME:
            existing = item
            break

    if existing:
        webhook_id = existing.get("id")
        response = client.put(
            f"{BASE_URL}/webhooks/{webhook_id}",
            headers=headers,
            json=payload,
        )
        action = "ATUALIZADO"
    else:
        response = client.post(
            f"{BASE_URL}/webhooks",
            headers=headers,
            json=payload,
        )
        action = "CRIADO"

    if response.status_code >= 400:
        fail(
            f"Falha ao salvar webhook ({response.status_code}): "
            f"{response.text}"
        )

    saved = response.json()

print("")
print(f"BIKEGO CHECKOUT {action} COM SUCESSO")
print(f"Ambiente: {ASAAS_ENV.upper()}")
print(f"Webhook ID: {saved.get('id')}")
print(f"Ativo: {saved.get('enabled')}")
print(f"Fila interrompida: {saved.get('interrupted')}")
print("Eventos:")
for event in EVENTS:
    print(f" - {event}")
