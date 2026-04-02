import os, requests, certifi, math, re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq
from dotenv import load_dotenv

# Load security keys
load_dotenv()

app = FastAPI()

# 1. Clients & DB Setup
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
    """STRICT CLEANING: Strips +, spaces, and dashes to ensure raw digits only."""
    if not phone_str: return ""
    return re.sub(r'\D', '', str(phone_str))

def send_text(phone, text):
    """X-RAY SENDER: Prints the exact payload to Render Logs for real-time debugging."""
    target = clean_phone(phone)
    
    if not target or len(target) < 10:
        print(f"⚠️ ALERT FAILED: Invalid or empty phone number: '{target}'")
        return None

    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    
    payload = {
        "message": {
            "phone_number": target, 
            "content": text, 
            "message_type": "text"
        }
    }
    
    # CRITICAL: Watch this line in your Render Logs!
    print(f"📡 PAYLOAD BEING SENT: {payload}")
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in [200, 201]:
            print(f"❌ KAPSO REJECTION ({response.status_code}): {response.text}")
        else:
            print(f"✅ MESSAGE DELIVERED TO {target}")
        return response
    except Exception as e:
        print(f"❌ NETWORK ERROR: {e}")
        return None

def check_triage(text):
    """STRICT GUARDRAIL: Binary output to prevent hallucinated handoffs."""
    triage_prompt = f"""
    Analyze this Nike Athlete message: "{text}"
    Does this involve: 
    - Privacy/Identity concerns (e.g., "how did you get my number")
    - Manager requests or wanting a person
    - Bulk orders, discounts, or refunds
    - Anger or frustration
    Reply ONLY 'ESCALATE' or 'AI_HANDLE'.
    """
    try:
        check = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": triage_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0
        )
        result = check.choices[0].message.content.strip().upper()
        print(f"🔍 TRIAGE DECISION: {result}")
        return "ESCALATE" in result
    except Exception as e:
        print(f"❌ TRIAGE CRASH: {e}")
        return False

# --- CORE WEBHOOK ---

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    payload = await request.json()
    msg_data = payload.get("message", {})
    msg_id = msg_data.get("id") or msg_data.get("message_id")
    
    # 0. Anti-Spam / Idempotency
    if msg_id and processed_msg_ids.find_one({"msg_id": msg_id}):
        return Response(status_code=200)
    if msg_id:
        processed_msg_ids.insert_one({"msg_id": msg_id, "created_at": datetime.utcnow()})

    try:
        # Kapso Sandbox Detect: Outbound direction or missing "from"
        direction = str(payload.get("direction", "")).lower()
        is_dashboard = (direction == "outbound") or (not msg_data.get("from"))
        
        sender = clean_phone(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = msg_data.get("text", {}).get("body") or ""
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = clean_phone(brand.get("manager_phone"))

        print(f"\n--- NEW EVENT | Sender: {sender} | Dashboard: {is_dashboard} ---")

        # --- 1. MANAGER TRACKING ---
        if is_dashboard or (sender == mgr_phone):
            print("👨‍💼 Manager detected. Muting AI.")
            customer = clean_phone(msg_data.get("to") or msg_data.get("recipient_id") or sender)
            if customer and user_text:
                chat_history.insert_one({
                    "phone_number": customer, "manager_msg": user_text,
                    "is_human_active": True, "last_human_interaction": datetime.utcnow(), "timestamp": datetime.utcnow()
                })
                chat_history.update_many({"phone_number": customer}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            return {"status": "manager_active"}

        # --- 2. TRIAGE / ESCALATION ---
        if user_text and check_triage(user_text):
            print("⚠️ ESCALATION TRIGGERED.")
            chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            
            # Send alert to Manager if phone exists
            if mgr_phone:
                send_text(mgr_phone, f"⚠️ *URGENT ESCALATION*: Athlete {sender} says: {user_text}")
            else:
                print("❌ ERROR: Manager Phone not found in Database!")
                
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            return {"status": "escalated"}

        # --- 3. SILENCE TIMER (30 Min Sliding Window) ---
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=30):
                print("🔇 AI MUTED. Extending window.")
                chat_history.update_many({"phone_number": sender}, {"$set": {"last_human_interaction": datetime.utcnow()}})
                return {"status": "muted"}
            else:
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LOGIC ---
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
                
                response = f"📍 *Nearest Nike Store Found!*\n\nName: {best.get('store_name')}\n\n{brand.get('signature')}"
                send_text(sender, response)
                if mgr_phone:
                    send_text(mgr_phone, f"🚨 *NEW LEAD*: {sender} is visiting {best.get('store_name')}.")
                return {"status": "routed"}

        # --- 5. AI ENGINE ---
        if not user_text: return Response(status_code=200)
        
        print("🤖 AI GENERATING RESPONSE...")
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        history.reverse()
        
        messages = [{"role": "system", "content": f"You are a Nike Concierge. Max 2 paragraphs. Persona: {brand.get('persona')}"}]
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
            if doc.get("manager_msg"): messages.append({"role": "assistant", "content": f"[Manager previously said]: {doc.get('manager_msg')}"})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(
            messages=messages, 
            model="llama-3.1-8b-instant",
            max_tokens=300
        )
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, "timestamp": datetime.utcnow()
        })
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"💥 WEBHOOK ERROR: {e}")
        return Response(status_code=200)
    return Response(status_code=200)

@app.get("/")
def home(): return {"status": "Enterprise AI OS - Intelligence Active"}