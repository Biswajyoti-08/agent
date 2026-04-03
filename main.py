import os, requests, certifi, math, re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq
from dotenv import load_dotenv

# Load security keys from environment
load_dotenv()

app = FastAPI()

# --- 1. CLIENTS & DB SETUP ---
# Using 70B model for Triage to ensure high-reasoning accuracy at scale
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]

chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]
processed_msg_ids = db["ProcessedMessages"] 

# --- HELPER FUNCTIONS ---

def clean_phone(phone_str):
    """RELIABILITY: Strips all non-digits to prevent Kapso 422 API errors."""
    if not phone_str: return ""
    return re.sub(r'\D', '', str(phone_str))

def send_text(phone, text):
    """X-RAY SENDER: Validates and cleans payloads before hitting the network."""
    target = clean_phone(phone)
    if not target or len(target) < 10:
        print(f"⚠️ SEND FAIL: Invalid phone '{target}'")
        return None

    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": target, "content": text, "message_type": "text"}}
    
    print(f"📡 PAYLOAD OUT: {payload}") # Critical for Render Log auditing
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in [200, 201]:
            print(f"❌ KAPSO REJECT ({response.status_code}): {response.text}")
        return response
    except Exception as e:
        print(f"❌ NETWORK ERROR: {e}")
        return None

def check_triage(text):
    """SEMANTIC INTENT CLASSIFIER: Scalable, language-agnostic boundary detection."""
    triage_prompt = f"""
    Analyze Intent: "{text}"
    
    Classify as 'ESCALATE' if the message contains:
    1. NEGOTIATION: Price, discounts, bulk, wholesale, "too expensive".
    2. HUMAN REQUEST: Asking for a manager, person, real employee, "talk to someone".
    3. FRUSTRATION: Anger, swearing, privacy complaints, "how did you get my number".
    
    Otherwise, classify as 'AI_HANDLE'. 
    Reply ONLY with the label.
    """
    try:
        # Using 70B for the "Gatekeeper" role to prevent hallucinations
        check = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": triage_prompt}],
            model="llama-3.1-70b-versatile",
            temperature=0
        )
        result = check.choices[0].message.content.strip().upper()
        print(f"🔍 SEMANTIC TRIAGE: {result}")
        return "ESCALATE" in result
    except:
        return False

# --- CORE WEBHOOK ---

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    payload = await request.json()
    msg_data = payload.get("message", {})
    msg_id = msg_data.get("id") or msg_data.get("message_id")
    
    # 0. IDEMPOTENCY: Prevent duplicate processing (Scalability requirement)
    if msg_id and processed_msg_ids.find_one({"msg_id": msg_id}):
        return Response(status_code=200)
    if msg_id:
        processed_msg_ids.insert_one({"msg_id": msg_id, "created_at": datetime.utcnow()})

    try:
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        brand_phone = clean_phone(brand.get('brand_phone'))
        sender = clean_phone(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = msg_data.get("text", {}).get("body") or ""
        mgr_phone = clean_phone(brand.get("manager_phone"))

        # SANDBOX/OUTBOUND DETECTION
        direction = str(payload.get("direction", "")).lower()
        is_dashboard = (direction == "outbound") or (sender == brand_phone)
        
        print(f"\n--- EVENT | Sender: {sender} | Human_Active_Source: {is_dashboard} ---")

        # --- 1. MANAGER TRACKING (Muting Logic) ---
        if is_dashboard or (sender == mgr_phone):
            customer = clean_phone(msg_data.get("to") or msg_data.get("recipient_id") or sender)
            if customer and user_text:
                chat_history.insert_one({
                    "phone_number": customer, "manager_msg": user_text,
                    "is_human_active": True, "last_human_interaction": datetime.utcnow(), "timestamp": datetime.utcnow()
                })
                # Set/Reset the 5-minute silence window
                chat_history.update_many({"phone_number": customer}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            return {"status": "manager_logged"}

        # --- 2. SEMANTIC TRIAGE (The Guardrail) ---
        if user_text and check_triage(user_text):
            print("⚠️ ESCALATION TRIGGERED.")
            chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            if mgr_phone:
                send_text(mgr_phone, f"⚠️ *URGENT ESCALATION*: Athlete {sender} says: {user_text}")
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            return {"status": "escalated"}

        # --- 3. 5-MINUTE SLIDING WINDOW ---
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=5):
                print("🔇 AI MUTED. Window extended by athlete reply.")
                chat_history.update_many({"phone_number": sender}, {"$set": {"last_human_interaction": datetime.utcnow()}})
                if user_text: # Log silent history for context injection later
                    chat_history.insert_one({"phone_number": sender, "user_msg": user_text, "timestamp": datetime.utcnow()})
                return {"status": "muted"}
            else:
                print("🔊 5-MIN EXPIRED. AI resuming duties.")
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LEAD GEN ---
        location = msg_data.get("location")
        if location:
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = math.sqrt((u_lat - s['lat'])**2 + (u_lon - s['lon'])**2)
                best = min(stores, key=lambda x: x["dist"])
                chat_history.insert_one({
                    "phone_number": sender, "user_msg": "[Location Pin]", 
                    "ai_reply": f"SYSTEM: Found {best.get('store_name')}", "goal_reached": True, "timestamp": datetime.utcnow()
                })
                send_text(sender, f"📍 *Nearest Nike Store Found!*\n\nName: {best.get('store_name')}\n\n{brand.get('signature')}")
                if mgr_phone:
                    send_text(mgr_phone, f"🚨 *NEW LEAD*: {sender} is visiting {best.get('store_name')}.")
                return {"status": "routed"}

        # --- 5. AI ENGINE (With Context Injection) ---
        if not user_text: return Response(status_code=200)
        
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        history.reverse()
        
        # Explicit constraints to prevent "Manager Rachel" hallucinations
        system_prompt = f"""
        You are a Nike Concierge. Persona: {brand.get('persona')}
        CONSTRAINTS:
        - You are an AI, NEVER pretend to be a human manager.
        - NEVER negotiate price or offer custom discounts.
        - If a human was recently active, acknowledge their previous points.
        """
        
        messages = [{"role": "system", "content": system_prompt}]
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
            if doc.get("manager_msg"): messages.append({"role": "assistant", "content": f"[Manager previously said]: {doc.get('manager_msg')}"})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(
            messages=messages, model="llama-3.1-8b-instant", max_tokens=300
        )
        ai_reply = completion.choices[0].message.content
        chat_history.insert_one({"phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, "timestamp": datetime.utcnow()})
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"💥 ERROR: {e}")
        return Response(status_code=200)
    return Response(status_code=200)

@app.get("/")
def home(): return {"status": "Nike Retail OS v2.5 - Scalable & Secure"}