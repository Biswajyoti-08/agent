import os, requests, certifi, math
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq
from dotenv import load_dotenv

# Load environment variables for security
load_dotenv()

app = FastAPI()

# --- 1. CLIENTS & DB SETUP ---
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]

chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]
processed_msg_ids = db["ProcessedMessages"] 

# --- HELPER FUNCTIONS ---

def get_distance(lat1, lon1, lat2, lon2):
    R = 6371 
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def send_text(phone, text):
    """X-RAY LOGGING: Catches Kapso rejections instead of failing silently."""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone, "content": text, "message_type": "text"}}
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in [200, 201]:
            print(f"❌ KAPSO SEND ERROR to {phone}: {response.text}")
        else:
            print(f"✅ Successfully sent message to {phone}")
        return response
    except Exception as e:
        print(f"❌ NETWORK ERROR sending to {phone}: {e}")
        return None

def check_triage(text):
    """STRICT GUARDRAIL: Binary output to prevent hallucinated handoffs."""
    triage_prompt = f"""
    Analyze this message from a Nike Athlete: "{text}"
    Does this message ask for a discount, mention a bulk order, ask for a refund, or express anger?
    Reply with ONLY the word "ESCALATE" if yes. 
    Reply with ONLY the word "AI_HANDLE" if no.
    """
    try:
        check = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": triage_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0
        )
        result = check.choices[0].message.content.strip().upper()
        print(f"🔍 TRIAGE LOG | User: '{text}' | Groq Decided: '{result}'")
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
        # KAPSO BLINDSPOT FIX: Detect outbound messages from the Kapso Web Sandbox
        direction = msg_data.get("direction")
        is_dashboard_reply = (direction == "outbound")
        
        sender = str(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = msg_data.get("text", {}).get("body") or ""
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = brand.get("manager_phone")

        print(f"\n--- NEW WEBHOOK EVENT | Sender/Target: {sender} | Direction: {direction} ---")

        # --- 1. MANAGER TRACKING (Handles WhatsApp Phone AND Kapso Web Sandbox) ---
        if sender == mgr_phone or is_dashboard_reply:
            print("👨‍💼 Manager message detected. Locking AI.")
            customer_phone = str(msg_data.get("to") or msg_data.get("recipient_id") or sender)
            
            if user_text:
                chat_history.insert_one({
                    "phone_number": customer_phone,
                    "manager_msg": user_text,
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(),
                    "timestamp": datetime.utcnow()
                })
                # Lock the AI and reset the global timer for this user
                chat_history.update_many(
                    {"phone_number": customer_phone}, 
                    {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}}
                )
            return {"status": "manager_logged"}

        # --- 2. STEALTH TRIAGE (Escalation Wall) ---
        if user_text and check_triage(user_text):
            print("⚠️ ESCALATION TRIGGERED! Halting AI.")
            chat_history.update_many(
                {"phone_number": sender}, 
                {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}}
            )
            print(f"🚨 Alerting Manager at {mgr_phone}...")
            send_text(mgr_phone, f"⚠️ *URGENT ESCALATION*: Athlete {sender} has a complex issue: {user_text}")
            send_text(sender, "That's a great question. Let me have our Store Manager grab those details for you. One moment! 👟")
            return {"status": "escalated_and_muted"}

        # --- 3. CHECK AI SILENCE STATUS (The Sliding Window Timer Fix) ---
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=5):
                print("🔇 AI is MUTED. Logging user reply and extending the 5-minute timer.")
                # Log the user's message while muted AND restart the 5-minute clock
                chat_history.insert_one({
                    "phone_number": sender, 
                    "user_msg": user_text, 
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(), 
                    "timestamp": datetime.utcnow()
                })
                chat_history.update_many(
                    {"phone_number": sender}, 
                    {"$set": {"last_human_interaction": datetime.utcnow()}}
                )
                return {"status": "ai_muted_human_is_handling"}
            else:
                print("🔊 5-minute silence window expired. AI regaining control.")
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LOGIC ---
        location = msg_data.get("location")
        if location:
            print("📍 Location pin received!")
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = get_distance(u_lat, u_lon, s.get("lat"), s.get("lon"))
                best = min(stores, key=lambda x: x["dist"])
                
                chat_history.insert_one({
                    "phone_number": sender, 
                    "user_msg": "[Shared Location Pin]", 
                    "ai_reply": f"SYSTEM_NOTE: Found {best.get('store_name')}.",
                    "goal_reached": True,
                    "timestamp": datetime.utcnow()
                })

                response = (
                    f"📍 *Nearest {brand.get('brand_name')} Store Found!*\n\n"
                    f"Name: {best.get('store_name')}\n"
                    f"Distance: {round(best['dist'], 1)} km\n"
                    f"Get Directions: {best.get('maps_url')}\n\n"
                    f"I'm here if you need anything else. {brand.get('signature', 'Just Do It.')}"
                )
                send_text(sender, response)
                
                if mgr_phone:
                    print(f"🚨 Attempting to send Location Alert to Manager at {mgr_phone}...")
                    send_text(mgr_phone, f"🚨 *NEW LEAD*: {sender} is heading to {best.get('store_name')}.")
                else:
                    print("⚠️ Manager phone not found in DB!")
                return {"status": "routed"}

        # --- 5. PROFESSIONAL AI CONCIERGE ---
        if not user_text: return {"status": "ignore"}
        
        print("🤖 Generating AI Response...")
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse() 
        
        instruction = (
            "You are a high-end Nike Concierge. BE CONCISE. Max 2 paragraphs. "
            "Never offer discounts, negotiate, or use slang. Never suggest wholesale ordering. "
            "If they ask for something you can't do, tell them a manager is needed."
        )
        if goal_reached:
            instruction += "The Athlete already found a store. Answer product questions directly."
        else:
            instruction += "Your goal is to guide them to click (📎 > Location) to share their pin."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')}"}]
        
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
            if doc.get("manager_msg"): messages.append({"role": "assistant", "content": f"[Human Manager intervened]: {doc.get('manager_msg')}"})
                
        messages.append({"role": "user", "content": user_text})

        # CUT-OFF FIX: Increased max_tokens from 150 to 300
        completion = groq_client.chat.completions.create(
            messages=messages, 
            model="llama-3.1-8b-instant",
            max_tokens=300
        )
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender, 
            "user_msg": user_text, 
            "ai_reply": ai_reply, 
            "timestamp": datetime.utcnow()
        })
        send_text(sender, ai_reply)
        print("✅ AI Response Sent successfully.")

    except Exception as e:
        print(f"💥 FATAL WEBHOOK ERROR: {e}")
        return Response(status_code=500)
        
    return Response(status_code=200)

@app.get("/")
def home(): return {"status": "Nike Retail OS Online - Enterprise Patch Applied"}