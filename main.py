import json
import os
import re
from typing import Optional

import firebase_admin
import httpx
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()

ASAAS_BASE_URL = (
    "https://api.asaas.com/v3"
    if ASAAS_ENV == "production"
    else "https://api-sandbox.asaas.com/v3"
)


_firebase_db = None


def _firebase_configurado() -> bool:
    return bool(
        FIREBASE_SERVICE_ACCOUNT_JSON
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    )


def get_firestore_db():
    global _firebase_db
    if _firebase_db is not None:
        return _firebase_db

    try:
        try:
            fb_app = firebase_admin.get_app()
        except ValueError:
            options = {"projectId": FIREBASE_PROJECT_ID} if FIREBASE_PROJECT_ID else None

            if FIREBASE_SERVICE_ACCOUNT_JSON:
                cred_info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
                cred = credentials.Certificate(cred_info)
                fb_app = firebase_admin.initialize_app(cred, options=options)
            else:
                # Em produção, use GOOGLE_APPLICATION_CREDENTIALS apontando
                # para um Secret File do Render com a chave do Firebase Admin.
                fb_app = firebase_admin.initialize_app(options=options)

        _firebase_db = firestore.client(app=fb_app)
        return _firebase_db
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Firebase Admin não configurado no backend. "
                "Adicione a chave de serviço do Firebase no Render. "
                f"Detalhe: {exc}"
            ),
        ) from exc


def _data_br(valor: Optional[str]) -> str:
    texto = (valor or "").strip()
    if not texto:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
        ano, mes, dia = texto.split("-")
        return f"{dia}/{mes}/{ano}"
    return texto


def _codigo_aplicativo(nome: str) -> str:
    n = (nome or "").upper()
    if "ESTOQUE" in n:
        return "estoque"
    if "RESGATE" in n:
        return "resgate"
    if "TRICICLO" in n:
        return "triciclo_familia"
    if "AVALIA" in n:
        return "avaliacao"
    if "BICICLETA" in n:
        return "bicicleta"

    slug = re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")
    return slug or "app"


def _id_licenca(cliente_id: str, empresa: str, aplicativo: str) -> str:
    codigo = _codigo_aplicativo(aplicativo)
    if (empresa or "").strip().upper() == "BIKEGO" and codigo == "estoque":
        return "bikego_estoque"
    return f"{cliente_id}_{codigo}"


async def _contexto_pagamento_asaas(payment: dict) -> tuple[Optional[str], dict, dict]:
    """Obtém externalReference e assinatura mesmo quando o webhook vier resumido."""
    payment_full = dict(payment or {})
    payment_id = payment_full.get("id")

    if payment_id:
        try:
            consultado = await asaas_get(f"/payments/{payment_id}")
            if isinstance(consultado, dict):
                payment_full.update(consultado)
        except HTTPException:
            # O webhook já pode conter dados suficientes; não falha só por isso.
            pass

    referencia = payment_full.get("externalReference")
    subscription_id = payment_full.get("subscription")
    if isinstance(subscription_id, dict):
        subscription_id = subscription_id.get("id")

    assinatura = {}
    if subscription_id:
        try:
            assinatura = await asaas_get(f"/subscriptions/{subscription_id}")
            if not referencia:
                referencia = assinatura.get("externalReference")
        except HTTPException:
            assinatura = {}

    return (
        str(referencia).strip() if referencia else None,
        payment_full,
        assinatura,
    )


def _mensagem_status(status: str) -> str:
    if status == "ATIVO":
        return "Pagamento confirmado. Aplicativo liberado."
    if status == "VENCIDO":
        return (
            "Fatura em atraso. O aplicativo permanece disponível durante "
            "o período de tolerância configurado."
        )
    if status == "BLOQUEADO":
        return "Pagamento pendente. Regularize a fatura para continuar utilizando o aplicativo."
    return "Aguardando confirmação do pagamento."


def _sincronizar_cliente_firebase(
    referencia: str,
    *,
    novo_status: Optional[str] = None,
    vencimento: Optional[str] = None,
    payment_id: Optional[str] = None,
) -> int:
    db = get_firestore_db()

    docs = list(
        db.collection("clientes")
        .where("referenciaAssinatura", "==", referencia)
        .stream()
    )

    atualizados = 0
    for doc in docs:
        dados = doc.to_dict() or {}
        empresa = str(dados.get("empresa") or "")
        status_atual = str(dados.get("status") or "AGUARDANDO PAGAMENTO").upper()
        status = (novo_status or status_atual).upper()
        liberado = status == "ATIVO"

        vencimento_final = _data_br(vencimento) if vencimento else str(
            dados.get("vencimento") or ""
        )

        apps = dados.get("aplicativosContratados")
        if not isinstance(apps, list) or not apps:
            app_unico = str(dados.get("app") or "").strip()
            apps = [app_unico] if app_unico else []

        mensagem = _mensagem_status(status)
        link = str(dados.get("linkAssinatura") or "")
        plano = str(dados.get("plano") or "")
        valor = dados.get("valor", 0)

        batch = db.batch()
        batch.set(
            doc.reference,
            {
                "status": status,
                "vencimento": vencimento_final,
                "atualizadoEm": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        for aplicativo in [str(a) for a in apps if str(a).strip()]:
            licenca_id = _id_licenca(doc.id, empresa, aplicativo)
            codigo = _codigo_aplicativo(aplicativo).upper()

            batch.set(
                db.collection("licencas").document(licenca_id),
                {
                    "clienteId": doc.id,
                    "clienteNome": empresa,
                    "aplicativo": codigo,
                    "aplicativoNome": aplicativo,
                    "plano": plano,
                    "valor": valor,
                    "vencimento": vencimento_final,
                    "status": status,
                    "liberado": liberado,
                    "mensagem": mensagem,
                    "linkPagamento": link,
                    "asaasPaymentId": payment_id or referencia,
                    "atualizadoEm": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )

            batch.set(
                db.collection("licencas_apps").document(licenca_id),
                {
                    "clienteId": doc.id,
                    "aplicativo": codigo,
                    "vencimento": vencimento_final,
                    "status": status,
                    "liberado": liberado,
                    "diasTolerancia": 2,
                    "mensagem": mensagem,
                    "linkPagamento": link,
                    "atualizadoEm": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )

        batch.commit()
        atualizados += 1

    return atualizados


app = FastAPI(title="BIKEGO Pagamentos Backend", version="2.1.0")

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
        "versao": "2.1.0",
        "ambiente_asaas": ASAAS_ENV,
        "firebase_configurado": _firebase_configurado(),
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "asaas_configurado": bool(ASAAS_API_KEY),
        "webhook_token_configurado": bool(ASAAS_WEBHOOK_TOKEN),
        "firebase_configurado": _firebase_configurado(),
        "firebase_project_id": FIREBASE_PROJECT_ID or None,
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
        "proximaCobranca": _data_br(assinatura.get("nextDueDate")),
        "valor": assinatura.get("value"),
    }


@app.post("/webhooks/asaas")
async def webhook_asaas(request: Request):
    """
    Recebe eventos do Asaas e sincroniza automaticamente o Firestore.
    PAYMENT_CONFIRMED / PAYMENT_RECEIVED -> ATIVO
    PAYMENT_OVERDUE -> VENCIDO (o app aplica os 2 dias de tolerância)
    PAYMENT_CREATED / PAYMENT_UPDATED -> atualiza o vencimento sem derrubar o acesso.
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

    referencia, payment_full, assinatura = await _contexto_pagamento_asaas(payment)
    payment_id = payment_full.get("id")
    due_date = payment_full.get("dueDate")

    if nome_evento in {"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"}:
        novo_status = "ATIVO"
        vencimento = assinatura.get("nextDueDate") or due_date
    elif nome_evento == "PAYMENT_OVERDUE":
        novo_status = "VENCIDO"
        vencimento = due_date
    elif nome_evento in {"PAYMENT_CREATED", "PAYMENT_UPDATED"}:
        novo_status = None
        vencimento = due_date
    else:
        return {
            "received": True,
            "event": nome_evento,
            "ignored": True,
        }

    if not referencia:
        return {
            "received": True,
            "event": nome_evento,
            "firebaseUpdated": 0,
            "warning": "Cobrança sem externalReference identificável.",
            "paymentId": payment_id,
        }

    atualizados = _sincronizar_cliente_firebase(
        referencia,
        novo_status=novo_status,
        vencimento=vencimento,
        payment_id=str(payment_id) if payment_id else None,
    )

    return {
        "received": True,
        "event": nome_evento,
        "paymentId": payment_id,
        "paymentStatus": payment_full.get("status"),
        "subscription": payment_full.get("subscription"),
        "externalReference": referencia,
        "firebaseUpdated": atualizados,
        "statusAplicado": novo_status,
        "vencimentoAplicado": _data_br(vencimento),
    }
