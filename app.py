import streamlit as st
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from streamlit_autorefresh import st_autorefresh
import random
import time

# ==========================================
# 0. 頁面基本設定
# ==========================================
st.set_page_config(page_title="雙人成語接龍", page_icon="🔗", layout="centered")

# ==========================================
# 1. 初始化設定 (Gemini & Google Sheets)
# ==========================================
# 讀取 Gemini API Key 並設定模型
try:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-3.1-flash-lite')
except Exception as e:
    st.error("請確認是否已在 Streamlit Secrets 中設定好 `GEMINI_API_KEY`！")
    st.stop()

# 設定 Google Sheets 連線
try:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=scopes
    )
    client = gspread.authorize(credentials)
    
    # ⚠️ 請確保你已經在 Google Drive 建立了一個名為 "Idiom_Game_DB" 的試算表
    spreadsheet = client.open("Idiom_Game_DB")
except Exception as e:
    st.error(f"Google Sheets 連線失敗！詳細錯誤原因：{e}")
    st.stop()

# ==========================================
# 定義 Gemini 安全設定 (允許所有字詞)
# ==========================================
custom_safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# ==========================================
# 2. 自動更新機制
# ==========================================
# 每 10000 毫秒 (10秒) 自動重新執行一次網頁，避開與 API 等待時間的衝突
st_autorefresh(interval=10000, limit=None, key="room_sync")

# ==========================================
# 3. Google Sheets 快取與讀寫邏輯
# ==========================================
@st.cache_data(ttl=5)
def get_room_history(room_name):
    """從 Google Sheets 讀取該房間的歷史對話，設定 5 秒快取避免 API 超載"""
    try:
        worksheet = spreadsheet.worksheet(room_name)
    except gspread.WorksheetNotFound:
        # 如果房間不存在，就建立一個新的工作表
        worksheet = spreadsheet.add_worksheet(title=room_name, rows="1000", cols="4")
        worksheet.append_row(["Timestamp", "User", "Text", "Type"]) # 寫入標題列
        return []
    
    # 取得所有紀錄，略過第一行的標題
    records = worksheet.get_all_records()
    return records

def save_message(room_name, user, text, msg_type="chat"):
    """將新訊息寫入 Google Sheets，並清除快取以強制抓取最新資料"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    worksheet = spreadsheet.worksheet(room_name)
    worksheet.append_row([timestamp, user, text, msg_type])
    
    # 寫入新資料後，馬上清除讀取快取，確保下次重繪畫面時能抓到這筆最新發言
    st.cache_data.clear()

# ==========================================
# 4. 側邊欄：房間與玩家設定
# ==========================================
with st.sidebar:
    st.header("🚪 遊戲大廳")
    room_name = st.text_input("輸入房間名稱", value=st.session_state.get('room', ''))
    player_name = st.text_input("你的名字", value=st.session_state.get('player', ''))
    
    if st.button("進入房間"):
        if room_name and player_name:
            st.session_state['room'] = room_name
            st.session_state['player'] = player_name
            st.cache_data.clear() # 進入新房間，強制清除快取
            st.rerun()
        else:
            st.warning("請完整輸入房間與名稱！")

# ==========================================
# 5. 主畫面：聊天室與遊戲介面
# ==========================================
st.title("🔗 雙人成語接龍")

if 'room' in st.session_state and 'player' in st.session_state:
    current_room = st.session_state['room']
    current_player = st.session_state['player']
    st.caption(f"📍 目前位置：{current_room} | 👤 玩家：{current_player}")
    
    # 取得房間對話紀錄
    chat_history = get_room_history(current_room)
    
    # --- 控制面板 ---
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🎲 AI 隨機出題", use_container_width=True):
            with st.status("AI 正在想題目，請稍候...", expanded=True) as status:
                try:
                    prompt = "請給出一個有趣的繁體中文四字成語，直接回傳四個字即可，不要有任何標點符號或解釋。"
                    # 加入安全設定，解除封印
                    response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                    
                    if response and response.text:
                        starting_idiom = response.text.strip()
                        if len(starting_idiom) > 10:
                             starting_idiom = starting_idiom[:4]
                        
                        system_msg = f"【系統廣播】遊戲開始！初始成語為「**{starting_idiom}**」。由大家自由開始接龍！"
                        save_message(current_room, "System", system_msg, "system")
                        status.update(label="出題成功！", state="complete", expanded=False)
                        st.rerun()
                    else:
                        st.error("AI 回傳了空的內容，請再試一次。")
                except Exception as e:
                    st.error(f"AI 出題失敗，原因：{str(e)}")
            
    with col2:
        if st.button("⚖️ 呼叫 AI 裁判", use_container_width=True):
            # 抓取最後一句玩家發言
            last_msg = next((m['Text'] for m in reversed(chat_history) if m['Type'] == 'chat'), None)
            
            if last_msg:
                with st.status("裁判正在看卷中...", expanded=True) as status:
                    try:
                        prompt = f"你是成語接龍裁判。請判斷「{last_msg}」是不是一個正式的中文成語。如果是請回傳『✅ 是成語』，如果不是請回傳『❌ 不是成語』，並附上一句簡單的解釋。請用繁體中文回答。"
                        # 加入安全設定，解除封印
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        judge_result = response.text.strip()
                        
                        save_message(current_room, "Referee (AI)", judge_result, "referee")
                        status.update(label="判定完成！", state="complete", expanded=False)
                        st.rerun()
                    except Exception as e:
                        st.error(f"裁判罷工了，原因：{str(e)}")
            else:
                st.toast("目前還沒有人輸入成語喔！")

    st.divider()

    # --- 顯示歷史對話 ---
    chat_container = st.container(height=400)
    with chat_container:
        if not chat_history:
            st.info("房間剛建立，趕快按下「AI 隨機出題」開始遊戲吧！")
        else:
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

