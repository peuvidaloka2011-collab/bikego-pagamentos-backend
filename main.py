import calendar
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timedelta, date
from typing import Optional
from zoneinfo import ZoneInfo

import firebase_admin
import httpx
from firebase_admin import credentials, firestore, auth
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "").strip()
MERCADOPAGO_PUBLIC_KEY = os.getenv("MERCADOPAGO_PUBLIC_KEY", "").strip()
MERCADOPAGO_TEST_ACCESS_TOKEN = os.getenv("MERCADOPAGO_TEST_ACCESS_TOKEN", "").strip()
MERCADOPAGO_TEST_PUBLIC_KEY = os.getenv("MERCADOPAGO_TEST_PUBLIC_KEY", "").strip()
# Desabilitado por padrao: previne criacao acidental de assinatura/cobranca.
MERCADOPAGO_TEST_CREATE_ENABLED = os.getenv("MERCADOPAGO_TEST_CREATE_ENABLED", "false").strip().lower() == "true"
MERCADOPAGO_TEST_SELLER_ID = os.getenv("MERCADOPAGO_TEST_SELLER_ID", "").strip()
# Segredos exclusivos da rota controlada de teste; nao reutilize senha da conta.
MERCADOPAGO_TEST_ADMIN_KEY = os.getenv("MERCADOPAGO_TEST_ADMIN_KEY", "").strip()
MERCADOPAGO_TEST_BUYER_EMAIL = os.getenv("MERCADOPAGO_TEST_BUYER_EMAIL", "").strip().lower()
# Liberacao EXCLUSIVA para gerar preferencias avulsas na conta Seller Test User.
# Nao altera as rotas de assinaturas ou a producao.
MERCADOPAGO_AVULSO_TEST_ENABLED = os.getenv("MERCADOPAGO_AVULSO_TEST_ENABLED", "false").strip().lower() == "true"
# V52: fluxo de fatura unica de PRODUCAO - desligado ate homologacao.
BIKEGO_MP_FATURA_UNICA_ENABLED = os.getenv("BIKEGO_MP_FATURA_UNICA_ENABLED", "false").strip().lower() == "true"
BIKEGO_MP_LEGACY_CREATE_ENABLED = os.getenv("BIKEGO_MP_LEGACY_CREATE_ENABLED", "false").strip().lower() == "true"
BIKEGO_ADMIN_UID = os.getenv("BIKEGO_ADMIN_UID", "").strip()
MERCADOPAGO_PRODUCTION_SELLER_ID = os.getenv("MERCADOPAGO_PRODUCTION_SELLER_ID", "").strip()
BIKEGO_FATURAS_CRON_KEY = os.getenv("BIKEGO_FATURAS_CRON_KEY", "").strip()

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


app = FastAPI(title="BIKEGO Pagamentos Backend", version="2.7.4")

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
    device_id: Optional[str] = Field(default=None, max_length=255)


class MercadoPagoOneTimePaymentRequest(BaseModel):
    empresa: str = Field(default="BIKEGO", min_length=2, max_length=120)
    valor: float = Field(gt=0, le=1000)
    referencia: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=5, max_length=180)
    card_token_id: str = Field(min_length=10, max_length=200)
    payment_method_id: str = Field(min_length=2, max_length=50)
    installments: int = Field(default=1, ge=1, le=12)
    issuer_id: Optional[str] = Field(default=None, max_length=50)
    identification_type: Optional[str] = Field(default=None, max_length=20)
    identification_number: Optional[str] = Field(default=None, max_length=30)
    cardholder_name: Optional[str] = Field(default=None, max_length=120)
    device_id: Optional[str] = Field(default=None, max_length=255)
    request_id: str = Field(min_length=8, max_length=80)


class PagamentoAvulsoTesteRequest(BaseModel):
    # Identificador unico fornecido pelo administrador; nunca usar uma referencia real.
    referencia: str = Field(min_length=12, max_length=90)




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

    if teste and token.startswith("TEST-"):
        headers["X-scope"] = "stage"
    # Tokens APP_USR- da conta Seller Test User sao credenciais de
    # producao *dessa conta de teste*. Nao usar X-scope: stage.

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
    extra_headers: Optional[dict] = None,
) -> dict:
    headers = mercadopago_headers(teste)
    if extra_headers:
        headers.update({
            str(k): str(v)
            for k, v in extra_headers.items()
            if v is not None and str(v).strip()
        })

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{MERCADOPAGO_BASE_URL}{path}",
                headers=headers,
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
        "versao": "2.7.4",
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
    """
    Cria o novo Asaas Checkout hospedado para assinatura mensal no cartão.
    O cliente digita os dados do cartão diretamente no Asaas.
    """
    agora_sp = datetime.now(ZoneInfo("America/Sao_Paulo"))
    primeira_cobranca = agora_sp + timedelta(minutes=2)

    callback_base = MERCADOPAGO_BACK_URL.rstrip("/")
    payload = {
        "billingTypes": ["CREDIT_CARD"],
        "chargeTypes": ["RECURRENT"],
        "minutesToExpire": 60,
        "externalReference": dados.referencia,
        "callback": {
            "successUrl": f"{callback_base}/?asaas=sucesso",
            "cancelUrl": f"{callback_base}/?asaas=cancelado",
            "expiredUrl": f"{callback_base}/?asaas=expirado",
        },
        "items": [
            {
                "name": "BIKEGO - ARMAZENAMENTO FIREBASE",
                "description": dados.descricao,
                "quantity": 1,
                "value": round(dados.valor, 2),
            }
        ],
        "subscription": {
            "cycle": "MONTHLY",
            "nextDueDate": primeira_cobranca.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{ASAAS_BASE_URL}/checkouts",
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

    checkout_id = str(resposta.get("id") or "").strip()
    if not checkout_id:
        raise HTTPException(
            status_code=502,
            detail="Asaas não retornou o identificador do novo Checkout.",
        )

    link = str(resposta.get("link") or "").strip()
    if not link:
        base_checkout = (
            "https://asaas.com/checkoutSession/show"
            if ASAAS_ENV == "production"
            else "https://sandbox.asaas.com/checkoutSession/show"
        )
        link = f"{base_checkout}?id={checkout_id}"

    # Mantém a relação entre o Checkout do Asaas e o cliente do BIKEGO.
    # Isso permite tratar CHECKOUT_PAID mesmo se o webhook não repetir
    # externalReference no objeto checkout.
    db = get_firestore_db()
    db.collection("asaas_checkouts").document(checkout_id).set(
        {
            "checkoutId": checkout_id,
            "referencia": dados.referencia or "",
            "empresa": dados.empresa,
            "valor": round(dados.valor, 2),
            "status": str(resposta.get("status") or "ACTIVE"),
            "url": link,
            "ambiente": ASAAS_ENV,
            "criadoEm": firestore.SERVER_TIMESTAMP,
            "atualizadoEm": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    resposta_publica = dict(resposta) if isinstance(resposta, dict) else {}
    resposta_publica["url"] = link

    return {
        "ok": True,
        "empresa": dados.empresa,
        "valor": dados.valor,
        "tipo": "ASAAS_CHECKOUT_RECURRENT_CREDIT_CARD",
        "referencia": dados.referencia,
        "checkoutId": checkout_id,
        "url": link,
        "asaas": resposta_publica,
    }


@app.get("/api/mercadopago/teste/diagnostico")
async def diagnostico_mercadopago_teste():
    """Compara autenticação GET sem realizar cobranças ou expor segredos."""
    if not MERCADOPAGO_TEST_ACCESS_TOKEN:
        return {"ok": False, "configurado": False, "ambiente": "TESTE"}

    tipo = ("TEST-" if MERCADOPAGO_TEST_ACCESS_TOKEN.startswith("TEST-") else
            "APP_USR-" if MERCADOPAGO_TEST_ACCESS_TOKEN.startswith("APP_USR-") else
            "OUTRO")
    resultados = {}
    async with httpx.AsyncClient(timeout=25.0) as client:
        for rotulo, com_stage in (("com_x_scope_stage", True), ("sem_x_scope_stage", False)):
            headers = {
                "Authorization": f"Bearer {MERCADOPAGO_TEST_ACCESS_TOKEN}",
                "Accept": "application/json",
            }
            if com_stage:
                headers["X-scope"] = "stage"
            try:
                r = await client.get(
                    f"{MERCADOPAGO_BASE_URL}/preapproval/search",
                    headers=headers, params={"limit": 1},
                )
                try:
                    corpo = r.json()
                except ValueError:
                    corpo = {}
                resultados[rotulo] = {
                    "status_http": r.status_code,
                    "autorizado": 200 <= r.status_code < 300,
                    "codigo_erro": corpo.get("code") if isinstance(corpo, dict) else None,
                    "bloqueado_por": corpo.get("blocked_by") if isinstance(corpo, dict) else None,
                }
            except httpx.RequestError as e:
                resultados[rotulo] = {"erro_rede": type(e).__name__}

    return {
        "ok": any(v.get("autorizado") for v in resultados.values()),
        "configurado": True,
        "ambiente": "TESTE",
        "tipo_token": tipo,
        "diagnostico": resultados,
        "observacao": "GET autorizado não garante permissão para POST /preapproval.",
    }


def _modo_credencial_mp(valor: str) -> str:
    v = (valor or "").strip().upper()
    if not v:
        return "NAO_CONFIGURADA"
    if v.startswith("TEST-"):
        return "TESTE"
    if v.startswith("APP_USR-"):
        return "PRODUCAO"
    return "DESCONHECIDO"


def _mascarar_email(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return ""
    nome, dominio = e.split("@", 1)
    if not nome:
        return f"***@{dominio}"
    return f"{nome[:1]}***@{dominio}"


@app.get("/api/mercadopago/diagnostico")
async def diagnostico_mercadopago():
    modo_public = _modo_credencial_mp(MERCADOPAGO_PUBLIC_KEY)
    modo_token = _modo_credencial_mp(MERCADOPAGO_ACCESS_TOKEN)

    vendedor = {}
    if MERCADOPAGO_ACCESS_TOKEN:
        vendedor = await mercadopago_usuario_atual()

    mesmo_ambiente = (
        modo_public in {"TESTE", "PRODUCAO"}
        and modo_token in {"TESTE", "PRODUCAO"}
        and modo_public == modo_token
    )

    assinaturas_api = "NAO_TESTADA"
    resultados = 0
    detalhe_assinaturas = None

    if MERCADOPAGO_ACCESS_TOKEN:
        try:
            resposta = await mercadopago_get(
                "/preapproval/search",
                params={"limit": 1},
            )
            assinaturas_api = "AUTORIZADA"
            resultados = len(resposta.get("results") or [])
        except HTTPException as exc:
            assinaturas_api = "NAO_AUTORIZADA"
            detalhe_assinaturas = exc.detail

    alertas = []
    if modo_public == "NAO_CONFIGURADA":
        alertas.append("MERCADOPAGO_PUBLIC_KEY não configurada.")
    if modo_token == "NAO_CONFIGURADA":
        alertas.append("MERCADOPAGO_ACCESS_TOKEN não configurado.")
    if (
        modo_public in {"TESTE", "PRODUCAO"}
        and modo_token in {"TESTE", "PRODUCAO"}
        and modo_public != modo_token
    ):
        alertas.append(
            "Public Key e Access Token estão em ambientes diferentes."
        )
    if MERCADOPAGO_ACCESS_TOKEN and not vendedor:
        alertas.append(
            "O Access Token não conseguiu identificar a conta vendedora."
        )
    if assinaturas_api != "AUTORIZADA":
        alertas.append(
            "A API de assinaturas não está autorizada com o Access Token atual."
        )

    return {
        "ok": True,
        "publicKeyConfigurada": bool(MERCADOPAGO_PUBLIC_KEY),
        "accessTokenConfigurado": bool(MERCADOPAGO_ACCESS_TOKEN),
        "ambientePublicKey": modo_public,
        "ambienteAccessToken": modo_token,
        "mesmoAmbiente": mesmo_ambiente,
        "assinaturasApi": assinaturas_api,
        "resultados": resultados,
        "vendedorReconhecido": bool(vendedor),
        "vendedorId": vendedor.get("id") if vendedor else None,
        "vendedorEmailMascarado": _mascarar_email(
            str(vendedor.get("email") or "")
        ) if vendedor else None,
        "alertas": alertas,
        "detail": detalhe_assinaturas,
        "observacao": (
            "Nenhuma credencial secreta é exibida. "
            "Se Public Key e Access Token estiverem em PRODUCAO, "
            "ainda confirme no painel Mercado Pago que pertencem à mesma aplicação."
        ),
    }


async def _verificar_conta_vendedora_teste() -> dict:
    """Confere titular do token de TESTE no provedor; nunca utiliza token real."""
    if not MERCADOPAGO_TEST_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="Token do vendedor de teste ausente.")
    if not MERCADOPAGO_TEST_SELLER_ID or not MERCADOPAGO_TEST_SELLER_ID.isdigit():
        raise HTTPException(status_code=503, detail="Configure MERCADOPAGO_TEST_SELLER_ID no Render.")
    if MERCADOPAGO_TEST_ACCESS_TOKEN == MERCADOPAGO_ACCESS_TOKEN:
        raise HTTPException(status_code=403, detail="Token de teste coincide com producao. Operacao bloqueada.")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                "https://api.mercadolibre.com/users/me",
                headers={"Authorization": f"Bearer {MERCADOPAGO_TEST_ACCESS_TOKEN}"},
            )
        response.raise_for_status()
        usuario = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Nao foi possivel confirmar titular do token de teste.") from exc
    if str(usuario.get("id") or "") != MERCADOPAGO_TEST_SELLER_ID:
        raise HTTPException(status_code=403, detail="ID do vendedor nao corresponde a Seller Test User autorizada.")
    return {"seller_id_confirmado": True, "conta_testada": True}


@app.get("/api/mercadopago/teste/validar-vendedor")
async def validar_vendedor_mercadopago_teste():
    """Verifica somente a identidade do token de teste, sem criar cobranca.

    Nao retorna dados pessoais, token, nem o ID do titular.
    """
    if not MERCADOPAGO_TEST_ACCESS_TOKEN or not MERCADOPAGO_TEST_SELLER_ID:
        return {"ok": False, "seller_confirmado": False,
                "motivo": "Configure token e Seller ID de teste no Render."}
    if MERCADOPAGO_TEST_ACCESS_TOKEN == MERCADOPAGO_ACCESS_TOKEN:
        return {"ok": False, "seller_confirmado": False,
                "motivo": "Token de teste igual ao de producao. Verificacao bloqueada."}
    if not MERCADOPAGO_TEST_SELLER_ID.isdigit():
        return {"ok": False, "seller_confirmado": False,
                "motivo": "Seller ID de teste invalido."}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resposta = await client.get(
                "https://api.mercadolibre.com/users/me",
                headers={"Authorization": f"Bearer {MERCADOPAGO_TEST_ACCESS_TOKEN}"},
            )
    except httpx.TimeoutException:
        return {"ok": False, "seller_confirmado": False,
                "motivo": "Timeout ao consultar identificacao do vendedor."}
    except httpx.HTTPError:
        return {"ok": False, "seller_confirmado": False,
                "motivo": "Falha de conexao com a API de identificacao."}
    if resposta.status_code != 200:
        return {"ok": False, "seller_confirmado": False,
                "status_code": resposta.status_code,
                "motivo": "API de identificacao recusou a consulta."}
    try:
        usuario = resposta.json()
        identificado = str(usuario.get("id") or "")
    except (ValueError, AttributeError):
        return {"ok": False, "seller_confirmado": False,
                "motivo": "Resposta de identificacao invalida."}
    corresponde = bool(identificado) and hmac.compare_digest(
        identificado, MERCADOPAGO_TEST_SELLER_ID
    )
    return {
        "ok": corresponde,
        "seller_confirmado": corresponde,
        "credenciais_distintas": True,
        "criacao_habilitada": MERCADOPAGO_TEST_CREATE_ENABLED,
        "motivo": "Seller confirmado" if corresponde else "Seller ID nao corresponde ao token.",
        "aviso": "Somente consulta. Nenhuma cobranca foi criada.",
    }


@app.get("/api/mercadopago/teste/preparacao")
async def preparacao_assinatura_mp_teste():
    """Leitura pura: nao cria assinatura, nao altera Firestore, nao revela segredos."""
    return {
        "ok": True,
        "criacao_habilitada": MERCADOPAGO_TEST_CREATE_ENABLED,
        "chave_admin_configurada": len(MERCADOPAGO_TEST_ADMIN_KEY) >= 32,
        "buyer_email_configurado": bool(MERCADOPAGO_TEST_BUYER_EMAIL),
        "seller_id_configurado": bool(MERCADOPAGO_TEST_SELLER_ID),
        "token_de_teste_configurado": bool(MERCADOPAGO_TEST_ACCESS_TOKEN),
        "credenciais_distintas": bool(MERCADOPAGO_TEST_ACCESS_TOKEN) and
            MERCADOPAGO_TEST_ACCESS_TOKEN != MERCADOPAGO_ACCESS_TOKEN,
        "cabecalho_stage": bool(MERCADOPAGO_TEST_ACCESS_TOKEN.startswith("TEST-")),
        "aviso": "Nenhum pagamento ou assinatura foi criado. Habilite somente com Seller Test User validado e compradores de teste.",
    }


@app.get("/api/mercadopago/teste/preflight")
async def preflight_assinatura_teste(
    x_bikego_test_admin_key: Optional[str] = Header(default=None),
):
    """Validacao local, sem chamar POST e sem criar assinatura."""
    if not MERCADOPAGO_TEST_ADMIN_KEY or not x_bikego_test_admin_key or not hmac.compare_digest(
        x_bikego_test_admin_key, MERCADOPAGO_TEST_ADMIN_KEY
    ):
        raise HTTPException(status_code=401, detail="Autorizacao administrativa necessaria.")
    from urllib.parse import urlparse
    parsed = urlparse(MERCADOPAGO_BACK_URL)
    checks = {
        "seller_id_configurado": bool(MERCADOPAGO_TEST_SELLER_ID),
        "token_teste_configurado": bool(MERCADOPAGO_TEST_ACCESS_TOKEN),
        "tokens_distintos": bool(MERCADOPAGO_TEST_ACCESS_TOKEN) and MERCADOPAGO_TEST_ACCESS_TOKEN != MERCADOPAGO_ACCESS_TOKEN,
        "buyer_email_formato_valido": bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", MERCADOPAGO_TEST_BUYER_EMAIL)),
        "buyer_email_dominio_teste": MERCADOPAGO_TEST_BUYER_EMAIL.endswith("@testuser.com"),
        "back_url_https": parsed.scheme == "https" and bool(parsed.netloc),
        "criacao_habilitada": MERCADOPAGO_TEST_CREATE_ENABLED,
    }
    return {"ok": all(checks.values()), "verificacoes": checks,
            "nota": "Checagem local: nao confirma cadastro do comprador nem permissao para POST /preapproval. Nenhuma assinatura criada."}


@app.get("/api/mercadopago/teste/diagnostico-400")
async def diagnostico_400_sem_cobranca(
    x_bikego_test_admin_key: Optional[str] = Header(default=None),
):
    """Leitura apenas; nao cria assinatura e nao expoe comprador/segredos.

    Confere cabecalhos e estrutura da requisicao; tenta consultar a lista de
    assinaturas na conta Seller Test User para ajudar a detectar duplicidade.
    Uma consulta autorizada nao garante que POST /preapproval sera aceito.
    """
    if (len(MERCADOPAGO_TEST_ADMIN_KEY) < 32 or not x_bikego_test_admin_key
            or not hmac.compare_digest(x_bikego_test_admin_key, MERCADOPAGO_TEST_ADMIN_KEY)):
        raise HTTPException(status_code=401, detail="Autorizacao administrativa necessaria.")
    from urllib.parse import urlparse
    retorno = urlparse(MERCADOPAGO_BACK_URL)
    cabecalhos = mercadopago_headers(teste=True)
    esperado = {
        "reason": "BIKEGO - TESTE DE ASSINATURA",
        "external_reference": "TESTE-BIKEGO-006",
        "payer_email": MERCADOPAGO_TEST_BUYER_EMAIL,
        "auto_recurring": {
            "frequency": 1, "frequency_type": "months",
            "transaction_amount": 5.0, "currency_id": "BRL"
        },
        "back_url": MERCADOPAGO_BACK_URL,
        "status": "pending",
    }
    resumo = {
        "metodo": "POST",
        "endpoint": "/preapproval",
        "valor": esperado["auto_recurring"]["transaction_amount"],
        "frequencia": "mensal",
        "status_inicial": "pending",
        "email_comprador_oculto": True,
        "email_formato_valido": bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", esperado["payer_email"])),
        "retorno_https": retorno.scheme == "https" and bool(retorno.netloc),
        "x_scope_stage_enviado": "X-scope" in cabecalhos,
        "token_teste_diferente_do_real": bool(MERCADOPAGO_TEST_ACCESS_TOKEN)
            and MERCADOPAGO_TEST_ACCESS_TOKEN != MERCADOPAGO_ACCESS_TOKEN,
        "identidade_do_comprador_verificada_pela_api": False,
    }
    # Nao exponha emails, IDs de assinaturas ou dados privados no endpoint.
    consulta = {"executada": False, "autorizada": False}
    try:
        resposta = await mercadopago_get(
            "/preapproval/search", params={"limit": 10, "offset": 0}, teste=True)
        consulta = {
            "executada": True,
            "autorizada": True,
            "total_reportado": resposta.get("paging", {}).get("total")
                if isinstance(resposta.get("paging"), dict) else None,
            "observacao": "Consulta de leitura; nenhum registro criado."
        }
    except HTTPException as exc:
        detalhe = exc.detail if isinstance(exc.detail, dict) else {}
        consulta = {
            "executada": True, "autorizada": False,
            "status_http": exc.status_code,
            "codigo": str(detalhe.get("code") or detalhe.get("error") or "")[:60]
        }
    return {
        "ok": True,
        "sem_cobranca": True,
        "parametros_seguros": resumo,
        "consulta_assinaturas": consulta,
        "conclusao": "O erro 400 ainda nao identifica o campo rejeitado. Nenhum POST foi feito."
    }


@app.post("/api/assinaturas/mercadopago/teste/link")
async def criar_link_assinatura_mercadopago_teste(
    dados: MercadoPagoLinkRequest,
    x_bikego_test_admin_key: Optional[str] = Header(default=None),
):
    """Cria SOMENTE assinatura pendente no Seller Test User validado.

    A rota exige habilitacao no Render, segredo administrativo separado e
    email exato do comprador de teste. Nenhum cartao e coletado ou cobrado.
    """
    if not MERCADOPAGO_TEST_CREATE_ENABLED:
        raise HTTPException(status_code=403, detail="Criacao de teste desabilitada.")
    if len(MERCADOPAGO_TEST_ADMIN_KEY) < 32:
        raise HTTPException(status_code=503, detail="Configure MERCADOPAGO_TEST_ADMIN_KEY com pelo menos 32 caracteres.")
    if not x_bikego_test_admin_key or not hmac.compare_digest(
        x_bikego_test_admin_key, MERCADOPAGO_TEST_ADMIN_KEY
    ):
        raise HTTPException(status_code=401, detail="Autorizacao administrativa necessaria.")
    if not MERCADOPAGO_TEST_BUYER_EMAIL or "@" not in MERCADOPAGO_TEST_BUYER_EMAIL:
        raise HTTPException(status_code=503, detail="Configure MERCADOPAGO_TEST_BUYER_EMAIL da conta Buyer Test User.")
    if MERCADOPAGO_TEST_ACCESS_TOKEN == MERCADOPAGO_ACCESS_TOKEN:
        raise HTTPException(status_code=403, detail="Credenciais de teste e producao coincidem.")
    if not dados.referencia.strip().upper().startswith("TESTE-"):
        raise HTTPException(status_code=400, detail="Referencia deve iniciar por TESTE-.")
    if abs(float(dados.valor) - 5.0) > 0.0001:
        raise HTTPException(status_code=400, detail="Valor permitido nesta rota: R$ 5,00.")
    if not hmac.compare_digest(dados.email.strip().lower(), MERCADOPAGO_TEST_BUYER_EMAIL):
        raise HTTPException(status_code=400, detail="Comprador diferente do Buyer Test User configurado.")
    await _verificar_conta_vendedora_teste()

    payload = {
        "reason": "BIKEGO - TESTE DE ASSINATURA",
        "external_reference": dados.referencia.strip(),
        "payer_email": MERCADOPAGO_TEST_BUYER_EMAIL,
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": 5.0,
            "currency_id": "BRL",
        },
        "back_url": MERCADOPAGO_BACK_URL,
        "status": "pending",
        # Nenhum webhook e enviado ao sistema de licencas real nesta etapa.
    }
    # Mesmo com status pending, o recurso e criado no Mercado Pago.
    # A conclusao do checkout sera uma etapa separada.
    try:
        resposta = await mercadopago_post("/preapproval", payload, teste=True)
    except HTTPException as exc:
        # Nao retorna informacoes do token, comprador ou payload ao navegador.
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        cause = detail.get("cause") if isinstance(detail.get("cause"), list) else []
        causas = []
        for item in cause[:5]:
            if isinstance(item, dict):
                causas.append({"code": str(item.get("code", ""))[:80],
                               "description": str(item.get("description", ""))[:180]})
        raise HTTPException(status_code=exc.status_code, detail={
            "origem": "mercado_pago",
            "status": exc.status_code,
            "codigo": str(detail.get("error") or detail.get("code") or "")[:80],
            "mensagem": str(detail.get("message") or "Requisicao rejeitada")[:180],
            "causas": causas,
            "acao": "Nao repita Execute ate investigar o motivo. Use /api/mercadopago/teste/preflight antes de nova tentativa."
        }) from exc
    return {
        "ok": True,
        "ambiente": "SELLER_TEST_USER",
        "status": resposta.get("status"),
        "subscriptionId": resposta.get("id"),
        "url": resposta.get("init_point"),
        "valor": 5.0,
        "referencia": dados.referencia.strip(),
        "aviso": "Assinatura pendente criada; nao autorize pagamento sem conferir a conta de teste.",
    }


# ----------------------------------------------------------------------
# BIKEGO V52 — FATURA MENSAL AVULSA PRODUTIVA / CHECKOUT PRO
# PROTEGIDO: requer usuario Firebase com UID explicitamente autorizado.
# NENHUM acesso a licenca ocorre por retorno de navegador.
# ----------------------------------------------------------------------

def _v52_agora():
    return datetime.now(ZoneInfo("America/Sao_Paulo"))


def _v52_data(texto):
    valor = str(texto or "").strip()
    try:
        return datetime.strptime(valor[:10], "%d/%m/%Y").date()
    except ValueError:
        return None


def _v52_proximo_mes(data_base):
    ano = data_base.year + (1 if data_base.month == 12 else 0)
    mes = 1 if data_base.month == 12 else data_base.month + 1
    dia = min(data_base.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def _v52_id_fatura(cliente_id, periodo):
    chave = hashlib.sha256(cliente_id.encode("utf-8")).hexdigest()[:20].upper()
    return f"BIKEGO-FAT-{chave}-{periodo}"


def _v52_admin(authorization):
    if not BIKEGO_ADMIN_UID:
        raise HTTPException(503, "BIKEGO_ADMIN_UID nao configurado: operacao bloqueada.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Autenticacao Firebase necessaria.")
    try:
        decoded = auth.verify_id_token(authorization.split(" ", 1)[1].strip(), check_revoked=True)
    except Exception:
        raise HTTPException(401, "Sessao Firebase invalida ou expirada.")
    if str(decoded.get("uid") or "") != BIKEGO_ADMIN_UID:
        raise HTTPException(403, "Usuario nao autorizado a emitir faturas.")
    return decoded


def _v52_operacional():
    if (not MERCADOPAGO_ACCESS_TOKEN
            or not MERCADOPAGO_PRODUCTION_SELLER_ID.isdigit()
            or not _firebase_configurado()):
        raise HTTPException(503, "Configuracao de producao incompleta: token, Seller ID ou Firebase.")


def _v52_habilitado():
    _v52_operacional()
    if not BIKEGO_MP_FATURA_UNICA_ENABLED:
        raise HTTPException(403, "Emissao de fatura avulsa desabilitada no Render.")


def _v52_pagar_seguro(dados, fatura):
    referencia = str(dados.get("external_reference") or "")
    cents = round(float(dados.get("transaction_amount") or 0) * 100)
    expected = int(fatura.get("valorCentavos") or 0)
    collector_id = str(dados.get("collector_id") or (dados.get("collector") or {}).get("id") or "")
    return (referencia == fatura.get("referencia") and cents == expected
            and collector_id == MERCADOPAGO_PRODUCTION_SELLER_ID
            and str(dados.get("currency_id") or "BRL").upper() == "BRL")


class V52NovaFaturaRequest(BaseModel):
    cliente_id: str = Field(min_length=4, max_length=200)


@app.get("/api/v52/faturas/preparacao")
def v52_preparacao():
    # Diagnostico sem segredos; para homologacao inicial.
    return {"ok": True, "habilitado": BIKEGO_MP_FATURA_UNICA_ENABLED,
            "admin_uid_configurado": bool(BIKEGO_ADMIN_UID),
            "seller_producao_configurado": bool(MERCADOPAGO_PRODUCTION_SELLER_ID),
            "webhook_secret_configurado": bool(MERCADOPAGO_WEBHOOK_SECRET),
            "firebase_configurado": _firebase_configurado(),
            "fluxo": "Checkout Pro pagamento unico",
            "aviso": "Esta consulta nao cria pagamentos nem libera licencas."}


@app.post("/api/v52/faturas/mercadopago/gerar")
async def v52_gerar_fatura(dados: V52NovaFaturaRequest, authorization: Optional[str] = Header(default=None)):
    _v52_admin(authorization)
    _v52_habilitado()
    if not MERCADOPAGO_WEBHOOK_SECRET:
        raise HTTPException(503, "Configure MERCADOPAGO_WEBHOOK_SECRET antes de cobrar clientes reais.")
    db = get_firestore_db()
    cliente_ref = db.collection("clientes").document(dados.cliente_id)
    cliente_snap = cliente_ref.get()
    if not cliente_snap.exists:
        raise HTTPException(404, "Cliente nao encontrado no Firebase.")
    cliente = cliente_snap.to_dict() or {}
    prov = str(cliente.get("provedorPagamento") or "").upper()
    if not prov.startswith("MERCADO_PAGO"):
        raise HTTPException(409, "Cliente nao esta configurado para Mercado Pago.")
    ref_anterior = str(cliente.get("referenciaAssinatura") or "").strip()
    if ref_anterior and not ref_anterior.startswith("BIKEGO-FAT-"):
        raise HTTPException(409, "Cliente possui referencia de cobranca antiga. Verifique/cancele a assinatura anterior e conclua a migracao antes de emitir fatura unica.")
    if not cliente.get("aplicativosContratados") and not cliente.get("app"):
        raise HTTPException(400, "Cadastre ao menos um aplicativo no cliente.")
    try:
        cents = round(float(cliente.get("valor") or 0) * 100)
    except (ValueError, TypeError):
        cents = 0
    if not (100 <= cents <= 10000000):
        raise HTTPException(400, "Valor do cliente invalido: R$ 1,00 a R$ 100.000,00.")
    email = str(cliente.get("emailPagamento") or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Cadastre um e-mail valido no cliente.")
    vencimento = _v52_data(cliente.get("vencimento")) or _v52_agora().date()
    # A fatura corresponde ao mes da data de vencimento cadastrada.
    periodo = vencimento.strftime("%Y%m")
    referencia = _v52_id_fatura(dados.cliente_id, periodo)
    fatura_ref = db.collection("faturas_mp").document(referencia)
    fatura_snap = fatura_ref.get()
    if fatura_snap.exists:
        existente = fatura_snap.to_dict() or {}
        if existente.get("status") == "PAGO":
            return {"ok": True, "ja_paga": True, "referencia": referencia,
                    "status": "PAGO", "url": None,
                    "mensagem": "Fatura ja paga. Nao efetue novo pagamento."}
        if existente.get("checkoutUrl"):
            return {"ok": True, "reutilizada": True,
                    "referencia": referencia, "status": existente.get("status"),
                    "url": existente.get("checkoutUrl"), "valor": cents / 100}
        # Uma primeira chamada pode ter enviado preferencia antes de perder conexao.
        # Nao criar nova ate reconciliar essa tentativa.
        raise HTTPException(409, "Fatura ja reservada e sem link. Confira a preferencia no Mercado Pago; nao gere duplicata.")

    # Reserva atomica para impedir duas preferencias do mesmo periodo.
    # create() falha se outro administrador ja reservou o documento.
    try:
        fatura_ref.create({
            "clienteId": dados.cliente_id, "referencia": referencia,
            "periodo": periodo, "vencimentoFatura": vencimento.strftime("%d/%m/%Y"),
            "valorCentavos": cents, "emailPagamento": email,
            "status": "CRIANDO", "provedor": "MERCADO_PAGO_AVULSO",
            "criadaEm": firestore.SERVER_TIMESTAMP,
        })
    except Exception:
        raise HTTPException(409, "Fatura ja esta sendo criada. Consulte o cliente antes de repetir.")

    payload = {
        "items": [{"id": referencia, "title": "BIKEGO - Mensalidade de aplicativos",
                   "description": f"Fatura {periodo} - Servicos BIKEGO",
                   "quantity": 1, "currency_id": "BRL", "unit_price": cents / 100}],
        "payer": {"email": email},
        "external_reference": referencia,
        "back_urls": {
            "success": "https://bikego-pagamentos-web.web.app/",
            "failure": "https://bikego-pagamentos-web.web.app/",
            "pending": "https://bikego-pagamentos-web.web.app/",
        },
        "notification_url": MERCADOPAGO_NOTIFICATION_URL,
        "expires": True,
        "expiration_date_from": _v52_agora().isoformat(timespec="seconds"),
        "expiration_date_to": (_v52_agora() + timedelta(days=40)).isoformat(timespec="seconds"),
    }
    try:
        pref = await mercadopago_post("/checkout/preferences", payload,
                                    extra_headers={"X-Idempotency-Key": hashlib.sha256(referencia.encode()).hexdigest()})
    except HTTPException:
        fatura_ref.set({"status": "ERRO_CRIACAO", "revisaoManual": True,
                        "atualizadaEm": firestore.SERVER_TIMESTAMP}, merge=True)
        raise HTTPException(502, "Nao foi possivel gerar a fatura. Revise no Mercado Pago antes de tentar novamente.")
    url = str(pref.get("init_point") or "")
    if not url.startswith("https://"):
        fatura_ref.set({"status": "SEM_LINK", "revisaoManual": True}, merge=True)
        raise HTTPException(502, "Mercado Pago nao forneceu link HTTPS. Confira a preferencia.")
    # Atualizacao de links nao libera nem bloqueia apps existentes.
    fatura_ref.set({"status": "PENDENTE", "checkoutUrl": url,
                    "preferenciaId": str(pref.get("id") or ""),
                    "atualizadaEm": firestore.SERVER_TIMESTAMP}, merge=True)
    cliente_ref.set({"referenciaAssinatura": referencia,
                     "linkAssinatura": url, "provedorPagamento": "MERCADO_PAGO_AVULSO",
                     "metodoPagamento": "CREDITO", "atualizadoEm": firestore.SERVER_TIMESTAMP}, merge=True)
    return {"ok": True, "referencia": referencia, "url": url,
            "valor": cents / 100, "status": "PENDENTE", "tipo": "PAGAMENTO_UNICO",
            "aviso": "Fatura emitida; cliente so sera liberado apos pagamento aprovado."}


async def _v52_conciliar_pagamento(payment_id):
    # Pagamentos existentes continuam conciliaveis mesmo com emissao desligada.
    _v52_operacional()
    pagamento = await mercadopago_get(f"/v1/payments/{payment_id}")
    ref = str(pagamento.get("external_reference") or "")
    if not re.fullmatch(r"BIKEGO-FAT-[0-9A-F]{20}-\d{6}", ref):
        raise HTTPException(400, "Pagamento nao pertence a uma fatura V52.")
    db = get_firestore_db()
    fatura_ref = db.collection("faturas_mp").document(ref)
    doc = fatura_ref.get()
    if not doc.exists:
        raise HTTPException(404, "Fatura desconhecida; cliente nao foi liberado.")
    fatura = doc.to_dict() or {}
    if not _v52_pagar_seguro(pagamento, fatura):
        raise HTTPException(409, "Referencia, valor ou vendedor divergente; sem liberacao.")
    novo_status = str(pagamento.get("status") or "").lower()
    if novo_status == "approved":
        if fatura.get("status") == "PAGO":
            return {"ok": True, "status": "PAGO", "ja_processado": True}
        cliente_id = fatura.get("clienteId")
        cliente_ref = db.collection("clientes").document(cliente_id)
        snap = cliente_ref.get()
        if not snap.exists or str((snap.to_dict() or {}).get("provedorPagamento") or "") != "MERCADO_PAGO_AVULSO":
            raise HTTPException(409, "Cliente sem vinculo de faturamento V52.")
        # Nao permitir pagamento antigo alterar uma fatura mais nova do cliente.
        cliente_atual = snap.to_dict() or {}
        if str(cliente_atual.get("referenciaAssinatura") or "") != ref:
            raise HTTPException(409, "Fatura nao e a referencia atual do cliente.")
        base = _v52_data(fatura.get("vencimentoFatura")) or _v52_agora().date()
        seguinte = _v52_proximo_mes(base).strftime("%d/%m/%Y")
        atualizados = _sincronizar_cliente_firebase(ref, novo_status="ATIVO",
                                                     vencimento=seguinte, payment_id=str(payment_id))
        if atualizados < 1:
            raise HTTPException(409, "Pagamento confirmado, mas nao foi encontrada licenca do cliente. Reconciliacao necessaria.")
        # Se a gravação da licença falhar, a fatura continua conciliável.
        fatura_ref.set({"status": "PAGO", "paymentId": str(payment_id),
                        "pagaEm": firestore.SERVER_TIMESTAMP,
                        "atualizadaEm": firestore.SERVER_TIMESTAMP}, merge=True)
        return {"ok": True, "status": "PAGO", "clienteLiberado": True,
                "vencimento": seguinte}
    if novo_status in {"refunded", "charged_back"}:
        cli = get_firestore_db().collection("clientes").document(str(fatura.get("clienteId") or "")).get()
        if not cli.exists or str((cli.to_dict() or {}).get("referenciaAssinatura") or "") != ref:
            raise HTTPException(409, "Fatura antiga nao pode reverter licenca atual.")
        if fatura.get("status") == "PAGO":
            fatura_ref.set({"status": "ESTORNADO", "statusMp": novo_status}, merge=True)
            _sincronizar_cliente_firebase(ref, novo_status="BLOQUEADO", payment_id=str(payment_id))
        return {"ok": True, "status": "ESTORNADO"}
    fatura_ref.set({"statusMp": novo_status,
                    "atualizadaEm": firestore.SERVER_TIMESTAMP}, merge=True)
    return {"ok": True, "status": novo_status.upper(), "clienteLiberado": False}


class V52ConciliaRequest(BaseModel):
    payment_id: str = Field(min_length=5, max_length=30)


@app.post("/api/v52/faturas/mercadopago/conciliar")
async def v52_conciliar_admin(dados: V52ConciliaRequest,
                              authorization: Optional[str] = Header(default=None)):
    _v52_admin(authorization)
    if not dados.payment_id.isdigit():
        raise HTTPException(400, "ID de pagamento invalido.")
    return await _v52_conciliar_pagamento(dados.payment_id)


@app.get("/api/v52/faturas/mercadopago/status/{cliente_id}")
async def v52_status_cliente(cliente_id: str,
                              authorization: Optional[str] = Header(default=None)):
    _v52_admin(authorization)
    db = get_firestore_db()
    cliente = db.collection("clientes").document(cliente_id).get()
    if not cliente.exists:
        raise HTTPException(404, "Cliente nao encontrado.")
    data = cliente.to_dict() or {}
    ref = str(data.get("referenciaAssinatura") or "")
    inv = db.collection("faturas_mp").document(ref).get() if ref.startswith("BIKEGO-FAT-") else None
    fatura = (inv.to_dict() or {}) if inv is not None and inv.exists else {}
    # Consulta sem alterar licencas. Webhook / conciliacao confirmam pagamentos.
    return {"ok": True, "clienteId": cliente_id,
            "status": data.get("status", "AGUARDANDO PAGAMENTO"),
            "vencimento": data.get("vencimento"), "referencia": ref,
            "faturaStatus": fatura.get("status"), "valor": (fatura.get("valorCentavos") or 0) / 100,
            "pago": fatura.get("status") == "PAGO"}


@app.post("/api/v52/faturas/conciliar-pendentes")
async def v52_conciliar_pendentes(authorization: Optional[str] = Header(default=None)):
    _v52_admin(authorization)
    _v52_operacional()
    db = get_firestore_db()
    refs = list(db.collection("faturas_mp").where("status", "==", "PENDENTE").limit(50).stream())
    saida = {"consultadas": 0, "pagas": 0, "erros": 0}
    for inv in refs:
        ref = inv.id
        try:
            pagamentos = await mercadopago_get("/v1/payments/search", params={"external_reference": ref, "limit": 30})
            saida["consultadas"] += 1
            for p in pagamentos.get("results") or []:
                if str(p.get("status") or "").lower() != "approved":
                    continue
                result = await _v52_conciliar_pagamento(str(p.get("id")))
                if result.get("status") == "PAGO":
                    saida["pagas"] += 1
                    break
        except Exception:
            saida["erros"] += 1
    return saida


@app.post("/api/v52/faturas/cron")
async def v52_cron(x_bikego_cron_key: Optional[str] = Header(default=None)):
    if (len(BIKEGO_FATURAS_CRON_KEY) < 32 or not x_bikego_cron_key
            or not hmac.compare_digest(x_bikego_cron_key, BIKEGO_FATURAS_CRON_KEY)):
        raise HTTPException(401, "Rotina nao autorizada.")
    _v52_operacional()
    db = get_firestore_db()
    hoje = _v52_agora().date()
    bloqueados = 0
    conciliadas = 0
    erros_conciliacao = 0
    # Recuperacao automatica caso uma notificacao Webhook nao chegue.
    pendentes = list(db.collection("faturas_mp").where("status", "==", "PENDENTE").limit(100).stream())
    for fatura_doc in pendentes:
        try:
            busca = await mercadopago_get("/v1/payments/search", params={
                "external_reference": fatura_doc.id, "limit": 30})
            for item in busca.get("results") or []:
                if item.get("status") != "approved":
                    continue
                r = await _v52_conciliar_pagamento(str(item.get("id")))
                if r.get("status") == "PAGO":
                    conciliadas += 1
                    break
        except Exception:
            erros_conciliacao += 1
    # Limite de seguranca; para volumes maiores, adicionar paginacao.
    docs = list(db.collection("clientes").where("provedorPagamento", "==", "MERCADO_PAGO_AVULSO").limit(100).stream())
    for cli in docs:
        dados = cli.to_dict() or {}
        venc = _v52_data(dados.get("vencimento"))
        if (venc and hoje > venc + timedelta(days=2)
                and str(dados.get("status") or "").upper() == "ATIVO"):
            ref = str(dados.get("referenciaAssinatura") or "")
            _sincronizar_cliente_firebase(ref, novo_status="BLOQUEADO")
            bloqueados += 1
    return {"ok": True, "clientesVerificados": len(docs), "bloqueados": bloqueados,
            "faturasConciliadas": conciliadas, "errosConciliacao": erros_conciliacao,
            "aviso": "Agende esta rota diariamente no Render; sem agendamento nao ha conciliacao e bloqueio periodicos."}


# ----------------------------------------------------------------------
# BIKEGO V51 — COBRANCA UNICA / CHECKOUT PRO (SOMENTE CONTA DE TESTE)
# Nenhuma rota abaixo altera clientes ou licencas no Firebase.
# ----------------------------------------------------------------------

def _proteger_avulso_teste(chave: Optional[str]) -> None:
    if not MERCADOPAGO_AVULSO_TEST_ENABLED:
        raise HTTPException(status_code=403, detail="Teste avulso desativado no Render.")
    if not MERCADOPAGO_TEST_ADMIN_KEY or len(MERCADOPAGO_TEST_ADMIN_KEY) < 32:
        raise HTTPException(status_code=503, detail="Configure uma chave administrativa de teste com 32 caracteres ou mais.")
    if not chave or not hmac.compare_digest(chave, MERCADOPAGO_TEST_ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Chave administrativa de teste invalida.")
    if not MERCADOPAGO_TEST_ACCESS_TOKEN or MERCADOPAGO_TEST_ACCESS_TOKEN == MERCADOPAGO_ACCESS_TOKEN:
        raise HTTPException(status_code=403, detail="As credenciais de teste nao estao separadas da producao.")


def _validar_ref_avulso_teste(referencia: str) -> str:
    ref = referencia.strip().upper()
    if not re.fullmatch(r"TESTE-AVULSA-[A-Z0-9-]{1,75}", ref):
        raise HTTPException(
            status_code=400,
            detail="Use uma referencia exclusiva no formato TESTE-AVULSA-001.",
        )
    return ref


@app.get("/api/pagamentos/mercadopago/teste/avulsa/preparacao")
async def preparacao_avulsa_mp_teste():
    # Apenas configuracao local; nao cria pagamento.
    return {
        "ok": True,
        "tipo": "PAGAMENTO_UNICO",
        "valor_teste": 5.0,
        "enabled": MERCADOPAGO_AVULSO_TEST_ENABLED,
        "chave_admin_configurada": len(MERCADOPAGO_TEST_ADMIN_KEY) >= 32,
        "seller_id_configurado": bool(MERCADOPAGO_TEST_SELLER_ID),
        "token_teste_configurado": bool(MERCADOPAGO_TEST_ACCESS_TOKEN),
        "token_distinto_da_producao": bool(MERCADOPAGO_TEST_ACCESS_TOKEN)
            and MERCADOPAGO_TEST_ACCESS_TOKEN != MERCADOPAGO_ACCESS_TOKEN,
        "altera_firebase": False,
        "aviso": "Nao gera assinatura, preferencia nem pagamento. Apenas leitura.",
    }


@app.post("/api/pagamentos/mercadopago/teste/fatura-avulsa")
async def criar_fatura_avulsa_mp_teste(
    dados: PagamentoAvulsoTesteRequest,
    x_bikego_test_admin_key: Optional[str] = Header(default=None),
):
    """
    Gera link de pagamento unico de R$ 5,00 no Checkout Pro de TESTE.

    A preferencia criada nao e uma assinatura, nao e uma cobranca aprovada e
    NAO modifica o Firebase. O comprador confirma no site do Mercado Pago.
    """
    _proteger_avulso_teste(x_bikego_test_admin_key)
    await _verificar_conta_vendedora_teste()
    ref = _validar_ref_avulso_teste(dados.referencia)

    # Busca a preferencia antes de criar, para evitar duplicar links da
    # mesma referencia em execucoes repetidas no Swagger.
    try:
        busca = await mercadopago_get(
            "/checkout/preferences/search",
            params={"external_reference": ref, "limit": 20},
            teste=True,
        )
    except HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail={"motivo": "Nao foi possivel consultar preferencias anteriores. Nenhuma nova preferencia foi criada.",
                    "codigo_mp": exc.status_code},
        ) from exc

    encontrados = busca.get("elements") or busca.get("results") or []
    if isinstance(encontrados, list):
        for pref in encontrados:
            if not isinstance(pref, dict) or str(pref.get("external_reference") or "") != ref:
                continue
            pref_id = str(pref.get("id") or "")
            if not pref_id:
                continue
            detalhada = await mercadopago_get(
                f"/checkout/preferences/{pref_id}",
                teste=True,
            )
            return {
                "ok": True,
                "reutilizada": True,
                "tipo": "FATURA_UNICA_TESTE",
                "valor": 5.0,
                "referencia": ref,
                "preference_id": pref_id,
                "checkout_url": detalhada.get("init_point") or detalhada.get("sandbox_init_point"),
                "altera_firebase": False,
            }

    # Preferencia sem token de cartao ou autorizacao de assinatura.
    payload = {
        "items": [{
            "id": "BIKEGO-TESTE-AVULSO",
            "title": "BIKEGO - Teste de fatura unica",
            "description": "Teste de pagamento unico, sem renovacao automatica",
            "quantity": 1,
            "currency_id": "BRL",
            "unit_price": 5.0,
        }],
        "external_reference": ref,
        "back_urls": {
            "success": "https://bikego-pagamentos-web.web.app/",
            "pending": "https://bikego-pagamentos-web.web.app/",
            "failure": "https://bikego-pagamentos-web.web.app/",
        },
    }
    try:
        preferencia = await mercadopago_post(
            "/checkout/preferences",
            payload,
            teste=True,
            extra_headers={
                "X-Idempotency-Key": hashlib.sha256(ref.encode("utf-8")).hexdigest()
            },
        )
    except HTTPException as exc:
        # Nao retornar payload ou credencial no erro.
        detalhe = exc.detail if isinstance(exc.detail, dict) else {}
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "origem": "mercado_pago",
                "codigo_http": exc.status_code,
                "codigo": detalhe.get("code") or detalhe.get("error"),
                "mensagem": str(detalhe.get("message") or "Preferencia nao criada")[:180],
                "acao": "Nao repita Execute com outra referencia sem verificar a consulta de preferencias.",
            },
        ) from exc

    return {
        "ok": True,
        "reutilizada": False,
        "tipo": "FATURA_UNICA_TESTE",
        "valor": 5.0,
        "referencia": ref,
        "preference_id": preferencia.get("id"),
        "checkout_url": preferencia.get("init_point") or preferencia.get("sandbox_init_point"),
        "altera_firebase": False,
        "aviso": "Link de teste criado, mas nao ha pagamento aprovado nem desbloqueio de licencas.",
    }


@app.get("/api/pagamentos/mercadopago/teste/avulsa/status/{payment_id}")
async def status_pagamento_avulso_mp_teste(
    payment_id: str,
    x_bikego_test_admin_key: Optional[str] = Header(default=None),
):
    """Consulta um pagamento ja realizado; nunca libera licencas."""
    _proteger_avulso_teste(x_bikego_test_admin_key)
    await _verificar_conta_vendedora_teste()
    if not payment_id.isdigit():
        raise HTTPException(status_code=400, detail="ID de pagamento invalido.")
    pagamento = await mercadopago_get(f"/v1/payments/{payment_id}", teste=True)
    referencia = str(pagamento.get("external_reference") or "")
    if not referencia.startswith("TESTE-AVULSA-"):
        raise HTTPException(status_code=404, detail="Nao e um pagamento da rotina de teste avulsa.")
    return {
        "ok": True,
        "tipo": "FATURA_UNICA_TESTE",
        "referencia": referencia,
        "payment_id": pagamento.get("id"),
        "status": pagamento.get("status"),
        "status_detail": pagamento.get("status_detail"),
        "valor": pagamento.get("transaction_amount"),
        "altera_firebase": False,
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


@app.get("/api/mercadopago/test-public-key")
def mercadopago_test_public_key():
    if not MERCADOPAGO_TEST_PUBLIC_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "MERCADOPAGO_TEST_PUBLIC_KEY não configurada no servidor. "
                "Copie a Public Key do ambiente Teste da mesma aplicação Mercado Pago."
            ),
        )
    return {
        "ok": True,
        "ambiente": "TESTE",
        "public_key": MERCADOPAGO_TEST_PUBLIC_KEY,
    }


@app.get("/api/mercadopago/public-key")
def mercadopago_public_key():
    if not MERCADOPAGO_PUBLIC_KEY:
        raise HTTPException(
            status_code=503,
            detail="MERCADOPAGO_PUBLIC_KEY não configurada no servidor.",
        )
    return {"ok": True, "public_key": MERCADOPAGO_PUBLIC_KEY}


@app.post("/api/pagamentos/mercadopago/cartao-unico")
async def criar_pagamento_mercadopago_cartao_unico(
    dados: MercadoPagoOneTimePaymentRequest,
):
    if not BIKEGO_MP_LEGACY_CREATE_ENABLED:
        raise HTTPException(410, "Rota de cobranca antiga desativada. Utilize o fluxo autenticado V52 de fatura unica.")

    """
    Diagnóstico de produção: cobra um valor informado pelo checkout, uma única vez.
    Não cria assinatura e não altera status/licenças no Firebase.
    """
    email = dados.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Informe um e-mail válido do pagador.")

    payment_method_id = dados.payment_method_id.strip().lower()
    if not payment_method_id:
        raise HTTPException(status_code=400, detail="Meio de pagamento não informado.")

    payer = {"email": email}
    ident_type = (dados.identification_type or "").strip().upper()
    ident_number = re.sub(r"\D", "", dados.identification_number or "")
    if ident_type and ident_number:
        payer["identification"] = {
            "type": ident_type,
            "number": ident_number,
        }

    nome_titular = " ".join((dados.cardholder_name or "").split()).strip()
    primeiro_nome = ""
    sobrenome = ""
    if nome_titular:
        partes_nome = nome_titular.split(" ")
        primeiro_nome = partes_nome[0]
        sobrenome = " ".join(partes_nome[1:]) if len(partes_nome) > 1 else ""
        payer["first_name"] = primeiro_nome
        if sobrenome:
            payer["last_name"] = sobrenome

    # Dados adicionais para o motor de risco.
    # O CPF/documento já é enviado em payer.identification no nível principal.
    # Não duplicamos identification dentro de additional_info.payer porque
    # algumas contas/versões da Payments API rejeitam esse caminho.
    additional_payer = {}
    if primeiro_nome:
        additional_payer["first_name"] = primeiro_nome
    if sobrenome:
        additional_payer["last_name"] = sobrenome
    additional_payer["authentication_type"] = "WEB"

    payload = {
        "transaction_amount": round(dados.valor, 2),
        "token": dados.card_token_id,
        "description": f"BIKEGO - TESTE PAGAMENTO UNICO R$ {dados.valor:.2f}",
        "installments": 1,
        "payment_method_id": payment_method_id,
        "payer": payer,
        "external_reference": dados.referencia,
        "statement_descriptor": "BIKEGO",
        "additional_info": {
            "items": [
                {
                    "id": "BIKEGO-FIREBASE",
                    "title": "Plano de armazenamento Firebase BIKEGO",
                    "description": "Servico digital de armazenamento e infraestrutura Firebase",
                    "category_id": "digital_goods",
                    "quantity": 1,
                    "unit_price": round(dados.valor, 2),
                }
            ],
            **({"payer": additional_payer} if additional_payer else {}),
        },
    }

    issuer = (dados.issuer_id or "").strip()
    if issuer:
        payload["issuer_id"] = issuer

    headers = mercadopago_headers(False)
    headers["X-Idempotency-Key"] = dados.request_id.strip()
    device_id = (dados.device_id or "").strip()
    if device_id:
        headers["X-meli-session-id"] = device_id

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{MERCADOPAGO_BASE_URL}/v1/payments",
                headers=headers,
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Falha de comunicação com o Mercado Pago: {exc}",
        ) from exc

    try:
        resposta = response.json()
    except Exception:
        resposta = {"raw": response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=resposta)

    status = str(resposta.get("status") or "").lower()
    return {
        "ok": True,
        "provedor": "MERCADO_PAGO",
        "tipo": "PAGAMENTO_UNICO_DIAGNOSTICO",
        "valor": round(dados.valor, 2),
        "referencia": dados.referencia,
        "paymentId": resposta.get("id"),
        "status": status,
        "statusDetail": resposta.get("status_detail"),
        "approved": status == "approved",
    }


@app.get("/api/pagamentos/mercadopago/cartao-unico/{payment_id}")
async def consultar_pagamento_mercadopago_cartao_unico(payment_id: str):
    """
    Consulta o status de um pagamento único já criado no Mercado Pago.
    Não gera nova cobrança e não altera status/licenças no Firebase.
    """
    pid = (payment_id or "").strip()
    if not re.fullmatch(r"\d+", pid):
        raise HTTPException(
            status_code=400,
            detail="ID de pagamento inválido.",
        )

    resposta = await mercadopago_get(f"/v1/payments/{pid}")
    status = str(resposta.get("status") or "").lower()
    status_detail = str(resposta.get("status_detail") or "")

    return {
        "ok": True,
        "provedor": "MERCADO_PAGO",
        "tipo": "CONSULTA_PAGAMENTO_UNICO_DIAGNOSTICO",
        "paymentId": resposta.get("id"),
        "status": status,
        "statusDetail": status_detail,
        "approved": status == "approved",
        "rejected": status == "rejected",
        "inProcess": status in {"in_process", "pending"},
        "valor": resposta.get("transaction_amount"),
        "referencia": resposta.get("external_reference"),
        "dateCreated": resposta.get("date_created"),
        "dateApproved": resposta.get("date_approved"),
    }


@app.post("/api/assinaturas/mercadopago/teste/cartao")
async def criar_assinatura_mercadopago_teste_cartao(
    dados: MercadoPagoCardSubscriptionRequest,
):
    """
    Sandbox da assinatura transparente com cartão.
    O card_token_id deve ser gerado no navegador com a Public Key de TESTE.
    """
    email = dados.email.strip().lower()
    if "@" not in email:
        raise HTTPException(
            status_code=400,
            detail="Informe um e-mail válido do pagador de teste.",
        )

    payload = {
        "reason": "FIREBASE ARMAZENAMENTO - TESTE",
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

    device_id = (dados.device_id or "").strip()
    resposta = await mercadopago_post(
        "/preapproval",
        payload,
        teste=True,
        extra_headers={"X-meli-session-id": device_id} if device_id else None,
    )

    return {
        "ok": True,
        "ambiente": "TESTE",
        "provedor": "MERCADO_PAGO_TRANSPARENTE_TESTE",
        "empresa": dados.empresa,
        "valor": dados.valor,
        "referencia": dados.referencia,
        "subscriptionId": resposta.get("id"),
        "status": resposta.get("status"),
        "nextPaymentDate": resposta.get("next_payment_date"),
        "paymentMethodId": resposta.get("payment_method_id"),
        "mercadopago": resposta,
    }


@app.post("/api/assinaturas/mercadopago/cartao")
async def criar_assinatura_mercadopago_cartao(
    dados: MercadoPagoCardSubscriptionRequest,
):
    if not BIKEGO_MP_LEGACY_CREATE_ENABLED:
        raise HTTPException(410, "Rota de cobranca antiga desativada. Utilize o fluxo autenticado V52 de fatura unica.")

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

    device_id = (dados.device_id or "").strip()
    resposta = await mercadopago_post(
        "/preapproval",
        payload,
        extra_headers={"X-meli-session-id": device_id} if device_id else None,
    )

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
    if not BIKEGO_MP_LEGACY_CREATE_ENABLED:
        raise HTTPException(410, "Rota de cobranca antiga desativada. Utilize o fluxo autenticado V52 de fatura unica.")

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
    if not BIKEGO_MP_LEGACY_CREATE_ENABLED:
        raise HTTPException(410, "Rota de cobranca antiga desativada. Utilize o fluxo autenticado V52 de fatura unica.")

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


@app.get("/webhooks/mercadopago/health")
def health_webhook_mercadopago():
    """Confirma existencia da rota, sem simular pagamento ou liberar licenca."""
    return {
        "ok": True,
        "webhook": "disponivel",
        "segredo_configurado": bool(MERCADOPAGO_WEBHOOK_SECRET),
        "aviso": "Consulta somente leitura; nao comprova entrega de notificacao.",
    }


@app.post("/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    # Swagger /docs gera POST vazio quando nenhum corpo e informado.
    # Isto nao e uma notificacao do Mercado Pago e nao deve causar HTTP 500.
    try:
        evento = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Corpo JSON ausente ou invalido. Use o simulador de Webhooks do Mercado Pago.",
        ) from exc
    if not isinstance(evento, dict):
        raise HTTPException(status_code=400, detail="Evento deve ser um objeto JSON.")
    data = evento.get("data") or {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Campo data da notificacao invalido.")
    data_id = str(
        request.query_params.get("data.id")
        or request.query_params.get("data_id")
        or data.get("id")
        or ""
    ).strip()

    if not data_id:
        return {"received": True, "ignored": True, "reason": "sem data.id"}

    # Rejeita notificacoes que nao puderem ser autenticadas; jamais confia
    # apenas no id ou no live_mode enviado no corpo da requisicao.
    if not MERCADOPAGO_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Assinatura secreta do webhook nao configurada.")
    _validar_webhook_mercadopago(request, data_id)

    # O simulador oficial envia live_mode=false, frequentemente com data.id
    # ficticio (por exemplo 123456). Nunca consultar /v1/payments/123456:
    # esse recurso nao existe e gerava HTTP 404. Validamos assinatura ANTES
    # de descartar a notificacao, e jamais atualizamos faturas reais.
    if evento.get("live_mode") is False:
        return {
            "received": True,
            "ignored": True,
            "reason": "Notificacao assinada de teste/simulacao; nenhuma licenca alterada.",
            "firebaseUpdated": 0,
        }

    tipo = str(evento.get("type") or "").lower()
    ambiente_teste = request.query_params.get("ambiente", "").lower() == "teste"
    if ambiente_teste:
        # Eventos de teste nunca alteram o Firestore de clientes reais.
        return {"received": True, "ignored": True, "ambiente": "TESTE", "firebaseUpdated": 0}
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

    # Nunca modificar licencas reais quando o evento referencia o
    # procedimento avulso de teste, mesmo que seja enviado sem ?ambiente=teste.
    if str(referencia).upper().startswith("TESTE-AVULSA-"):
        return {
            "received": True,
            "ignored": True,
            "reason": "fatura avulsa de teste",
            "firebaseUpdated": 0,
        }

    if str(referencia).startswith("BIKEGO-FAT-"):
        if not MERCADOPAGO_WEBHOOK_SECRET:
            raise HTTPException(503, "Webhook assinado obrigatorio para faturas V52.")
        if tipo != "payment":
            return {"received": True, "ignored": True, "reason": "evento nao e pagamento"}
        resultado = await _v52_conciliar_pagamento(str(payment_id))
        return {"received": True, "faturaV52": True, "resultado": resultado}

    # Protecao V48: eventos Mercado Pago somente afetam clientes vinculados
    # a esse provedor; pagamentos de outro provedor nao liberam licencas.
    db_verificacao = get_firestore_db()
    clientes_referencia = list(
        db_verificacao.collection("clientes")
        .where("referenciaAssinatura", "==", str(referencia))
        .stream()
    )
    if not clientes_referencia:
        return {"received": True, "ignored": True, "reason": "referencia desconhecida"}
    if any(
        not str((d.to_dict() or {}).get("provedorPagamento") or "")
        .upper().startswith("MERCADO_PAGO")
        for d in clientes_referencia
    ):
        return {"received": True, "ignored": True, "reason": "provedor divergente"}

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

    if nome_evento.startswith("CHECKOUT_"):
        checkout = evento.get("checkout") or {}
        checkout_id = str(checkout.get("id") or "").strip()
        referencia = str(checkout.get("externalReference") or "").strip()

        db = get_firestore_db()
        checkout_doc = None
        if checkout_id:
            checkout_doc = db.collection("asaas_checkouts").document(checkout_id)
            snap = checkout_doc.get()
            if snap.exists:
                salvo = snap.to_dict() or {}
                if not referencia:
                    referencia = str(salvo.get("referencia") or "").strip()

        if nome_evento == "CHECKOUT_PAID":
            novo_status = "ATIVO"
        elif nome_evento in {"CHECKOUT_CANCELED", "CHECKOUT_EXPIRED"}:
            novo_status = "AGUARDANDO PAGAMENTO"
        else:
            novo_status = None

        if checkout_doc is not None:
            checkout_doc.set(
                {
                    "status": str(checkout.get("status") or ""),
                    "ultimoEvento": nome_evento,
                    "atualizadoEm": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )

        atualizados = 0
        if referencia and novo_status is not None:
            atualizados = _sincronizar_cliente_firebase(
                referencia,
                novo_status=novo_status,
                payment_id=checkout_id or None,
            )

        return {
            "received": True,
            "event": nome_evento,
            "checkoutId": checkout_id,
            "externalReference": referencia or None,
            "firebaseUpdated": atualizados,
            "statusAplicado": novo_status,
        }

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
