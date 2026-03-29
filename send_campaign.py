import requests
import time

# Credentials
KAPSO_API_KEY = "67d30f40d1f73775087a3287"
RECIPIENTS = ["919437725393", "918660855203"] # Samanta & Friend

def trigger_outbound():
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {
        "X-API-Key": KAPSO_API_KEY, 
        "Content-Type": "application/json"
    }
    
    for phone in RECIPIENTS:
        payload = {
            "message": {
                "phone_number": phone,
                "message_type": "text",
                "content": "Hey Athlete, the Rio Collection is here! 🏖️ Breezy shirts and fresh colors. Get ₹1000 additional shopping FREE at our Brigade Road store today! 🎁 Just Do It."
            }
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 200 or response.status_code == 201:
                print(f"✅ Campaign successfully sent to: {phone}")
            else:
                print(f"❌ Failed for {phone}: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"⚠️ Connection Error for {phone}: {e}")
            
        # Small delay to prevent rate limiting
        time.sleep(1)

if __name__ == "__main__":
    print("🚀 Initiating Proactive Marketing Campaign...")
    trigger_outbound()
    print("🏁 Campaign Trigger Sequence Complete.")
