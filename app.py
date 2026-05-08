import streamlit as st
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
import random
import time

# ==========================================
# 0. 頁面基本設定
# ==========================================
st.set_page_config(page_title="雙人成語接龍", page_icon="🔗", layout="centered")

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

# 表情貼清單
AVATAR_LIST = ["🥴", "🤩", "🤓", "😎", "🥸", "😇", "😉", "🫪", "👧", "🧒", "👦", "👩", "🧑", "👨", "👩‍🦰", "🧑‍🦰", "👨‍🦰", "👱‍♀️", "👱", "👱‍♂️", "👩‍🦳", "🧑‍🦳", "👨‍🦳", "👩‍🦲", "🧑‍🦲"]

# ==========================================
# 2. Google Sheets 讀寫邏輯
# ==========================================
@st.cache_data(ttl=5)
def get_room_history(room_name):
    try:
        worksheet = spreadsheet.worksheet(room_name)
    except gspread.WorksheetNotFound:
        # 如果是新房間，建立 5 欄的表頭
        worksheet = spreadsheet.add_worksheet(title=room_name, rows="1000", cols="5")
        worksheet.append_row(["Timestamp", "User", "Text", "Type", "Avatar"])
        return []
    records = worksheet.get_all_records()
    return records

def save_message(room_name, user, text, msg_type="chat", avatar=""):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    worksheet = spreadsheet.worksheet(room_name)
    # 寫入五個欄位
    worksheet.append_row([timestamp, user, text, msg_type, avatar])
    st.cache_data.clear()

def clear_game_data(room_name):
    """清空指定房間除了標題以外的所有內容"""
    worksheet = spreadsheet.worksheet(room_name)
    # 刪除第 2 列到最後一列
    rows_count = len(worksheet.get_all_values())
    if rows_count > 1:
        worksheet.delete_rows(2, rows_count)
    st.cache_data.clear()

# ==========================================
# 3. 彈窗與按鈕邏輯
# ==========================================
@st.dialog("⚠️ 確定要重新開始嗎？")
def confirm_restart_dialog(room_name):
    st.write(f"這將會清空「{room_name}」房間的所有對話紀錄且無法復原。")
    if st.button("確定清空，重新開始", type="primary", use_container_width=True):
        clear_game_data(room_name)
        st.success("遊戲已重置！")
        time.sleep(1)
        st.rerun()

# ==========================================
# 4. 主畫面邏輯
# ==========================================
st.title("🔗 雙人成語接龍")

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

        # 角色名稱與頭像記憶邏輯
        player_options = []
        player_avatars = {}
        if room_choice != "--- 建立新房間 ---" and final_room_name:
            records = get_room_history(final_room_name)
            for r in records:
                u = str(r.get("User", ""))
                if u and u not in ["System", "Referee (AI)"]:
                    # 抓取該玩家最後一次使用的頭像，若無則預設為 😎
                    a = str(r.get("Avatar", ""))
                    player_avatars[u] = a if a else "😎"
            player_options = sorted(list(player_avatars.keys()))

        player_choice = st.selectbox("選擇身份", options=["--- 使用新名字 ---"] + player_options)
        
        # 判斷是否為新玩家，來決定要不要顯示頭像選擇器
        if player_choice == "--- 使用新名字 ---":
            col_p, col_a = st.columns([2, 1])
            with col_p:
                final_player_name = st.text_input("名字", key="new_player_input")
            with col_a:
                selected_avatar = st.selectbox("選擇頭像貼", options=AVATAR_LIST)
        else:
            final_player_name = player_choice
            selected_avatar = player_avatars.get(final_player_name, "😎")
            st.success(f"歡迎回來！你的專屬頭像：{selected_avatar}")

        st.write("") # 排版留白
        if st.button("🚀 確認進入", type="primary", use_container_width=True):
            if final_room_name and final_player_name:
                st.session_state['room'] = final_room_name
                st.session_state['player'] = final_player_name
                st.session_state['avatar'] = selected_avatar
                st.cache_data.clear()
                st.rerun()
            else:
                st.warning("請完整填寫房間與名字！")

# --- 狀態二：遊戲室 ---
else:
    current_room = st.session_state['room']
    current_player = st.session_state['player']
    current_avatar = st.session_state.get('avatar', '😎')
    chat_history = get_room_history(current_room)

    # ==========================================
    # 5. 側邊欄控制台
    # ==========================================
    with st.sidebar:
        st.header("🎮 遊戲控制台")
        st.write(f"📍 房間：{current_room}")
        st.write(f"👤 身份：{current_player} {current_avatar}")
        st.divider()
        
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
        # 清除遊戲按鈕
        if st.button("🧹 清除遊戲重新開始", use_container_width=True):
            confirm_restart_dialog(current_room)

        if st.button("🚪 返回大廳", use_container_width=True):
            del st.session_state['room']
            del st.session_state['player']
            st.rerun()

    # ==========================================
    # 6. 聊天室主畫面 (局部更新)
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
                    msg_type = msg.get("Type", "chat")
                    msg_user = msg.get("User", "")
                    msg_text = msg.get("Text", "")
                    msg_avatar = msg.get("Avatar", "😎") # 讀取該訊息發出時的頭像

                    if msg_type == "system":
                        st.info(msg_text)
                    elif msg_type == "referee":
                        with st.chat_message("ai"):
                            st.write(msg_text)
                    else:
                        is_self = (msg_user == player_name)
                        # 使用儲存在該則訊息中的頭像
                        with st.chat_message("user", avatar=msg_avatar):
                            st.write(f"**{msg_user}**: {msg_text}")

    display_chat_room(current_room, current_player)

    user_input = st.chat_input("輸入你的成語...")
    if user_input:
        save_message(current_room, current_player, user_input, "chat", current_avatar)
        st.rerun()
