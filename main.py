"""
Enterprise WhatsApp AI OS — v5.2
Fixes applied over v5.1:
  - Dedup: unique index on msg_id (hard backstop for race conditions)
  - Dedup: content-hash fallback when Kapso omits message id
  - Manager hand-back grounding + history window raised to 10
  - Explicit [Specialist said] instruction in system prompt
  - Media type detector for graceful non-text ACK
"""

import os, math, re, asyncio, hashlib
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, Response
from motor.motor_asyncio import AsyncIOMotorClient
from groq import AsyncGroq
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── 0. Startup validation ─────────────────────────────────────────────────────
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


# ── Startup: indexes ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def create_indexes():
    # TTL: auto-purge dedup records after 24 h
    await dedup_col.create_index("ts", expireAfterSeconds=86400)
    # FIX: unique index on msg_id — hard DB-level backstop against race conditions.
    # Even if two requests pass the find_one_and_update simultaneously,
    # MongoDB will reject the second insert at the storage layer.
    await dedup_col.create_index("msg_id", unique=True)
    await state_col.create_index("phone_number", unique=True)
    await chat_col.create_index([("phone_number", 1), ("timestamp", -1)])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def clean(val) -> str:
    return re.sub(r"\D", "", str(val or ""))

def derive_msg_id(msg_data: dict, payload: dict) -> str | None:
    """
    FIX: Kapso sometimes omits the message id field on retries or test pings.
    If a real id exists, use it. Otherwise build a stable content-hash from
    sender + text + timestamp so we can still dedup the request.

    The hash is deterministic: identical payloads always produce the same key,
    so a true retry (same content, no id) is still caught.
    A different message from the same sender will produce a different hash.
    """
    msg_id = msg_data.get("id") or msg_data.get("message_id")
    if msg_id:
        return str(msg_id)

    # Fallback: hash sender + body only — no timestamp.
    # Two rapid taps of the same message from the same sender produce the same
    # hash and are correctly deduplicated. A genuinely different message (different
    # body) produces a different hash and is processed normally.
    sender = msg_data.get("from") or msg_data.get("phone_number") or ""
    body   = (msg_data.get("text") or {}).get("body") or ""
    raw    = f"{sender}:{body}"
    if raw == ":":
        return None   # nothing to hash — skip dedup for empty/test pings
    return "hash:" + hashlib.sha256(raw.encode()).hexdigest()

async def is_duplicate(msg_id: str) -> bool:
    """
    Atomic upsert: first caller inserts and gets None back (not a duplicate).
    Every subsequent caller finds the existing doc and gets it back (duplicate).
    The unique index is the safety net if two requests race past this simultaneously.
    """
    try:
        existing = await dedup_col.find_one_and_update(
            {"msg_id": msg_id},
            {"$setOnInsert": {"msg_id": msg_id, "ts": datetime.utcnow()}},
            upsert=True,
            return_document=False   # None = first time seen; doc object = duplicate
        )
        return existing is not None
    except Exception:
        # DuplicateKeyError from the unique index — a second concurrent request
        # lost the race at the DB layer. Treat as duplicate, return 200 silently.
        return True

async def send_whatsapp(phone: str, text: str, retries: int = 2):
    target = clean(phone)
    if len(target) < 10:
        print(f"⚠️  Invalid phone skipped: '{target}'")
        return
    headers = {"X-API-Key": os.getenv("KAPSO_API_KEY"), "Content-Type": "application/json"}
    body    = {"message": {"phone_number": target, "content": text, "message_type": "text"}}
    for attempt in range(retries + 1):
        try:
            r = await http.post(
                "https://app.kapso.ai/api/v1/whatsapp_messages",
                headers=headers, json=body
            )
            if r.status_code in (200, 201):
                print(f"✅ sent → {target}")
                return
            print(f"⚠️  Kapso {r.status_code} attempt {attempt + 1}: {r.text[:120]}")
        except Exception as e:
            print(f"❌ network attempt {attempt + 1}: {e}")
        if attempt < retries:
            await asyncio.sleep(2 ** attempt)   # 1 s → 2 s

async def load_brand(brand_phone: str = "") -> dict:
    """
    Multi-brand routing: match by the WhatsApp number that received the message.
    Falls back to NIKE_IND so single-brand setups need zero config changes.
    """
    brand = None
    if brand_phone:
        brand = await brands_col.find_one({"brand_phone": brand_phone})
        if not brand:
            print(f"⚠️  Phone {brand_phone} didn't match. Falling back to NIKE_IND.")
    if not brand:
        brand = await brands_col.find_one({"brand_id": "NIKE_IND"})
    if not brand:
        raise ValueError("System Error: Default brand 'NIKE_IND' missing from DB.")
    return brand

def nearest_store(stores: list, lat: float, lon: float):
    if not stores:
        return None
    return min(stores, key=lambda s: math.sqrt((lat - s["lat"])**2 + (lon - s["lon"])**2))

async def should_escalate(text: str) -> bool:
    """
    Binary triage — 500-char cap blocks token-spike abuse.
    Covers: negotiation, frustration, human requests, out-of-scope.
    """
    safe   = text[:500]
    prompt = f"""You are a triage classifier for a brand WhatsApp assistant.

Message: "{safe}"

Escalate if ANY apply:
1. Negotiation — pricing, discount, bulk order, wholesale, refund
2. Frustration — anger, insults, threats, aggressive language
3. Human request — wants a person, manager, real employee, callback
4. Out-of-scope — legal, medical, data privacy, "how did you get my number"

Reply ONLY: ESCALATE or AI_HANDLE"""
    try:
        r = await groq.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0, max_tokens=5
        )
        decision = r.choices[0].message.content.strip().upper()
        print(f"🔍 triage → {decision}")
        return "ESCALATE" in decision
    except Exception as e:
        print(f"❌ triage error: {e}")
        return False   # fail-open: don't spam manager on triage outage

async def is_muted(sender: str) -> bool:
    """30-min sliding window using UserState — never touches ChatHistory."""
    doc = await state_col.find_one({"phone_number": sender})
    if not doc or not doc.get("is_human_active"):
        return False
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
            "brand_id":               brand_id,
            "is_human_active":        muted,
            "last_human_interaction": datetime.utcnow()
        }},
        upsert=True
    )

async def build_messages(brand: dict, sender: str, current_text: str) -> list:
    """
    Groq message list: system persona + last 10 turns of history.
    Manager messages are woven in so AI is never out-of-context on re-activation.
    """
    brand_name = brand.get("brand_name", "Nike")
    system = (
        f"You are a {brand_name} Concierge. "
        f"Persona: {brand.get('persona', 'Friendly and knowledgeable.')} "
        f"Goal: {brand.get('nudge_goal', 'Help the customer find what they need.')} "
        "Rules: Max 3 short paragraphs. Never mention competitor brands. "
        "Do not reveal you are an AI unless directly asked. "
        "Never invent offers, prices, or policies not present in the conversation. "
        "CRITICAL: If the conversation history contains a message prefixed with "
        "'[Specialist said]', you MUST acknowledge and reference it naturally in "
        "your reply — never ignore it."
    )
    messages = [{"role": "system", "content": system}]

    history = await chat_col.find(
        {"phone_number": sender}
    ).sort("_id", -1).limit(10).to_list(length=10)
    history.reverse()

    for doc in history:
        if doc.get("user_msg") and not doc.get("muted"):
            messages.append({"role": "user",      "content": doc["user_msg"]})
        if doc.get("ai_reply"):
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        if doc.get("manager_msg"):
            messages.append({"role": "assistant",
                             "content": f"[(Team Update) Our Nike Specialist previously told you]: {doc['manager_msg']}"})

    messages.append({"role": "user", "content": current_text})
    return messages

def detect_media_type(msg_data: dict) -> str | None:
    """Returns a friendly label for non-text media, or None for text/location."""
    for key, label in {
        "image": "image", "video": "video", "audio": "audio",
        "document": "document", "sticker": "sticker", "voice": "voice note"
    }.items():
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

    # ── 0. Deduplication ──────────────────────────────────────────────────────
    # derive_msg_id: uses real id if present, content-hash as fallback.
    # is_duplicate: atomic upsert + unique index as double-layer protection.
    msg_id = derive_msg_id(msg_data, payload)
    if msg_id and await is_duplicate(msg_id):
        print(f"⏭️  Duplicate caught: {msg_id[:40]}")
        return Response(status_code=200)

    try:
        sender    = clean(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = (msg_data.get("text") or {}).get("body") or ""
        location  = msg_data.get("location")
        direction = str(payload.get("direction", "")).lower()

        # ── Brand routing ─────────────────────────────────────────────────────
        to_phone  = clean(msg_data.get("to") or payload.get("to") or "")
        brand     = await load_brand(brand_phone=to_phone)
        brand_id  = brand["brand_id"]
        mgr_phone = clean(brand.get("manager_phone", ""))

        is_outbound = direction == "outbound" or not msg_data.get("from")
        is_manager  = bool(mgr_phone) and (sender == mgr_phone)

        print(f"\n── {brand_id} | from:{sender} | outbound:{is_outbound} | mgr:{is_manager} ──")

        # ── 1. Manager message → log + mute AI ───────────────────────────────
        if is_outbound or is_manager:
            customer = clean(msg_data.get("to") or msg_data.get("recipient_id") or "")
            if customer:
                await set_muted(customer, brand_id, muted=True)
                if user_text:
                    await chat_col.insert_one({
                        "brand_id":     brand_id,
                        "phone_number": customer,
                        "manager_msg":  user_text,
                        "timestamp":    datetime.utcnow()
                    })
            print(f"👨‍💼 manager active for {customer}")
            return {"status": "manager_logged"}

        # ── 2. Non-text media → graceful ACK ─────────────────────────────────
        media_type = detect_media_type(msg_data)
        if media_type and not user_text and not location:
            await send_whatsapp(sender,
                f"Thanks for sending that {media_type}! I work best with text — "
                f"just type your question and I'll help right away. 💬")
            return {"status": "media_ack"}

        # ── 3. Triage FIRST — even muted users can trigger escalation ─────────
        if user_text and await should_escalate(user_text):
            await set_muted(sender, brand_id, muted=True)
            await asyncio.gather(
                send_whatsapp(mgr_phone,
                    f"⚠️ *URGENT ESCALATION*\nAthlete: {sender}\n\n_{user_text[:400]}_"),
                send_whatsapp(sender,
                    f"I want to make sure you get the best help possible — connecting you "
                    f"with a {brand.get('brand_name', 'brand')} specialist right away! 🙌")
            )
            await chat_col.insert_one({
                "brand_id":     brand_id,
                "phone_number": sender,
                "user_msg":     user_text,
                "escalated":    True,
                "timestamp":    datetime.utcnow()
            })
            return {"status": "escalated"}

        # ── 4. Mute check ─────────────────────────────────────────────────────
        if await is_muted(sender):
            if user_text:
                await chat_col.insert_one({
                    "brand_id":     brand_id,
                    "phone_number": sender,
                    "user_msg":     user_text,
                    "muted":        True,
                    "timestamp":    datetime.utcnow()
                })
            print(f"🔇 muted — {sender}")
            return {"status": "muted"}

        # ── 5. Location → nearest store + manager lead alert ──────────────────
        if location:
            lat, lon = location.get("latitude"), location.get("longitude")
            stores   = await stores_col.find({"brand_id": brand_id}).to_list(length=500)
            store    = nearest_store(stores, lat, lon)
            if store:
                maps  = f"https://www.google.com/maps/search/?api=1&query={store['lat']},{store['lon']}"
                reply = (
                    f"📍 *Nearest {brand.get('brand_name')} Store*\n\n"
                    f"*{store['store_name']}*\n"
                    f"{store.get('address', '')}\n\n"
                    f"🗺 Directions: {maps}\n\n"
                    f"{brand.get('signature', 'Just Do It')}"
                )
                await asyncio.gather(
                    send_whatsapp(sender, reply),
                    send_whatsapp(mgr_phone,
                        f"🚨 *NEW LEAD*: {sender} is heading to {store['store_name']}. "
                        f"Check in if needed!")
                )
                await chat_col.insert_one({
                    "brand_id":     brand_id,
                    "phone_number": sender,
                    "user_msg":     "[Location Pin]",
                    "ai_reply":     reply,   # store actual sent text so AI history is accurate
                    "timestamp":    datetime.utcnow()
                })
            else:
                await send_whatsapp(sender,
                    "Thanks for sharing your location! I couldn't find a nearby store — "
                    "a specialist will reach out to help you shortly. 📍")
            if not user_text:
                return {"status": "location_handled"}

        # ── 6. AI response ────────────────────────────────────────────────────
        if not user_text:
            return Response(status_code=200)

        print(f"🤖 generating reply for {sender}…")
        messages   = await build_messages(brand, sender, user_text)
        completion = await groq.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            max_tokens=300
        )
        ai_reply = completion.choices[0].message.content

        await asyncio.gather(
            chat_col.insert_one({
                "brand_id":     brand_id,
                "phone_number": sender,
                "user_msg":     user_text,
                "ai_reply":     ai_reply,
                "timestamp":    datetime.utcnow()
            }),
            send_whatsapp(sender, ai_reply)
        )

    except ValueError as e:
        print(f"⚠️  Brand routing: {e}")
    except Exception as e:
        print(f"💥 WEBHOOK ERROR: {e}")

    return Response(status_code=200)


@app.get("/")
async def health():
    return {"status": "Enterprise AI OS — live", "version": "5.2"}