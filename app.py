import streamlit as st
import google.generativeai as genai
import random
import time
from streamlit_autorefresh import st_autorefresh
# 匯入 Google Sheets 操作套件
# import gspread
# from oauth2client.service_account import ServiceAccountCredentials

# ==========================================
# 1. 遊戲設定與自動更新 (解決併發問題)
# ==========================================
# 每 5000 毫秒 (5秒) 自動重新整理一次頁面
# 這樣就算對方發言，5秒內你的畫面也會自動抓取最新進度
count = st_autorefresh(interval=5000, limit=None, key="room_sync")

# ==========================================
# 2. Google Sheets 快取讀寫邏輯 (解決 API 效能問題)
# ==========================================
# 實際開發時，把認證與連線寫在這裡
# scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
# creds = ServiceAccountCredentials.from_json_keyfile_name("your-secret.json", scope)
# client = gspread.authorize(creds)
# spreadsheet = client.open("Idiom_Game_DB")

# 使用 ttl=5 (5秒過期快取)，避免每次重新整理都去呼叫 Google Sheets API
@st.cache_data(ttl=5)
def get_room_history(room_name):
    """從 Google Sheets 讀取該房間的歷史對話"""
    # 實際串接邏輯：
    # try:
    #     worksheet = spreadsheet.worksheet(room_name)
    # except gspread.WorksheetNotFound:
    #     worksheet = spreadsheet.add_worksheet(title=room_name, rows="1000", cols="4")
    #     worksheet.append_row(["Timestamp", "User", "Text", "Type"]) # 寫入標題
    # 
    # records = worksheet.get_all_records()
    # return records
    
    # 雛形階段：模擬回傳 (如果 session_state 裡有東西就回傳，模擬從資料庫抓資料)
    if f"db_{room_name}" not in st.session_state:
        st.session_state[f"db_{room_name}"] = []
    return st.session_state[f"db_{room_name}"]

def save_message(room_name, user, text, msg_type="chat"):
    """將新訊息寫入 Google Sheets，並清除快取以強制抓取最新資料"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    
    # 實際串接邏輯：
    # worksheet = spreadsheet.worksheet(room_name)
    # worksheet.append_row([timestamp, user, text, msg_type])
    
    # 雛形階段模擬：
    new_msg = {"Timestamp": timestamp, "User": user, "Text": text, "Type": msg_type}
    if f"db_{room_name}" not in st.session_state:
         st.session_state[f"db_{room_name}"] = []
    st.session_state[f"db_{room_name}"].append(new_msg)

    # 【關鍵】寫入新資料後，馬上清除讀取快取！
    # 這樣程式重新執行時，get_room_history 才會去抓取包含剛剛發言的最新資料
    st.cache_data.clear()

# ==========================================
# 3. 側邊欄：房間與玩家設定
# ==========================================
with st.sidebar:
    st.header("🚪 遊戲大廳")
    room_name = st.text_input("輸入房間名稱", value=st.session_state.get('room', ''))
    player_name = st.text_input("你的名字", value=st.session_state.get('player', ''))
    
    if st.button("進入房間"):
        if room_name and player_name:
            st.session_state['room'] = room_name
            st.session_state['player'] = player_name
            # 進入新房間，清除舊快取
            st.cache_data.clear() 
            st.rerun()
        else:
            st.warning("請完整輸入房間與名稱！")

# ==========================================
# 4. 主畫面：聊天室與遊戲介面
# ==========================================
st.title("🔗 雙人成語接龍")

if 'room' in st.session_state and 'player' in st.session_state:
    current_room = st.session_state['room']
    current_player = st.session_state['player']
    st.caption(f"📍 目前位置：{current_room} | 👤 玩家：{current_player}")
    
    # 取得房間對話紀錄 (有快取保護，不會瘋狂吃 API 額度)
    chat_history = get_room_history(current_room)
    
    # --- 控制面板 ---
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🎲 AI 隨機出題並抽籤", use_container_width=True):
            starting_idiom = "一馬當先" # 之後串 Gemini
            players = [current_player, "女友"] # 雛形寫死
            first_player = random.choice(players)
            
            system_msg = f"【系統廣播】遊戲開始！初始成語為「**{starting_idiom}**」。由 {first_player} 先開始！"
            save_message(current_room, "System", system_msg, "system")
            st.rerun()
            
    with col2:
        if st.button("⚖️ 呼叫 AI 裁判", use_container_width=True):
            # 抓取最後一句非系統的玩家發言
            last_msg = next((m['Text'] for m in reversed(chat_history) if m['Type'] == 'chat'), None)
            
            if last_msg:
                judge_result = f"✅ 裁判判定：「{last_msg}」是標準成語！" # 之後串 Gemini
                save_message(current_room, "Referee (AI)", judge_result, "referee")
                st.rerun()
            else:
                st.toast("目前還沒有人輸入成語喔！")

    st.divider()

    # --- 顯示歷史對話 ---
    for msg in chat_history:
        if msg["Type"] == "system":
            st.info(msg["Text"])
        elif msg["Type"] == "referee":
            with st.chat_message("ai"):
                st.write(msg["Text"])
        else:
            is_self = (msg["User"] == current_player)
            avatar = "😎" if is_self else "👩"
            with st.chat_message("user", avatar=avatar):
                st.write(f"**{msg['User']}**: {msg['Text']}")

    # --- 輸入框 ---
    user_input = st.chat_input("輸入你的成語...")
    if user_input:
        save_message(current_room, current_player, user_input, "chat")
        st.rerun()

else:
    st.info("👈 請先從左側選單輸入房間名稱與名字來進入遊戲。")
