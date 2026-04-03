"""
Enterprise WhatsApp AI OS — v5.2
Fixes: Manager hand-back grounding, history window 6→10,
       explicit [Specialist said] instruction, media ACK handler.
"""

import os, math, re, asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, Response
from motor.motor_asyncio import AsyncIOMotorClient
from groq import AsyncGroq
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── 0. Startup validation ───────────────────────────────────────────────────
_REQUIRED = ["GROQ_API_KEY", "KAPSO_API_KEY", "MONGO_URI"]
_missing  = [k for k in _REQUIRED if not os.getenv(k)]
if _missing:
    raise RuntimeError(f"Missing env vars: {', '.join(_missing)}")

# ── Clients ───────────────────────────────────────────────────────────────────
app        = FastAPI()
groq       = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
http       = httpx.AsyncClient(timeout=10.0)
mongo      = AsyncIOMotorClient(os.getenv("MONGO_URI"))
db         = mongo["EnterpriseAgent"]

brands_col = db["Brands"]
stores_col = db["Stores"]
chat_col   = db["ChatHistory"]
state_col  = db["UserState"]
dedup_col  = db["ProcessedMessages"]

@app.on_event("startup")
async def create_indexes():
    await dedup_col.create_index("ts", expireAfterSeconds=86400)
    await state_col.create_index("phone_number", unique=True)
    await chat_col.create_index([("phone_number", 1), ("timestamp", -1)])

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def clean(val) -> str:
    return re.sub(r"\D", "", str(val or ""))

async def send_whatsapp(phone: str, text: str, retries: int = 2):
    target = clean(phone)
    if len(target) < 10: return
    headers = {"X-API-Key": os.getenv("KAPSO_API_KEY"), "Content-Type": "application/json"}
    body    = {"message": {"phone_number": target, "content": text, "message_type": "text"}}
    for attempt in range(retries + 1):
        try:
            r = await http.post("https://app.kapso.ai/api/v1/whatsapp_messages", headers=headers, json=body)
            if r.status_code in (200, 201): return
        except Exception as e:
            print(f"❌ network attempt {attempt + 1}: {e}")
        if attempt < retries: await asyncio.sleep(2 ** attempt)

async def load_brand(brand_phone: str = "", brand_id: str = "NIKE_IND") -> dict:
    """Permanent Routing Fix: Match by phone, fallback to default ID."""
    brand = None
    if brand_phone:
        brand = await brands_col.find_one({"brand_phone": brand_phone})
    if not brand:
        brand = await brands_col.find_one({"brand_id": brand_id})
    if not brand:
        raise ValueError(f"System Error: Default brand '{brand_id}' missing from DB.")
    return brand

def nearest_store(stores: list, lat: float, lon: float):
    if not stores: return None
    return min(stores, key=lambda s: math.sqrt((lat - s["lat"])**2 + (lon - s["lon"])**2))

async def should_escalate(text: str) -> bool:
    safe = text[:500]
    prompt = (
        "Analyze: '{safe}'. Reply 'ESCALATE' if user wants a manager, human, "
        "discount, or is angry. Otherwise 'AI_HANDLE'. Reply ONLY the word."
    ).format(safe=safe)
    try:
        r = await groq.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", temperature=0, max_tokens=5
        )
        return "ESCALATE" in r.choices[0].message.content.strip().upper()
    except:
        return False

async def is_muted(sender: str) -> bool:
    doc = await state_col.find_one({"phone_number": sender})
    if not doc or not doc.get("is_human_active"): return False
    last = doc.get("last_human_interaction")
    if last and (datetime.utcnow() - last) < timedelta(minutes=30):
        await state_col.update_one(
            {"phone_number": sender},
            {"$set": {"last_human_interaction": datetime.utcnow()}}
        )
        return True
    await state_col.update_one(
        {"phone_number": sender},
        {"$set": {"is_human_active": False}}
    )
    return False

async def set_muted(sender: str, brand_id: str, muted: bool):
    await state_col.update_one(
        {"phone_number": sender},
        {"$set": {
            "brand_id": brand_id,
            "is_human_active": muted,
            "last_human_interaction": datetime.utcnow()
        }},
        upsert=True
    )

# ── FIX 1: Grounded system prompt + FIX 2: History window 6 → 10 ─────────────
async def build_messages(brand: dict, sender: str, current_text: str) -> list:
    brand_name = brand.get("brand_name", "Nike")
    system = (
        f"You are a {brand_name} Concierge. "
        f"Persona: {brand.get('persona')}. "
        f"Goal: {brand.get('nudge_goal')} "
        f"Rules: Max 3 short paragraphs. No competitors. Do not reveal you are AI. "
        # ── THE CORE FIX ──
        f"CRITICAL: If the conversation history contains a message prefixed with "
        f"'[Specialist said]', you MUST acknowledge and reference it naturally in "
        f"your reply — do not ignore it. "
        f"Ground every response strictly in the actual conversation history. "
        f"Never invent offers, packages, product details, or discounts that are "
        f"not explicitly present in the history."
    )
    messages = [{"role": "system", "content": system}]

    # FIX 2: Raised limit from 6 → 10 so the manager message survives the
    # 30-minute gap and is always present in the context window.
    history = await chat_col.find(
        {"phone_number": sender}
    ).sort("_id", -1).limit(10).to_list(length=10)
    history.reverse()

    for doc in history:
        # Only add user_msg if it's a genuine user turn (not a muted log)
        if doc.get("user_msg") and not doc.get("muted"):
            messages.append({"role": "user", "content": doc["user_msg"]})
        if doc.get("ai_reply"):
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        if doc.get("manager_msg"):
            # FIX 3: Explicit label so the model cannot miss or misinterpret it
            messages.append({
                "role": "assistant",
                "content": f"[Specialist said earlier in this conversation]: {doc['manager_msg']}"
            })

    messages.append({"role": "user", "content": current_text})
    return messages

# ── FIX 4: Media type detector ────────────────────────────────────────────────
def detect_media_type(msg_data: dict) -> str | None:
    """
    Returns a friendly media label if the message contains non-text media,
    or None if it is a plain text / location message.
    """
    media_keys = {
        "image":    "image",
        "video":    "video",
        "audio":    "audio",
        "document": "document",
        "sticker":  "sticker",
        "voice":    "voice note",
    }
    for key, label in media_keys.items():
        if msg_data.get(key):
            return label
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request):
    payload  = await request.json()
    msg_data = payload.get("message", {})
    msg_id   = msg_data.get("id") or msg_data.get("message_id")

    # ── 0. Atomic dedup ──────────────────────────────────────────────────────
    if msg_id:
        existing = await dedup_col.find_one_and_update(
            {"msg_id": msg_id},
            {"$setOnInsert": {"msg_id": msg_id, "ts": datetime.utcnow()}},
            upsert=True, return_document=False
        )
        if existing is not None:
            return Response(status_code=200)

    try:
        sender    = clean(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = (msg_data.get("text") or {}).get("body") or ""
        location  = msg_data.get("location")
        direction = str(payload.get("direction", "")).lower()

        # ── 0.5 Brand routing ─────────────────────────────────────────────────
        to_phone = clean(msg_data.get("to") or payload.get("to") or "")
        brand    = await load_brand(brand_phone=to_phone)
        brand_id, mgr_phone = brand["brand_id"], clean(brand.get("manager_phone", ""))

        is_outbound = direction == "outbound" or not msg_data.get("from")
        is_manager  = bool(mgr_phone) and (sender == mgr_phone)

        # ── 1. Manager tracking ───────────────────────────────────────────────
        if is_outbound or is_manager:
            customer = clean(msg_data.get("to") or msg_data.get("recipient_id") or "")
            if customer:
                await set_muted(customer, brand_id, muted=True)
                if user_text:
                    # FIX 5: Only save manager_msg — no user_msg/ai_reply fields
                    # on this document to avoid polluting the history weave.
                    await chat_col.insert_one({
                        "brand_id":    brand_id,
                        "phone_number": customer,
                        "manager_msg": user_text,
                        "timestamp":   datetime.utcnow()
                    })
            return {"status": "manager_logged"}

        # ── 1.5 Non-text media graceful exit (Test 4) ─────────────────────────
        media_type = detect_media_type(msg_data)
        if media_type and not user_text and not location:
            ack = (
                f"Thanks for sending that {media_type}! "
                f"I work best with text — just type your question and I'll help right away. 💬"
            )
            await send_whatsapp(sender, ack)
            return {"status": "media_ack"}

        # ── 2. Triage (the hard-stop) ─────────────────────────────────────────
        if user_text and await should_escalate(user_text):
            await set_muted(sender, brand_id, muted=True)
            alert = f"🚨 *URGENT ESCALATION*\nAthlete: {sender}\nSaid: {user_text[:400]}"
            await asyncio.gather(
                send_whatsapp(mgr_phone, alert),
                send_whatsapp(sender, "I'm connecting you with a Nike specialist right away! 🙌")
            )
            return {"status": "escalated"}

        # ── 3. Mute check ─────────────────────────────────────────────────────
        if await is_muted(sender):
            if user_text:
                await chat_col.insert_one({
                    "brand_id":    brand_id,
                    "phone_number": sender,
                    "user_msg":    user_text,
                    "muted":       True,
                    "timestamp":   datetime.utcnow()
                })
            return {"status": "muted"}

        # ── 4. Location lead gen ──────────────────────────────────────────────
        if location:
            lat, lon = location.get("latitude"), location.get("longitude")
            stores   = await stores_col.find({"brand_id": brand_id}).to_list(length=100)
            store    = nearest_store(stores, lat, lon)
            if store:
                maps  = f"https://www.google.com/maps?q={store['lat']},{store['lon']}"
                reply = (
                    f"📍 *Nearest Nike Store*\n\n"
                    f"*{store['store_name']}*\n{store.get('address', '')}\n\n"
                    f"🗺 Directions: {maps}\n\nJust Do It"
                )
                await asyncio.gather(
                    send_whatsapp(sender, reply),
                    send_whatsapp(mgr_phone, f"🚨 *NEW LEAD*: {sender} is visiting {store['store_name']}")
                )
            if not user_text:
                return {"status": "location_handled"}

        # ── 5. AI engine ──────────────────────────────────────────────────────
        if not user_text:
            return Response(status_code=200)

        messages   = await build_messages(brand, sender, user_text)
        completion = await groq.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            max_tokens=300
        )
        ai_reply = completion.choices[0].message.content

        await asyncio.gather(
            chat_col.insert_one({
                "brand_id":    brand_id,
                "phone_number": sender,
                "user_msg":    user_text,
                "ai_reply":    ai_reply,
                "timestamp":   datetime.utcnow()
            }),
            send_whatsapp(sender, ai_reply)
        )

    except Exception as e:
        print(f"💥 Webhook Error: {e}")

    return Response(status_code=200)


@app.get("/")
async def health():
    return {"status": "Enterprise AI OS — live", "version": "5.2"}