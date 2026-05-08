import streamlit as st
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
import random
import time

# ==========================================
# 0. 頁面基本設定
# ==========================================
st.set_page_config(page_title="成語接龍", page_icon="🔗", layout="centered")

# ==========================================
# 1. 初始化設定 (Gemini & Google Sheets)
# ==========================================
try:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
except Exception as e:
    st.error("請確認是否已在 Streamlit Secrets 中設定好 `GEMINI_API_KEY`！")
    st.stop()

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
    spreadsheet = client.open("Idiom_Game_DB")
except Exception as e:
    st.error(f"Google Sheets 連線失敗！詳細錯誤原因：{e}")
    st.stop()

# 定義 Gemini 安全設定
custom_safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# ==========================================
# 2. Google Sheets 讀寫邏輯
# ==========================================
@st.cache_data(ttl=5)
def get_room_history(room_name):
    try:
        worksheet = spreadsheet.worksheet(room_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=room_name, rows="1000", cols="4")
        worksheet.append_row(["Timestamp", "User", "Text", "Type"])
        return []
    records = worksheet.get_all_records()
    return records

def save_message(room_name, user, text, msg_type="chat"):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    worksheet = spreadsheet.worksheet(room_name)
    worksheet.append_row([timestamp, user, text, msg_type])
    st.cache_data.clear()

# ==========================================
# 3. 主畫面邏輯
# ==========================================
st.title("成語接龍🐉")

# --- 狀態一：登入大廳 ---
if 'room' not in st.session_state or 'player' not in st.session_state:
    with st.container(border=True):
        st.subheader("🚪 進入遊戲大廳")
        
        try:
            all_worksheets = spreadsheet.worksheets()
            room_options = [ws.title for ws in all_worksheets]
        except Exception:
            room_options = []
        
        room_choice = st.selectbox("快速進入房間", options=["--- 建立新房間 ---"] + room_options)
        if room_choice == "--- 建立新房間 ---":
            final_room_name = st.text_input("輸入新房間名稱", key="new_room_input")
        else:
            final_room_name = room_choice

        st.divider()

        player_options = []
        if room_choice != "--- 建立新房間 ---" and final_room_name:
            try:
                ws = spreadsheet.worksheet(final_room_name)
                all_users = ws.col_values(2)
                exclude_list = ["User", "System", "Referee (AI)"]
                player_options = sorted(list(set([u for u in all_users if u not in exclude_list])))
            except Exception:
                player_options = []

        player_choice = st.selectbox("選擇你的身份", options=["--- 使用新名字 ---"] + player_options)
        if player_choice == "--- 使用新名字 ---":
            final_player_name = st.text_input("你的名字", key="new_player_input")
        else:
            final_player_name = player_choice
        
        if st.button("🚀 確認進入", type="primary", use_container_width=True):
            if final_room_name and final_player_name:
                st.session_state['room'] = final_room_name
                st.session_state['player'] = final_player_name
                st.cache_data.clear()
                st.rerun()
            else:
                st.warning("請完整填寫房間與名字！")

# --- 狀態二：遊戲室 ---
else:
    current_room = st.session_state['room']
    current_player = st.session_state['player']
    chat_history = get_room_history(current_room)

    # ==========================================
    # 4. 側邊欄控制台 (僅在遊戲室顯示)
    # ==========================================
    with st.sidebar:
        st.header("🎮 遊戲控制台")
        st.caption(f"📍 房間：{current_room}")
        st.caption(f"👤 身份：{current_player}")
        st.divider()
        
        # 🎲 AI 出題按鈕
        if st.button("🎲 AI 隨機出題", use_container_width=True):
            with st.status("AI 思考中...", expanded=False):
                try:
                    prompt = "請給出一個常見的繁體中文四字成語，只需回傳成語本身。"
                    response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                    if response and response.text:
                        idiom = response.text.strip()[:4]
                        save_message(current_room, "System", f"【系統】遊戲開始！題目為「**{idiom}**」", "system")
                        st.rerun()
                except Exception as e:
                    st.error(f"出題失敗：{e}")

        # ⚖️ AI 裁判按鈕
        if st.button("⚖️ AI 裁判判斷", use_container_width=True):
            last_msg = next((m['Text'] for m in reversed(chat_history) if m['Type'] == 'chat'), None)
            if last_msg:
                with st.status("裁判審核中...", expanded=False):
                    try:
                        prompt = f"請判斷「{last_msg}」是否為正確的中文成語。請用繁體中文回答：『✅ 是成語』或『❌ 不是成語』，並簡述解釋。"
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        save_message(current_room, "Referee (AI)", response.text.strip(), "referee")
                        st.rerun()
                    except Exception as e:
                        st.error(f"判斷失敗：{e}")
            else:
                st.toast("目前尚無訊息可判斷")

        # 💡 AI 提示按鈕
        if st.button("💡 獲取 AI 提示", use_container_width=True):
            last_msg = next((m['Text'] for m in reversed(chat_history) if m['Type'] == 'chat'), None)
            if last_msg:
                with st.status("翻閱典籍中...", expanded=False):
                    try:
                        last_char = last_msg[-1]
                        prompt = f"請給出一個以「{last_char}」開頭（或同音）的四字成語。只回傳成語。"
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        if response and response.text:
                            hint_char = response.text.strip()[-2]
                            save_message(current_room, "Referee (AI)", f"💡 提示：下一句的倒數第二個字可以是「**{hint_char}**」", "referee")
                            st.rerun()
                    except Exception as e:
                        st.error(f"提示失敗：{e}")
        
        st.divider()
        if st.button("🚪 返回大廳", use_container_width=True):
            del st.session_state['room']
            del st.session_state['player']
            st.rerun()

    # ==========================================
    # 5. 聊天室主畫面 (局部更新)
    # ==========================================
    @st.fragment(run_every=5)
    def display_chat_room(room_name, player_name):
        history = get_room_history(room_name)
        chat_container = st.container(height=500)
        with chat_container:
            if not history:
                st.info("趕快開始出題吧！")
            else:
                for msg in history:
                    if msg["Type"] == "system":
                        st.info(msg["Text"])
                    elif msg["Type"] == "referee":
                        with st.chat_message("ai"):
                            st.write(msg["Text"])
                    else:
                        is_self = (msg["User"] == player_name)
                        avatar = "😎" if is_self else "👩"
                        with st.chat_message("user", avatar=avatar):
                            st.write(f"**{msg['User']}**: {msg['Text']}")

    display_chat_room(current_room, current_player)

    user_input = st.chat_input("輸入你的成語...")
    if user_input:
        save_message(current_room, current_player, user_input, "chat")
        st.rerun()
