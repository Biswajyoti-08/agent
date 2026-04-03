"""
Enterprise WhatsApp AI OS — v5.0
Production-ready: async, multi-brand, atomic dedup, full conversation context.
"""

import os, math, re, asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, Response
from motor.motor_asyncio import AsyncIOMotorClient
from groq import AsyncGroq
import httpx
from dotenv import load_dotenv

load_dotenv()

# ── 0. Startup validation — fail fast, don't boot broken ─────────────────────
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
state_col  = db["UserState"]        # one doc per user — lean mute/escalation state
dedup_col  = db["ProcessedMessages"]


# ── Startup: create indexes once ──────────────────────────────────────────────
@app.on_event("startup")
async def create_indexes():
    await dedup_col.create_index("ts",           expireAfterSeconds=86400)
    await state_col.create_index("phone_number", unique=True)
    await chat_col.create_index([("phone_number", 1), ("timestamp", -1)])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def clean(val) -> str:
    return re.sub(r"\D", "", str(val or ""))

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

async def load_brand(brand_phone: str = "", brand_id: str = "") -> dict:
    """
    Multi-brand routing: match by the WhatsApp number that received the message.
    Falls back to brand_id. Raises ValueError if neither matches — stops blind processing.
    """
    brand = None
    if brand_phone:
        brand = await brands_col.find_one({"brand_phone": brand_phone})
    if not brand and brand_id:
        brand = await brands_col.find_one({"brand_id": brand_id})
    if not brand:
        raise ValueError(f"No brand matched (phone={brand_phone}, id={brand_id})")
    return brand

def nearest_store(stores: list, lat: float, lon: float):
    if not stores:
        return None
    return min(stores, key=lambda s: math.sqrt((lat - s["lat"])**2 + (lon - s["lon"])**2))

async def should_escalate(text: str) -> bool:
    """
    Binary triage — truncated to 500 chars to block token-spike abuse.
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
    """
    True if a human is active within the 30-min sliding window.
    Operates only on UserState — never touches the large ChatHistory collection.
    """
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
    # Window expired — re-enable AI
    await state_col.update_one(
        {"phone_number": sender},
        {"$set": {"is_human_active": False}}
    )
    return False

async def set_muted(sender: str, brand_id: str, muted: bool):
    await state_col.update_one(
        {"phone_number": sender},
        {"$set": {
            "brand_id":                brand_id,
            "is_human_active":         muted,
            "last_human_interaction":  datetime.utcnow()
        }},
        upsert=True
    )

async def build_messages(brand: dict, sender: str, current_text: str) -> list:
    """
    Groq message list: system persona + last 6 turns of history.
    Manager messages are included so AI is never out-of-context on re-activation.
    """
    system = (
        f"You are a WhatsApp concierge for {brand.get('brand_name', 'our brand')}.\n"
        f"Persona: {brand.get('persona', 'Friendly and knowledgeable.')}\n"
        f"Goal: {brand.get('nudge_goal', 'Help the customer find what they need.')}\n"
        "Rules:\n"
        "- Max 3 short paragraphs per reply.\n"
        "- Never mention competitor brands.\n"
        "- If you cannot help, say a specialist will assist shortly — never invent policies.\n"
        "- Do not reveal you are an AI unless directly asked."
    )
    messages = [{"role": "system", "content": system}]

    history = await chat_col.find(
        {"phone_number": sender}
    ).sort("_id", -1).limit(6).to_list(length=6)
    history.reverse()

    for doc in history:
        if doc.get("user_msg"):
            messages.append({"role": "user",      "content": doc["user_msg"]})
        if doc.get("ai_reply"):
            messages.append({"role": "assistant", "content": doc["ai_reply"]})
        if doc.get("manager_msg"):
            messages.append({"role": "assistant",
                             "content": f"[Store specialist said]: {doc['manager_msg']}"})

    messages.append({"role": "user", "content": current_text})
    return messages


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/webhook")
async def webhook(request: Request):
    payload  = await request.json()
    msg_data = payload.get("message", {})
    msg_id   = msg_data.get("id") or msg_data.get("message_id")

    # ── 0. Atomic dedup — prevents race conditions on simultaneous retries ─────
    if msg_id:
        existing = await dedup_col.find_one_and_update(
            {"msg_id": msg_id},
            {"$setOnInsert": {"msg_id": msg_id, "ts": datetime.utcnow()}},
            upsert=True,
            return_document=False   # None = first time seen; doc = duplicate
        )
        if existing is not None:
            return Response(status_code=200)

    try:
        sender    = clean(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = (msg_data.get("text") or {}).get("body") or ""
        location  = msg_data.get("location")
        msg_type  = msg_data.get("type", "text")
        direction = str(payload.get("direction", "")).lower()

        # ── Brand routing (multi-brand: match by receiving WhatsApp number) ───
        to_phone = clean(msg_data.get("to") or payload.get("to") or "")
        brand    = await load_brand(brand_phone=to_phone)   # raises ValueError if not found
        brand_id  = brand["brand_id"]
        mgr_phone = clean(brand.get("manager_phone", ""))

        is_outbound = direction == "outbound" or not msg_data.get("from")
        is_manager  = bool(mgr_phone) and (sender == mgr_phone)

        print(f"\n── {brand_id} | from:{sender} | outbound:{is_outbound} | mgr:{is_manager} ──")

        # ── 1. Manager message → log it, mute AI ─────────────────────────────
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

        # ── 2. Non-text media — acknowledge gracefully ────────────────────────
        if not user_text and not location and msg_type in ("image", "audio", "video", "document", "sticker"):
            await send_whatsapp(sender,
                "Thanks for sending that! I work best with text — "
                "just type your question and I'll help right away. 👟")
            return {"status": "media_ack"}

        # ── 3. Triage FIRST — even muted users can trigger escalation ─────────
        if user_text and await should_escalate(user_text):
            await set_muted(sender, brand_id, muted=True)
            await asyncio.gather(
                send_whatsapp(mgr_phone,
                    f"⚠️ *URGENT ESCALATION*\nAthlete: {sender}\n\n_{user_text[:400]}_"),
                send_whatsapp(sender,
                    f"I want to make sure you get the best help possible — connecting you with a "
                    f"{brand.get('brand_name', 'brand')} specialist right away! 🙌")
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
                    f"{brand.get('signature', '')}"
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
                    "ai_reply":     f"Routed to {store['store_name']}",
                    "timestamp":    datetime.utcnow()
                })
            else:
                await send_whatsapp(sender,
                    "Thanks for sharing your location! I couldn't find a nearby store — "
                    "a specialist will reach out to help you. 📍")
            # If the user sent text + location together, fall through to AI
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

        # Fire DB write and WhatsApp send concurrently
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
        # Brand not found — misconfigured webhook or a test ping, not a crash
        print(f"⚠️  Brand routing: {e}")
    except Exception as e:
        print(f"💥 WEBHOOK ERROR: {e}")

    return Response(status_code=200)


@app.get("/")
async def health():
    return {"status": "Enterprise AI OS — live", "version": "5.0"}