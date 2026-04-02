import os
import streamlit as st
import pandas as pd
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# Run using: python -m streamlit run dashboard.py --server.fileWatcherType none

st.set_page_config(page_title="Nike Control Tower", layout="wide")
client = MongoClient(os.environ.get("MONGO_URI"))
db = client["EnterpriseAgent"]

st.title("👟 Nike India: Retail Operations Control Tower")

def get_data():
    return pd.DataFrame(list(db["ChatHistory"].find().sort("timestamp", -1)))

df = get_data()

if not df.empty:
    total_leads = len(df['phone_number'].unique())
    escalated = len(df[df['is_human_active'] == True]['phone_number'].unique())
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Athletes", total_leads)
    c2.metric("Human Handover Active", escalated)
    
    efficiency = f"{int((1 - escalated/total_leads)*100)}%" if total_leads > 0 else "N/A"
    c3.metric("AI Efficiency", efficiency)

    st.subheader("📋 Live Conversation Monitor")
    selected_user = st.selectbox("Select Athlete Phone to Audit", df['phone_number'].unique())
    
    user_df = df[df['phone_number'] == selected_user].sort_values("timestamp")
    
    for _, row in user_df.iterrows():
        if pd.notnull(row.get('user_msg')):
            st.chat_message("user").write(row['user_msg'])
        if pd.notnull(row.get('ai_reply')):
            st.chat_message("assistant", avatar="👟").write(row['ai_reply'])
        if pd.notnull(row.get('manager_msg')):
            st.chat_message("assistant", avatar="👨‍💼").write(f"**MANAGER:** {row['manager_msg']}")

else:
    st.write("Waiting for data...")