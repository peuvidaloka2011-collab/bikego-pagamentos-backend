import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()

ASAAS_BASE_URL = (
    "https://api.asaas.com/v3"
    if ASAAS_ENV == "production"
    else "https://api-sandbox.asaas.com/v3"
)

app = FastAPI(title="BIKEGO Pagamentos Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bikego-pagamentos-web.web.app",
        "https://bikego-pagamentos-web.firebaseapp.com",
    ],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class LinkAssinaturaRequest(BaseModel):
    empresa: str = Field(min_length=2, max_length=120)
    valor: float = Field(gt=0)
    descricao: str = Field(
        default="Assinatura mensal dos aplicativos BIKEGO - Armazenamento e serviços",
        max_length=500,
    )
    referencia: Optional[str] = Field(default=None, max_length=200)


def asaas_headers() -> dict:
    if not ASAAS_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ASAAS_API_KEY não configurada no servidor.",
        )

    return {
        "access_token": ASAAS_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
        "User-Agent": "BIKEGO-Pagamentos/2.0",
    }


async def asaas_get(path: str, params: Optional[dict] = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{ASAAS_BASE_URL}{path}",
                headers=asaas_headers(),
                params=params or {},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação com o Asaas: {exc}",
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=data)

    return data


@app.get("/")
def raiz():
    return {
        "app": "BIKEGO Pagamentos Backend",
        "status": "online",
        "versao": "2.0.0",
        "ambiente_asaas": ASAAS_ENV,
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "asaas_configurado": bool(ASAAS_API_KEY),
        "webhook_token_configurado": bool(ASAAS_WEBHOOK_TOKEN),
        "ambiente": ASAAS_ENV,
    }


@app.post("/api/assinaturas/link")
async def criar_link_assinatura(dados: LinkAssinaturaRequest):
    payload = {
        "billingType": "CREDIT_CARD",
        "chargeType": "RECURRENT",
        "name": "BIKEGO - ARMAZENAMENTO FIREBASE",
        "description": dados.descricao,
        "value": round(dados.valor, 2),
        "subscriptionCycle": "MONTHLY",
        "notificationEnabled": False,
    }

    if dados.referencia:
        payload["externalReference"] = dados.referencia

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{ASAAS_BASE_URL}/paymentLinks",
                headers=asaas_headers(),
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação com o Asaas: {exc}",
        ) from exc

    try:
        resposta = response.json()
    except Exception:
        resposta = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=resposta)

    return {
        "ok": True,
        "empresa": dados.empresa,
        "valor": dados.valor,
        "tipo": "CREDIT_CARD_RECURRENT",
        "referencia": dados.referencia,
        "asaas": resposta,
    }


@app.get("/api/assinaturas/status/{referencia}")
async def status_assinatura(referencia: str):
    """
    Consulta pontual de conciliação.
    Não faz polling contínuo: o Webhook continua sendo o mecanismo principal.
    """
    assinaturas = await asaas_get(
        "/subscriptions",
        params={
            "externalReference": referencia,
            "limit": 10,
        },
    )

    lista = assinaturas.get("data") or []
    if not lista:
        return {
            "ok": True,
            "referencia": referencia,
            "status": "AGUARDANDO_PAGAMENTO",
            "pago": False,
        }

    assinatura = lista[0]
    subscription_id = assinatura.get("id")

    pagamentos = await asaas_get(
        "/payments",
        params={
            "subscription": subscription_id,
            "limit": 20,
        },
    )

    items = pagamentos.get("data") or []
    statuses = [str(x.get("status", "")).upper() for x in items]

    pago = any(s in {"CONFIRMED", "RECEIVED"} for s in statuses)
    vencido = any(s == "OVERDUE" for s in statuses)

    if pago:
        status = "ATIVO"
    elif vencido:
        status = "VENCIDO"
    else:
        status = "AGUARDANDO_PAGAMENTO"

    return {
        "ok": True,
        "referencia": referencia,
        "status": status,
        "pago": pago,
        "subscriptionId": subscription_id,
        "subscriptionStatus": assinatura.get("status"),
        "proximaCobranca": assinatura.get("nextDueDate"),
        "valor": assinatura.get("value"),
    }


@app.post("/webhooks/asaas")
async def webhook_asaas(request: Request):
    """
    Recebe eventos do Asaas.
    O token configurado no Asaas deve ser o MESMO de ASAAS_WEBHOOK_TOKEN no Render.
    """
    if not ASAAS_WEBHOOK_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ASAAS_WEBHOOK_TOKEN não configurado no servidor.",
        )

    token_recebido = request.headers.get("asaas-access-token", "")
    if token_recebido != ASAAS_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Webhook não autorizado.")

    evento = await request.json()
    nome_evento = str(evento.get("event", "")).upper()

    payment = evento.get("payment") or {}
    subscription = payment.get("subscription") or evento.get("subscription")

    return {
        "received": True,
        "event": nome_evento,
        "paymentId": payment.get("id"),
        "paymentStatus": payment.get("status"),
        "subscription": subscription,
    }
