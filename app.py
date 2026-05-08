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
# 讀取 Gemini API Key 並設定模型
try:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
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
# 2. Google Sheets 快取與讀寫邏輯
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
    st.cache_data.clear()

# ==========================================
# 3. 主畫面切換邏輯 (大廳 vs 遊戲室)
# ==========================================
st.title("🔗 雙人成語接龍")

# --- 狀態一：還沒進入房間 (顯示登入大廳) ---
if 'room' not in st.session_state or 'player' not in st.session_state:
    
    # 使用 container(border=True) 弄一個漂亮的登入卡片
    with st.container(border=True):
        st.subheader("🚪 進入遊戲大廳")
        
        # --- 抓取試算表所有分頁 ---
        try:
            all_worksheets = spreadsheet.worksheets()
            room_options = [ws.title for ws in all_worksheets]
        except Exception:
            room_options = []
        
        room_choice = st.selectbox(
            "快速進入房間", 
            options=["--- 建立新房間 ---"] + room_options
        )
        
        if room_choice == "--- 建立新房間 ---":
            final_room_name = st.text_input("輸入新房間名稱 (例如：週末挑戰賽)", key="new_room_input")
        else:
            final_room_name = room_choice

        st.divider()

        # --- 抓取該房間不重複使用者 ---
        player_options = []
        if room_choice != "--- 建立新房間 ---" and final_room_name:
            try:
                ws = spreadsheet.worksheet(final_room_name)
                all_users = ws.col_values(2)
                exclude_list = ["User", "System", "Referee (AI)"]
                player_options = sorted(list(set([u for u in all_users if u not in exclude_list])))
            except Exception:
                player_options = []

        player_choice = st.selectbox(
            "選擇你的身份",
            options=["--- 使用新名字 ---"] + player_options
        )
        
        if player_choice == "--- 使用新名字 ---":
            final_player_name = st.text_input("你的名字", key="new_player_input")
        else:
            final_player_name = player_choice
        
        st.write("") # 增加一點排版空白
        
        # --- 進入按鈕 ---
        if st.button("🚀 確認進入", type="primary", use_container_width=True):
            if final_room_name and final_player_name:
                st.session_state['room'] = final_room_name
                st.session_state['player'] = final_player_name
                st.cache_data.clear() # 強制更新快取以載入新資料
                st.rerun()
            else:
                st.warning("請確保房間名稱與你的名字都有填寫喔！")


# --- 狀態二：已進入房間 (顯示遊戲介面) ---
else:
    current_room = st.session_state['room']
    current_player = st.session_state['player']
    
    # 頂部狀態列與退出按鈕
    colA, colB = st.columns([3, 1])
    with colA:
        st.caption(f"📍 目前位置：{current_room} | 👤 玩家：{current_player}")
    with colB:
        if st.button("🚪 返回大廳", use_container_width=True):
            del st.session_state['room']
            del st.session_state['player']
            st.rerun()
            
    chat_history = get_room_history(current_room)
    
    # --- 控制面板 ---
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🎲 AI 出題", use_container_width=True):
            with st.status("AI 正在想題目...", expanded=True) as status:
                try:
                    prompt = "請給出一個有趣的繁體中文四字成語，直接回傳四個字即可，不要有任何標點符號或解釋。"
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
                        st.error("AI 腦袋卡住了，請重試。")
                except Exception as e:
                    st.error(f"出題失敗：{str(e)}")
            
    with col2:
        if st.button("⚖️ AI 裁判", use_container_width=True):
            last_msg = next((m['Text'] for m in reversed(chat_history) if m['Type'] == 'chat'), None)
            if last_msg:
                with st.status("裁判正在看卷中...", expanded=True) as status:
                    try:
                        prompt = f"你是成語接龍裁判。請判斷「{last_msg}」是不是一個正式的中文成語。如果是請回傳『✅ 是成語』，如果不是請回傳『❌ 不是成語』，並附上一句簡單的解釋。請用繁體中文回答。"
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        save_message(current_room, "Referee (AI)", response.text.strip(), "referee")
                        status.update(label="判定完成！", state="complete", expanded=False)
                        st.rerun()
                    except Exception as e:
                        st.error(f"判定失敗：{str(e)}")
            else:
                st.toast("目前還沒有人輸入成語喔！")

    with col3:
        if st.button("💡 提示", use_container_width=True):
            last_msg = next((m['Text'] for m in reversed(chat_history) if m['Type'] == 'chat'), None)
            if last_msg:
                last_char = last_msg[-1] 
                with st.status("找尋提示中...", expanded=True) as status:
                    try:
                        prompt = f"請你想一個繁體中文的四字成語，這個成語的第一個字必須是「{last_char}」或者是與「{last_char}」發音相同的字。請只回傳該四字成語，不要包含任何標點符號或其他解釋。"
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        
                        if response and response.text:
                            ai_idiom = response.text.strip()
                            if len(ai_idiom) >= 4:
                                hint_char = ai_idiom[-2] 
                                hint_msg = f"💡 AI 提示：可以接一個成語，它的倒數第二個字是「**{hint_char}**」喔！"
                                save_message(current_room, "Referee (AI)", hint_msg, "referee")
                                status.update(label="提示完成！", state="complete", expanded=False)
                                st.rerun()
                            else:
                                st.error("提示格式錯誤，請再按一次！")
                        else:
                            st.error("AI 腦袋卡住了，請重試。")
                    except Exception as e:
                        st.error(f"提示失敗：{str(e)}")
            else:
                st.toast("目前還沒有人輸入成語喔！")

    st.divider()

    # ==========================================
    # 局部更新魔法：只在背景更新這個對話區塊，畫面不閃爍！
    # ==========================================
    @st.fragment(run_every=5)
    def display_chat_room(room_name, player_name):
        history = get_room_history(room_name)
        chat_container = st.container(height=400)
        with chat_container:
            if not history:
                st.info("房間剛建立，趕快按下「AI 隨機出題」開始遊戲吧！")
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

    # --- 下方的輸入框 ---
    user_input = st.chat_input("輸入你的成語...")
    if user_input:
        save_message(current_room, current_player, user_input, "chat")
        st.rerun()