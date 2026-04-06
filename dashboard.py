import os
import streamlit as st
import pandas as pd
from pymongo import MongoClient
import certifi
from dotenv import load_dotenv

load_dotenv()

# --- Page Setup ---
st.set_page_config(page_title="Nike Control Tower", layout="wide", page_icon="👟")

# --- Database Connection ---
@st.cache_resource
def init_connection():
    return MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())

client = init_connection()
db = client["EnterpriseAgent"]

# --- Custom Styling ---
st.markdown("""
    <style>
    .main { background-color: #f5f5f5; }
    .stMetric { background-color: white; padding: 15px; border-radius: 10px; box-shadow: 2px 2px 5px rgba(0,0,0,0.1); }
    </style>
    """, unsafe_allow_html=True)

st.title("👟 Nike India: Retail Operations Control Tower")
st.sidebar.image("https://upload.wikimedia.org/wikipedia/commons/a/a6/Logo_NIKE.svg", width=100)
st.sidebar.header("Global Filters")

# --- Data Engine ---
def fetch_data():
    data = list(db["ChatHistory"].find().sort("timestamp", -1))
    return pd.DataFrame(data)

df = fetch_data()

if not df.empty:
    # --- Top Level Metrics ---
    total_users = len(df['phone_number'].unique())
    # Count leads that reached a store link
    hot_leads = len(df[df['ai_reply'].str.contains("Nearest Nike Store", na=False)]['phone_number'].unique())
    # Count humans active
    escalated = len(db["UserState"].distinct("phone_number", {"is_human_active": True}))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Athletes", total_users)
    c2.metric("Hot Leads (Store Visits)", hot_leads)
    c3.metric("Live Human Chats", escalated, delta_color="inverse")
    
    efficiency = f"{int((1 - escalated/total_users)*100)}%" if total_users > 0 else "100%"
    c4.metric("AI Automation Rate", efficiency)

    # --- The "One-Click" Audit Section ---
    st.divider()
    st.subheader("📋 Performance Audit: Conversation Monitor")
    
    col_a, col_b = st.columns([1, 3])
    
    with col_a:
        st.write("Select an Athlete to verify performance:")
        # Highlight athletes who are currently with a human
        unique_phones = df['phone_number'].unique()
        selected_user = st.selectbox("Search by Phone Number", unique_phones)
        
    with col_b:
        user_df = df[df['phone_number'] == selected_user].sort_values("timestamp")
        st.info(f"Viewing Audit Trail for: {selected_user}")
        
        for _, row in user_df.iterrows():
            # User Message
            if pd.notnull(row.get('user_msg')):
                with st.chat_message("user"):
                    st.write(row['user_msg'])
                    st.caption(f"Time: {row['timestamp']}")
            
            # AI Response
            if pd.notnull(row.get('ai_reply')):
                with st.chat_message("assistant", avatar="👟"):
                    st.markdown(f"**AI CONCIERGE:** {row['ai_reply']}")
            
            # Human Manager Response
            if pd.notnull(row.get('manager_msg')):
                with st.chat_message("assistant", avatar="👨‍💼"):
                    st.markdown(f"**MANAGER (AUDIT):** :blue[{row['manager_msg']}]")
                    st.caption("Human Intervention Verified")

else:
    st.error("No data found in ChatHistory. Ensure your webhook is logging correctly.")

# Auto-refresh button
if st.button("🔄 Refresh Live Feed"):
    st.rerun()