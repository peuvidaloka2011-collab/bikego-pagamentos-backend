import hashlib
import hmac
import json
import os
import re
import uuid
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
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()
MERCADOPAGO_PUBLIC_KEY = os.getenv("MERCADOPAGO_PUBLIC_KEY", "").strip()
MERCADOPAGO_TEST_ACCESS_TOKEN = os.getenv("MERCADOPAGO_TEST_ACCESS_TOKEN", "").strip()
MERCADOPAGO_WEBHOOK_SECRET = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "").strip()
PAGBANK_TOKEN = os.getenv("PAGBANK_TOKEN", "").strip()
PAGBANK_ENV = os.getenv("PAGBANK_ENV", "production").strip().lower()
APPLEPAY_MERCHANT_ID = os.getenv("APPLEPAY_MERCHANT_ID", "").strip()
APPLEPAY_MERCHANT_CERTIFICATE_ID = os.getenv("APPLEPAY_MERCHANT_CERTIFICATE_ID", "").strip()
MERCADOPAGO_APPLEPAY_PRODUCT_ID = os.getenv("MERCADOPAGO_APPLEPAY_PRODUCT_ID", "").strip()
APPLEPAY_DOMAIN = os.getenv("APPLEPAY_DOMAIN", "bikego-pagamentos-web.web.app").strip()
APPLEPAY_DISPLAY_NAME = os.getenv("APPLEPAY_DISPLAY_NAME", "FIREBASE ARMAZENAMENTO").strip()
MERCADOPAGO_BACK_URL = os.getenv(
    "MERCADOPAGO_BACK_URL",
    "https://bikego-pagamentos-web.web.app",
).strip()
MERCADOPAGO_NOTIFICATION_URL = os.getenv(
    "MERCADOPAGO_NOTIFICATION_URL",
    "https://bikego-pagamentos-backend.onrender.com/webhooks/mercadopago",
).strip()
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()

ASAAS_BASE_URL = (
    "https://api.asaas.com/v3"
    if ASAAS_ENV == "production"
    else "https://api-sandbox.asaas.com/v3"
)

PAGBANK_BASE_URL = (
    "https://api.pagseguro.com"
    if PAGBANK_ENV == "production"
    else "https://sandbox.api.pagseguro.com"
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


def _registrar_notificacao_firebase(
    db,
    *,
    cliente_id: str,
    cliente_nome: str,
    tipo: str,
    titulo: str,
    mensagem: str,
    nivel: str,
    referencia: str,
    payment_id: Optional[str] = None,
    valor=None,
    provedor: Optional[str] = None,
) -> None:
    chave_base = f"{cliente_id}|{tipo}|{payment_id or referencia}"
    doc_id = hashlib.sha256(chave_base.encode()).hexdigest()[:32]
    db.collection("notificacoes").document(doc_id).set(
        {
            "tipo": tipo,
            "titulo": titulo,
            "mensagem": mensagem,
            "nivel": nivel,
            "clienteId": cliente_id,
            "clienteNome": cliente_nome,
            "valor": valor,
            "provedor": provedor or "",
            "referencia": referencia,
            "paymentId": payment_id or "",
            "lida": False,
            "criadaEm": firestore.SERVER_TIMESTAMP,
            "origem": "BACKEND",
        },
        merge=True,
    )


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
        provedor = str(dados.get("provedorPagamento") or "ASAAS").upper()

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

        if status != status_atual:
            if status == "ATIVO":
                _registrar_notificacao_firebase(
                    db,
                    cliente_id=doc.id,
                    cliente_nome=empresa,
                    tipo="PAGAMENTO_APROVADO",
                    titulo="Pagamento aprovado",
                    mensagem=f"{empresa} teve o pagamento confirmado. Aplicativo liberado.",
                    nivel="SUCESSO",
                    referencia=referencia,
                    payment_id=payment_id,
                    valor=valor,
                    provedor=provedor,
                )
            elif status == "VENCIDO":
                _registrar_notificacao_firebase(
                    db,
                    cliente_id=doc.id,
                    cliente_nome=empresa,
                    tipo="FATURA_VENCIDA",
                    titulo="Fatura vencida",
                    mensagem=f"{empresa} entrou em atraso. Verifique a cobrança recorrente.",
                    nivel="ATENCAO",
                    referencia=referencia,
                    payment_id=payment_id,
                    valor=valor,
                    provedor=provedor,
                )
            elif status == "BLOQUEADO":
                _registrar_notificacao_firebase(
                    db,
                    cliente_id=doc.id,
                    cliente_nome=empresa,
                    tipo="APP_BLOQUEADO",
                    titulo="Aplicativo bloqueado",
                    mensagem=f"{empresa} teve o acesso bloqueado.",
                    nivel="ERRO",
                    referencia=referencia,
                    payment_id=payment_id,
                    valor=valor,
                    provedor=provedor,
                )

        atualizados += 1

    return atualizados


app = FastAPI(title="BIKEGO Pagamentos Backend", version="2.7.2")

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


class ApplePaySessionRequest(BaseModel):
    validation_url: str = Field(min_length=10, max_length=1000)


class ApplePayPaymentRequest(BaseModel):
    empresa: str = Field(min_length=2, max_length=120)
    valor: float = Field(gt=0)
    referencia: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=5, max_length=180)
    cpf: str = Field(min_length=11, max_length=20)
    payment_data_b64: str = Field(min_length=20)
    transaction_identifier: str = Field(min_length=4, max_length=300)
    network: str = Field(min_length=2, max_length=50)


class MercadoPagoCardSubscriptionRequest(LinkAssinaturaRequest):
    email: str = Field(min_length=5, max_length=180)
    card_token_id: str = Field(min_length=10, max_length=200)


class MercadoPagoLinkRequest(LinkAssinaturaRequest):
    email: str = Field(min_length=5, max_length=180)


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


def pagbank_headers() -> dict:
    if not PAGBANK_TOKEN:
        raise HTTPException(
            status_code=503,
            detail=(
                "PAGBANK_TOKEN não configurado no servidor. "
                "Adicione o token do PagBank no Render e solicite a liberação "
                "do método DEBIT_CARD na conta PagBank."
            ),
        )
    return {
        "Authorization": f"Bearer {PAGBANK_TOKEN}",
        "accept": "application/json",
        "content-type": "application/json",
    }


async def pagbank_post(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{PAGBANK_BASE_URL}{path}",
                headers=pagbank_headers(),
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação com o PagBank: {exc}",
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=data)
    return data


async def pagbank_get(path: str, params: Optional[dict] = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{PAGBANK_BASE_URL}{path}",
                headers=pagbank_headers(),
                params=params or {},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação com o PagBank: {exc}",
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=data)
    return data


def _applepay_configurado() -> bool:
    return bool(
        MERCADOPAGO_PUBLIC_KEY
        and MERCADOPAGO_ACCESS_TOKEN
        and MERCADOPAGO_APPLEPAY_PRODUCT_ID
        and APPLEPAY_MERCHANT_ID
        and APPLEPAY_MERCHANT_CERTIFICATE_ID
        and APPLEPAY_DOMAIN
    )


def applepay_headers() -> dict:
    if not _applepay_configurado():
        raise HTTPException(
            status_code=503,
            detail=(
                "Apple Pay ainda não configurado. Verifique Merchant ID, "
                "certificate_id, Product ID, Public Key e Access Token."
            ),
        )
    return {
        "X-Product-ID": MERCADOPAGO_APPLEPAY_PRODUCT_ID,
        "X-Public-key": MERCADOPAGO_PUBLIC_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }


async def applepay_post(path: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"https://api.mercadopago.com{path}",
                headers=applepay_headers(),
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação Apple Pay/Mercado Pago: {exc}",
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=data)
    return data


async def mercadopago_order_post(payload: dict) -> dict:
    headers = mercadopago_headers(False)
    headers["X-Idempotency-Key"] = str(uuid.uuid4())
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.mercadopago.com/v1/orders",
                headers=headers,
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha ao criar order Apple Pay: {exc}",
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=data)
    return data


MERCADOPAGO_BASE_URL = "https://api.mercadopago.com"


def mercadopago_headers(teste: bool = False) -> dict:
    token = MERCADOPAGO_TEST_ACCESS_TOKEN if teste else MERCADOPAGO_ACCESS_TOKEN
    nome = "MERCADOPAGO_TEST_ACCESS_TOKEN" if teste else "MERCADOPAGO_ACCESS_TOKEN"
    if not token:
        raise HTTPException(
            status_code=500,
            detail=f"{nome} não configurado no servidor.",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "accept": "application/json",
        "content-type": "application/json",
    }
    if teste:
        headers["X-scope"] = "stage"
    return headers


async def mercadopago_usuario_atual() -> dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://api.mercadolibre.com/users/me",
                headers={
                    "Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}",
                    "accept": "application/json",
                },
            )
        if response.status_code >= 400:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def mercadopago_get(
    path: str,
    params: Optional[dict] = None,
    *,
    teste: bool = False,
) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"{MERCADOPAGO_BASE_URL}{path}",
                headers=mercadopago_headers(teste),
                params=params or {},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação com o Mercado Pago: {exc}",
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=data)

    return data


async def mercadopago_post(
    path: str,
    payload: dict,
    *,
    teste: bool = False,
) -> dict:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{MERCADOPAGO_BASE_URL}{path}",
                headers=mercadopago_headers(teste),
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação com o Mercado Pago: {exc}",
        ) from exc

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=data)

    return data


def _data_br_iso(valor: Optional[str]) -> str:
    texto = (valor or "").strip()
    if len(texto) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto[:10]):
        ano, mes, dia = texto[:10].split("-")
        return f"{dia}/{mes}/{ano}"
    return _data_br(texto)


def _validar_webhook_mercadopago(request: Request, data_id: str) -> None:
    # Quando uma assinatura secreta estiver configurada, validamos a origem.
    # Sem ela, o backend ainda consulta o recurso diretamente na API do
    # Mercado Pago usando o Access Token antes de alterar qualquer licença.
    if not MERCADOPAGO_WEBHOOK_SECRET:
        return

    assinatura = request.headers.get("x-signature", "")
    request_id = request.headers.get("x-request-id", "")
    if not assinatura or not request_id:
        raise HTTPException(status_code=401, detail="Webhook Mercado Pago sem assinatura.")

    partes = {}
    for parte in assinatura.split(","):
        if "=" in parte:
            chave, valor = parte.split("=", 1)
            partes[chave.strip()] = valor.strip()

    ts = partes.get("ts", "")
    v1 = partes.get("v1", "")
    if not ts or not v1:
        raise HTTPException(status_code=401, detail="Assinatura Mercado Pago inválida.")

    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    esperado = hmac.new(
        MERCADOPAGO_WEBHOOK_SECRET.encode(),
        manifest.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(esperado, v1):
        raise HTTPException(status_code=401, detail="Webhook Mercado Pago não autorizado.")


@app.get("/")
def raiz():
    return {
        "app": "BIKEGO Pagamentos Backend",
        "status": "online",
        "versao": "2.7.2",
        "ambiente_asaas": ASAAS_ENV,
        "mercadopago_configurado": bool(MERCADOPAGO_ACCESS_TOKEN),
        "mercadopago_public_key_configurada": bool(MERCADOPAGO_PUBLIC_KEY),
        "firebase_configurado": _firebase_configurado(),
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "asaas_configurado": bool(ASAAS_API_KEY),
        "webhook_token_configurado": bool(ASAAS_WEBHOOK_TOKEN),
        "mercadopago_configurado": bool(MERCADOPAGO_ACCESS_TOKEN),
        "mercadopago_teste_configurado": bool(MERCADOPAGO_TEST_ACCESS_TOKEN),
        "mercadopago_webhook_configurado": bool(MERCADOPAGO_WEBHOOK_SECRET),
        "pagbank_configurado": bool(PAGBANK_TOKEN),
        "pagbank_ambiente": PAGBANK_ENV,
        "applepay_configurado": _applepay_configurado(),
        "mercadopago_notification_url": MERCADOPAGO_NOTIFICATION_URL,
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


@app.get("/api/mercadopago/teste/diagnostico")
async def diagnostico_mercadopago_teste():
    if not MERCADOPAGO_TEST_ACCESS_TOKEN:
        return {
            "ok": False,
            "configurado": False,
            "mensagem": "MERCADOPAGO_TEST_ACCESS_TOKEN não configurado.",
        }

    prefixo = MERCADOPAGO_TEST_ACCESS_TOKEN[:8]
    try:
        resposta = await mercadopago_get("/preapproval/search", params={"limit": 1}, teste=True)
        return {
            "ok": True,
            "configurado": True,
            "token_prefixo": f"{prefixo}...",
            "assinaturas_api": "AUTORIZADA",
            "ambiente": "TESTE",
            "resultados": len(resposta.get("results") or []),
        }
    except HTTPException as exc:
        return {
            "ok": False,
            "configurado": True,
            "token_prefixo": f"{prefixo}...",
            "assinaturas_api": "NAO_AUTORIZADA",
            "ambiente": "TESTE",
            "status_code": exc.status_code,
            "detail": exc.detail,
        }


@app.get("/api/mercadopago/diagnostico")
async def diagnostico_mercadopago():
    if not MERCADOPAGO_ACCESS_TOKEN:
        return {
            "ok": False,
            "configurado": False,
            "mensagem": "MERCADOPAGO_ACCESS_TOKEN não configurado.",
        }

    prefixo = MERCADOPAGO_ACCESS_TOKEN[:8]
    try:
        resposta = await mercadopago_get(
            "/preapproval/search",
            params={"limit": 1},
        )
        return {
            "ok": True,
            "configurado": True,
            "token_prefixo": f"{prefixo}...",
            "assinaturas_api": "AUTORIZADA",
            "resultados": len(resposta.get("results") or []),
        }
    except HTTPException as exc:
        detalhe = exc.detail
        return {
            "ok": False,
            "configurado": True,
            "token_prefixo": f"{prefixo}...",
            "assinaturas_api": "NAO_AUTORIZADA",
            "status_code": exc.status_code,
            "detail": detalhe,
        }


@app.post("/api/assinaturas/mercadopago/teste/link")
async def criar_link_assinatura_mercadopago_teste(dados: MercadoPagoLinkRequest):
    # O pagador precisa ser uma conta de teste COMPRADOR do mesmo site/país
    # da conta vendedora. Para Brasil, use uma conta de teste MLB criada no
    # painel do Mercado Pago Developers.
    email = dados.email.strip().lower()
    if "@" not in email:
        raise HTTPException(
            status_code=400,
            detail="Informe o e-mail da conta de teste COMPRADOR do Mercado Pago.",
        )

    payload = {
        "reason": "BIKEGO - TESTE ARMAZENAMENTO FIREBASE",
        "external_reference": dados.referencia,
        "payer_email": email,
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": round(dados.valor, 2),
            "currency_id": "BRL",
        },
        "back_url": MERCADOPAGO_BACK_URL,
        "notification_url": f"{MERCADOPAGO_NOTIFICATION_URL}?ambiente=teste",
        "status": "pending",
    }

    resposta = await mercadopago_post("/preapproval", payload, teste=True)
    link = resposta.get("init_point")
    if not link:
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago teste não retornou o link da assinatura.",
        )

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO_TESTE",
        "empresa": dados.empresa,
        "valor": dados.valor,
        "referencia": dados.referencia,
        "subscriptionId": resposta.get("id"),
        "url": link,
        "mercadopago": resposta,
    }


@app.get("/api/applepay/config")
def applepay_config():
    return {
        "ok": True,
        "configured": _applepay_configurado(),
        "merchantIdentifier": APPLEPAY_MERCHANT_ID if _applepay_configurado() else None,
        "domainName": APPLEPAY_DOMAIN if _applepay_configurado() else None,
        "displayName": APPLEPAY_DISPLAY_NAME if _applepay_configurado() else None,
        "countryCode": "BR",
        "currencyCode": "BRL",
        "supportedNetworks": ["visa", "masterCard"],
    }


@app.post("/api/applepay/session")
async def applepay_session(dados: ApplePaySessionRequest):
    payload = {
        "id": APPLEPAY_MERCHANT_CERTIFICATE_ID,
        "merchantIdentifier": APPLEPAY_MERCHANT_ID,
        "domainName": APPLEPAY_DOMAIN,
        "displayName": APPLEPAY_DISPLAY_NAME,
        "initiative": "web",
        "initiativeContext": APPLEPAY_DOMAIN,
        "validationURL": dados.validation_url,
    }
    return await applepay_post("/applepay/v1/session", payload)


@app.post("/api/applepay/pay")
async def applepay_pay(dados: ApplePayPaymentRequest):
    cpf = re.sub(r"\D", "", dados.cpf or "")
    if len(cpf) != 11:
        raise HTTPException(status_code=400, detail="Informe um CPF válido para o Apple Pay.")

    network_raw = (dados.network or "").strip().lower()
    network_map = {
        "visa": "visa",
        "mastercard": "master",
        "mastercardcredit": "master",
        "master card": "master",
        "master": "master",
    }
    payment_method_id = network_map.get(network_raw)
    if not payment_method_id:
        raise HTTPException(
            status_code=400,
            detail="Apple Pay disponível neste checkout somente para Visa e Mastercard.",
        )

    tokenize_payload = {
        "payment_method": {
            "type": "applepay",
            "payment_data": dados.payment_data_b64,
        },
        "transaction_identifier": dados.transaction_identifier,
    }
    tokenizado = await applepay_post(
        "/platforms/pci/applepay/v1/tokenize",
        tokenize_payload,
    )
    token = str(tokenizado.get("id") or "").strip()
    if not token:
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago não retornou o token do Apple Pay.",
        )

    order_payload = {
        "type": "online",
        "processing_mode": "automatic",
        "total_amount": f"{dados.valor:.2f}",
        "currency_id": "BRL",
        "external_reference": dados.referencia[:64],
        "payer": {
            "email": dados.email.strip().lower(),
            "identification": {
                "type": "CPF",
                "number": cpf,
            },
        },
        "transactions": {
            "payments": [
                {
                    "amount": f"{dados.valor:.2f}",
                    "payment_method": {
                        "id": payment_method_id,
                        "type": "credit_card",
                        "token": token,
                        "installments": 1,
                    },
                }
            ]
        },
    }

    order = await mercadopago_order_post(order_payload)
    status = str(order.get("status") or "").lower()
    status_detail = str(order.get("status_detail") or "").lower()
    order_id = str(order.get("id") or "")

    db = get_firestore_db()
    db.collection("mercadopago_orders").document(dados.referencia).set(
        {
            "orderId": order_id,
            "referencia": dados.referencia,
            "empresa": dados.empresa,
            "valor": dados.valor,
            "metodo": "APPLE_PAY",
            "status": status,
            "statusDetail": status_detail,
            "atualizadoEm": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    if status == "processed":
        _sincronizar_cliente_firebase(
            dados.referencia,
            novo_status="ATIVO",
            payment_id=order_id or None,
        )

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO",
        "metodo": "APPLE_PAY",
        "orderId": order_id,
        "status": status,
        "statusDetail": status_detail,
        "approved": status == "processed",
        "order": order,
    }


@app.get("/api/mercadopago/public-key")
def mercadopago_public_key():
    if not MERCADOPAGO_PUBLIC_KEY:
        raise HTTPException(
            status_code=503,
            detail="MERCADOPAGO_PUBLIC_KEY não configurada no servidor.",
        )
    return {"ok": True, "public_key": MERCADOPAGO_PUBLIC_KEY}


@app.post("/api/assinaturas/mercadopago/cartao")
async def criar_assinatura_mercadopago_cartao(
    dados: MercadoPagoCardSubscriptionRequest,
):
    """
    Cria assinatura recorrente diretamente com card_token_id gerado
    pelo Card Payment Brick no navegador do cliente.
    O backend nunca recebe número completo do cartão nem CVV.
    """
    email = dados.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Informe um e-mail válido do pagador.")

    vendedor = await mercadopago_usuario_atual()
    email_vendedor = str(vendedor.get("email") or "").strip().lower()
    if email_vendedor and email == email_vendedor:
        raise HTTPException(
            status_code=400,
            detail=(
                "Use o e-mail real do comprador/titular do cartão. "
                "O e-mail do pagador não pode ser o mesmo da conta vendedora do Mercado Pago."
            ),
        )

    payload = {
        "reason": "BIKEGO - ARMAZENAMENTO FIREBASE",
        "external_reference": dados.referencia,
        "payer_email": email,
        "card_token_id": dados.card_token_id,
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": round(dados.valor, 2),
            "currency_id": "BRL",
        },
        "back_url": MERCADOPAGO_BACK_URL,
        "status": "authorized",
    }

    resposta = await mercadopago_post("/preapproval", payload)

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO_TRANSPARENTE",
        "empresa": dados.empresa,
        "valor": dados.valor,
        "referencia": dados.referencia,
        "subscriptionId": resposta.get("id"),
        "status": resposta.get("status"),
        "nextPaymentDate": resposta.get("next_payment_date"),
        "paymentMethodId": resposta.get("payment_method_id"),
        "mercadopago": resposta,
    }


@app.post("/api/assinaturas/mercadopago/link")
async def criar_link_assinatura_mercadopago(dados: MercadoPagoLinkRequest):
    email = dados.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Informe um e-mail válido do pagador.")

    payload = {
        "reason": "BIKEGO - ARMAZENAMENTO FIREBASE",
        "external_reference": dados.referencia,
        "payer_email": email,
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": round(dados.valor, 2),
            "currency_id": "BRL",
        },
        "back_url": MERCADOPAGO_BACK_URL,
        "notification_url": MERCADOPAGO_NOTIFICATION_URL,
        "status": "pending",
    }

    resposta = await mercadopago_post("/preapproval", payload)
    link = resposta.get("init_point")
    if not link:
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago não retornou o link da assinatura.",
        )

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO",
        "empresa": dados.empresa,
        "valor": dados.valor,
        "referencia": dados.referencia,
        "subscriptionId": resposta.get("id"),
        "url": link,
        "mercadopago": resposta,
    }


@app.post("/api/pagamentos/pagbank/debito/link")
async def criar_link_pagamento_debito_pagbank(dados: MercadoPagoLinkRequest):
    """
    Gera checkout PagBank aceitando somente cartão de débito.
    O método DEBIT_CARD depende de liberação/homologação da conta PagBank.
    """
    referencia = (dados.referencia or "").strip()
    if not referencia:
        raise HTTPException(status_code=400, detail="Referência da cobrança não informada.")

    payload = {
        "reference_id": referencia,
        "customer_modifiable": True,
        "items": [
            {
                "reference_id": "bikego_mensalidade",
                "name": "Mensalidade BIKEGO",
                "quantity": 1,
                "unit_amount": int(round(dados.valor * 100)),
            }
        ],
        "payment_methods": [
            {"type": "DEBIT_CARD"},
        ],
        "redirect_url": MERCADOPAGO_BACK_URL,
        "return_url": MERCADOPAGO_BACK_URL,
    }

    resposta = await pagbank_post("/checkouts", payload)
    checkout_id = str(resposta.get("id") or "").strip()
    links = resposta.get("links") or []
    link = next(
        (
            str(item.get("href") or "")
            for item in links
            if str(item.get("rel") or "").upper() == "PAY"
        ),
        "",
    )

    if not checkout_id or not link:
        raise HTTPException(
            status_code=502,
            detail="PagBank não retornou o checkout/link de pagamento.",
        )

    # Guarda o vínculo para permitir consulta segura do status pelo backend.
    db = get_firestore_db()
    db.collection("pagbank_checkouts").document(referencia).set(
        {
            "checkoutId": checkout_id,
            "referencia": referencia,
            "empresa": dados.empresa,
            "valor": dados.valor,
            "metodo": "DEBIT_CARD",
            "criadoEm": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return {
        "ok": True,
        "provedor": "PAGBANK",
        "metodo": "DEBITO",
        "empresa": dados.empresa,
        "valor": dados.valor,
        "referencia": referencia,
        "checkoutId": checkout_id,
        "url": link,
        "pagbank": resposta,
    }


@app.get("/api/pagamentos/pagbank/debito/status/{referencia}")
async def status_pagamento_debito_pagbank(referencia: str):
    db = get_firestore_db()
    doc = db.collection("pagbank_checkouts").document(referencia).get()
    if not doc.exists:
        return {
            "ok": True,
            "provedor": "PAGBANK",
            "metodo": "DEBITO",
            "referencia": referencia,
            "status": "AGUARDANDO_PAGAMENTO",
            "pago": False,
        }

    dados = doc.to_dict() or {}
    checkout_id = str(dados.get("checkoutId") or "").strip()
    if not checkout_id:
        raise HTTPException(status_code=500, detail="Checkout PagBank sem identificador.")

    checkout = await pagbank_get(f"/checkouts/{checkout_id}", params={"limit": 100})

    statuses = []
    def coletar_status(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "status" and isinstance(v, str):
                    statuses.append(v.upper())
                else:
                    coletar_status(v)
        elif isinstance(obj, list):
            for item in obj:
                coletar_status(item)

    coletar_status(checkout)

    pago = "PAID" in statuses
    recusado = any(s in {"DECLINED", "CANCELED", "CANCELLED"} for s in statuses)
    status = "ATIVO" if pago else ("VENCIDO" if recusado else "AGUARDANDO_PAGAMENTO")

    if pago:
        _sincronizar_cliente_firebase(referencia, novo_status="ATIVO")
    elif recusado:
        _sincronizar_cliente_firebase(referencia, novo_status="VENCIDO")

    return {
        "ok": True,
        "provedor": "PAGBANK",
        "metodo": "DEBITO",
        "referencia": referencia,
        "checkoutId": checkout_id,
        "status": status,
        "pago": pago,
        "checkoutStatus": checkout.get("status"),
    }


@app.post("/api/pagamentos/mercadopago/debito/link")
async def criar_link_pagamento_debito_mercadopago(dados: MercadoPagoLinkRequest):
    """
    Gera uma cobrança mensal avulsa no Checkout Pro, priorizando cartão de débito.
    Diferente do cartão de crédito recorrente, esta cobrança não cria assinatura:
    a cada novo mês é necessário gerar uma nova cobrança.
    """
    email = dados.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Informe um e-mail válido do pagador.")

    payload = {
        "items": [
            {
                "id": "bikego_mensalidade",
                "title": "BIKEGO - ARMAZENAMENTO FIREBASE",
                "description": dados.descricao,
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": round(dados.valor, 2),
            }
        ],
        "payer": {"email": email},
        "external_reference": dados.referencia,
        "back_urls": {
            "success": MERCADOPAGO_BACK_URL,
            "pending": MERCADOPAGO_BACK_URL,
            "failure": MERCADOPAGO_BACK_URL,
        },
        "auto_return": "approved",
        "notification_url": MERCADOPAGO_NOTIFICATION_URL,
        "payment_methods": {
            "excluded_payment_types": [
                {"id": "credit_card"},
                {"id": "ticket"},
                {"id": "bank_transfer"},
                {"id": "prepaid_card"},
            ],
            "installments": 1,
        },
        "statement_descriptor": "BIKEGO",
    }

    resposta = await mercadopago_post("/checkout/preferences", payload)
    link = resposta.get("init_point")
    if not link:
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago não retornou o link para pagamento no débito.",
        )

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO",
        "metodo": "DEBITO",
        "empresa": dados.empresa,
        "valor": dados.valor,
        "referencia": dados.referencia,
        "preferenceId": resposta.get("id"),
        "url": link,
        "mercadopago": resposta,
    }


@app.get("/api/pagamentos/mercadopago/debito/status/{referencia}")
async def status_pagamento_debito_mercadopago(referencia: str):
    busca = await mercadopago_get(
        "/v1/payments/search",
        params={
            "external_reference": referencia,
            "sort": "date_created",
            "criteria": "desc",
            "limit": 20,
        },
    )
    resultados = busca.get("results") or []

    if not resultados:
        return {
            "ok": True,
            "provedor": "MERCADO_PAGO",
            "metodo": "DEBITO",
            "referencia": referencia,
            "status": "AGUARDANDO_PAGAMENTO",
            "pago": False,
        }

    aprovado = next(
        (p for p in resultados if str(p.get("status") or "").lower() == "approved"),
        None,
    )
    ultimo = aprovado or resultados[0]
    status_mp = str(ultimo.get("status") or "").lower()

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO",
        "metodo": "DEBITO",
        "referencia": referencia,
        "status": "ATIVO" if aprovado else "AGUARDANDO_PAGAMENTO",
        "pago": bool(aprovado),
        "paymentId": ultimo.get("id"),
        "paymentStatus": status_mp,
        "valor": ultimo.get("transaction_amount"),
    }


@app.get("/api/assinaturas/mercadopago/teste/status/{referencia}")
async def status_assinatura_mercadopago_teste(
    referencia: str,
    email: Optional[str] = None,
):
    params = {"limit": 50}
    if email:
        params["payer_email"] = email.strip().lower()

    busca = await mercadopago_get("/preapproval/search", params=params, teste=True)
    resultados = busca.get("results") or []
    assinatura = next(
        (
            item
            for item in resultados
            if str(item.get("external_reference") or "") == referencia
        ),
        None,
    )

    if not assinatura:
        return {
            "ok": True,
            "provedor": "MERCADO_PAGO_TESTE",
            "referencia": referencia,
            "status": "AGUARDANDO_PAGAMENTO",
            "pago": False,
        }

    status_mp = str(assinatura.get("status") or "").lower()
    pago = status_mp == "authorized"
    vencido = status_mp in {"paused", "cancelled"}
    status = "ATIVO" if pago else ("VENCIDO" if vencido else "AGUARDANDO_PAGAMENTO")

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO_TESTE",
        "referencia": referencia,
        "status": status,
        "pago": pago,
        "subscriptionId": assinatura.get("id"),
        "subscriptionStatus": status_mp,
        "proximaCobranca": _data_br_iso(assinatura.get("next_payment_date")),
        "valor": (assinatura.get("auto_recurring") or {}).get("transaction_amount"),
    }


@app.get("/api/assinaturas/mercadopago/status/{referencia}")
async def status_assinatura_mercadopago(
    referencia: str,
    email: Optional[str] = None,
):
    params = {"limit": 50}
    if email:
        params["payer_email"] = email.strip().lower()

    busca = await mercadopago_get("/preapproval/search", params=params)
    resultados = busca.get("results") or []
    assinatura = next(
        (
            item
            for item in resultados
            if str(item.get("external_reference") or "") == referencia
        ),
        None,
    )

    if not assinatura:
        return {
            "ok": True,
            "provedor": "MERCADO_PAGO",
            "referencia": referencia,
            "status": "AGUARDANDO_PAGAMENTO",
            "pago": False,
        }

    subscription_id = assinatura.get("id")
    status_mp = str(assinatura.get("status") or "").lower()
    proxima = _data_br_iso(assinatura.get("next_payment_date"))

    # "authorized" significa que o cartão/assinatura foi autorizado,
    # mas a primeira parcela ainda pode estar aguardando processamento.
    pago = False
    vencido = status_mp in {"paused", "cancelled"}

    if subscription_id:
        try:
            faturas = await mercadopago_get(
                "/authorized_payments/search",
                params={"preapproval_id": subscription_id},
            )
            items = faturas.get("results") or []
            if items:
                items = sorted(
                    items,
                    key=lambda x: str(x.get("debit_date") or x.get("date_created") or ""),
                    reverse=True,
                )
                ultima = items[0]
                pagamento = ultima.get("payment") or {}
                payment_status = str(pagamento.get("status") or "").lower()
                if payment_status == "approved":
                    pago = True
                    vencido = False
                elif payment_status in {
                    "rejected",
                    "cancelled",
                    "refunded",
                    "charged_back",
                }:
                    pago = False
                    vencido = True
        except HTTPException:
            pass

    if pago:
        status = "ATIVO"
    elif vencido:
        status = "VENCIDO"
    else:
        status = "AGUARDANDO_PAGAMENTO"

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO",
        "referencia": referencia,
        "status": status,
        "pago": pago,
        "subscriptionId": subscription_id,
        "subscriptionStatus": status_mp,
        "proximaCobranca": proxima,
        "valor": (assinatura.get("auto_recurring") or {}).get("transaction_amount"),
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


@app.post("/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    evento = await request.json()
    data = evento.get("data") or {}
    data_id = str(
        request.query_params.get("data.id")
        or request.query_params.get("data_id")
        or data.get("id")
        or ""
    ).strip()

    if not data_id:
        return {"received": True, "ignored": True, "reason": "sem data.id"}

    _validar_webhook_mercadopago(request, data_id)

    tipo = str(evento.get("type") or "").lower()
    ambiente_teste = request.query_params.get("ambiente", "").lower() == "teste"
    referencia = None
    novo_status = None
    vencimento = None
    payment_id = None

    if tipo == "payment":
        pagamento = await mercadopago_get(f"/v1/payments/{data_id}", teste=ambiente_teste)
        referencia = pagamento.get("external_reference")
        payment_id = str(pagamento.get("id") or data_id)
        payment_status = str(pagamento.get("status") or "").lower()

        if payment_status == "approved":
            novo_status = "ATIVO"
        elif payment_status in {
            "rejected",
            "cancelled",
            "refunded",
            "charged_back",
        }:
            novo_status = "VENCIDO"

    elif tipo == "subscription_preapproval":
        assinatura = await mercadopago_get(f"/preapproval/{data_id}", teste=ambiente_teste)
        referencia = assinatura.get("external_reference")
        status_mp = str(assinatura.get("status") or "").lower()
        vencimento = assinatura.get("next_payment_date")

        if status_mp == "authorized":
            # Assinatura autorizada ainda não é confirmação de cobrança.
            # A liberação acontece em subscription_authorized_payment/payment aprovado.
            novo_status = None
        elif status_mp in {"paused", "cancelled"}:
            novo_status = "VENCIDO"

    elif tipo == "subscription_authorized_payment":
        fatura = await mercadopago_get(f"/authorized_payments/{data_id}", teste=ambiente_teste)
        referencia = fatura.get("external_reference")
        vencimento = fatura.get("debit_date")
        pagamento = fatura.get("payment") or {}
        payment_id = str(pagamento.get("id") or "")
        payment_status = str(pagamento.get("status") or "").lower()

        if payment_status == "approved":
            novo_status = "ATIVO"
        elif payment_status in {
            "rejected",
            "cancelled",
            "refunded",
            "charged_back",
        }:
            novo_status = "VENCIDO"
    else:
        return {"received": True, "type": tipo, "ignored": True}

    if not referencia:
        return {
            "received": True,
            "type": tipo,
            "firebaseUpdated": 0,
            "warning": "Evento Mercado Pago sem external_reference.",
        }

    atualizados = _sincronizar_cliente_firebase(
        str(referencia),
        novo_status=novo_status,
        vencimento=vencimento,
        payment_id=payment_id,
    )

    return {
        "received": True,
        "type": tipo,
        "externalReference": referencia,
        "firebaseUpdated": atualizados,
        "statusAplicado": novo_status,
        "vencimentoAplicado": _data_br_iso(vencimento),
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
