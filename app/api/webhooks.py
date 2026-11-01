"""The WhatsApp Cloud API webhook, normalised into handle_inbound()."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.core.config import get_settings
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.reminders import handle_inbound

router = APIRouter(prefix="/webhooks")


@router.get("/meta")
def meta_verify(req: Request):
    """Meta calls this once when you save the webhook URL: echo hub.challenge back if the
    verify token matches META_VERIFY_TOKEN."""
    q = req.query_params
    token = get_settings().meta_verify_token
    if q.get("hub.mode") == "subscribe" and token and q.get("hub.verify_token") == token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verify token mismatch")


@router.post("/meta")
async def meta(req: Request, db: Session = Depends(get_db)):
    payload = await req.json()
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            for m in ch.get("value", {}).get("messages", []):
                if m.get("type") == "text":
                    handle_inbound(db, "+" + m["from"], m["text"]["body"])
    return {"ok": True}
