import os
from typing import Optional
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()
ASAAS_API_KEY = os.getenv('ASAAS_API_KEY', '').strip()
ASAAS_ENV = os.getenv('ASAAS_ENV', 'sandbox').strip().lower()
ASAAS_BASE_URL = 'https://api.asaas.com/v3' if ASAAS_ENV == 'production' else 'https://api-sandbox.asaas.com/v3'

app = FastAPI(title='BIKEGO Pagamentos Backend', version='1.0.0')
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r'https?://(localhost|127\.0\.0\.1)(:\d+)?',
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

class LinkAssinaturaRequest(BaseModel):
    empresa: str = Field(min_length=2, max_length=120)
    valor: float = Field(gt=0)
    descricao: str = Field(default='Assinatura mensal dos aplicativos BIKEGO - Armazenamento e serviços', max_length=500)
    referencia: Optional[str] = Field(default=None, max_length=200)

def asaas_headers():
    if not ASAAS_API_KEY:
        raise HTTPException(status_code=500, detail='ASAAS_API_KEY não configurada no arquivo .env')
    return {
        'access_token': ASAAS_API_KEY,
        'accept': 'application/json',
        'content-type': 'application/json',
        'User-Agent': 'BIKEGO-Pagamentos/1.0',
    }

@app.get('/')
def raiz():
    return {'app': 'BIKEGO Pagamentos Backend', 'status': 'online', 'ambiente_asaas': ASAAS_ENV}

@app.get('/health')
def health():
    return {'ok': True, 'asaas_configurado': bool(ASAAS_API_KEY), 'ambiente': ASAAS_ENV}

@app.post('/api/assinaturas/link')
async def criar_link_assinatura(dados: LinkAssinaturaRequest):
    payload = {
        'billingType': 'CREDIT_CARD',
        'chargeType': 'RECURRENT',
        'name': 'BIKEGO - ARMAZENAMENTO FIREBASE',
        'description': dados.descricao,
        'value': round(dados.valor, 2),
        'subscriptionCycle': 'MONTHLY',
        'notificationEnabled': False,
    }
    if dados.referencia:
        payload['externalReference'] = dados.referencia

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f'{ASAAS_BASE_URL}/paymentLinks',
                headers=asaas_headers(),
                json=payload,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f'Falha de comunicação com o Asaas: {exc}') from exc

    try:
        resposta = response.json()
    except Exception:
        resposta = {'raw': response.text}

    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=resposta)

    return {'ok': True, 'empresa': dados.empresa, 'valor': dados.valor, 'tipo': 'CREDIT_CARD_RECURRENT', 'asaas': resposta}

@app.post('/webhooks/asaas')
async def webhook_asaas(request: Request):
    evento = await request.json()
    return {'received': True, 'event': evento.get('event')}
