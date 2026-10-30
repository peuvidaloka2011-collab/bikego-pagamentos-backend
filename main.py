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
