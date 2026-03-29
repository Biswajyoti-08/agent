import requests
import time

KAPSO_API_KEY = "67d30f40d1f73775087a3287"
RECIPIENTS = ["919437725393", "918660855203"] 

def trigger_nike_rio_campaign():
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": KAPSO_API_KEY, "Content-Type": "application/json"}
    
    NIKE_RIO_IMAGE = "https://secure-images.nike.com/is/image/DotCom/310805_019_A_PREM?$SNKRS_COVER_WD$"
    
    for phone in RECIPIENTS:
        payload = {
            "message": {
                "phone_number": phone,
                "message_type": "image",
                "image": {"url": NIKE_RIO_IMAGE},
                "content": (
                    "🇧🇷 *The International City Series: RIO* 🌊\n\n"
                    "Athlete, the Air Jordan X 'Rio' has landed. Inspired by the beautiful Brazilian seaside "
                    "and the iconic Christ the Redeemer statue. Premium craftsmanship for a world-class city.\n\n"
                    "Exclusive stock available at the *Brigade Road* store today. Just Do It."
                )
            }
        }
        
        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code in [200, 201]:
                print(f"✅ Nike Rio Campaign delivered to: {phone}")
            else:
                print(f"❌ Error {response.status_code}: {response.text}")
        except Exception as e:
            print(f"⚠️ Connection Error: {e}")
            
        time.sleep(1)

if __name__ == "__main__":
    print("🚀 Triggering official Nike Air Jordan 10 'Rio' Campaign...")
    trigger_nike_rio_campaign()
