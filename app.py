import streamlit as st
import google.generativeai as genai
import random
import time
import firebase_admin
from firebase_admin import credentials, firestore

# ==========================================
# 0. 頁面基本設定
# ==========================================
st.set_page_config(page_title="成語接龍", page_icon="🔗", layout="centered")

# ==========================================
# 1. 初始化設定 (Gemini)
# ==========================================
try:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-3.1-flash-lite')
except Exception as e:
    st.error("請確認是否已在 Streamlit Secrets 中設定好 `GEMINI_API_KEY`！")
    st.stop()

custom_safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

AVATAR_LIST = ["🥴", "🤩", "🤓", "😎", "🥸", "😇", "😉", "🫪", "👧", "🧒", "👦", "👩", "🧑", "👨", "👩‍🦰", "🧑‍🦰", "👨‍🦰", "👱‍♀️", "👱", "👱‍♂️", "👩‍🦳", "🧑‍🦳", "👨‍🦳", "👩‍🦲", "🧑‍🦲"]

# ==========================================
# 2. Firebase 連線與讀寫邏輯
# ==========================================
# 確保 Streamlit 重新執行時不會重複初始化 Firebase
if not firebase_admin._apps:
    try:
        cred_dict = dict(st.secrets["firebase_key"])
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        st.error(f"Firebase 初始化失敗，請檢查 Secrets 設定：{e}")
        st.stop()

db = firestore.client()
# 我們將所有對話存放在這個集合 (Collection) 裡
CHAT_COLLECTION = "chat_messages"

def get_room_history(room_name):
    # 根據房間名稱查詢，並依照時間戳記排序
    docs = db.collection(CHAT_COLLECTION).where("room_name", "==", room_name).order_by("timestamp").stream()
    history = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id  # 把 Firebase 產生的隨機文件 ID 存起來
        history.append(data)
    return history

def save_message(room_name, user, text, msg_type="chat", avatar="", hint_answer=""):
    data = {
        "room_name": room_name,
        "user_name": user,
        "text": text,
        "type": msg_type,
        "avatar": avatar,
        "hint_answer": hint_answer,
        "timestamp": firestore.SERVER_TIMESTAMP # 使用 Firebase 伺服器時間
    }
    db.collection(CHAT_COLLECTION).add(data)

def clear_game_data(room_name):
    # 找出該房間所有對話並批次刪除
    docs = db.collection(CHAT_COLLECTION).where("room_name", "==", room_name).stream()
    batch = db.batch()
    for doc in docs:
        batch.delete(doc.reference)
    batch.commit()

def delete_messages_from(room_name, target_timestamp):
    # 利用時間戳記，刪除大於等於該時間的所有對話
    docs = db.collection(CHAT_COLLECTION)\
             .where("room_name", "==", room_name)\
             .where("timestamp", ">=", target_timestamp)\
             .stream()
    batch = db.batch()
    for doc in docs:
        batch.delete(doc.reference)
    batch.commit()

def get_all_rooms():
    # Firebase 沒有直接撈出「不重複值」的語法，我們抓取所有房間名稱來過濾
    docs = db.collection(CHAT_COLLECTION).select(["room_name"]).stream()
    return list(set([doc.to_dict().get("room_name") for doc in docs if doc.to_dict().get("room_name")]))
    
# --- 管理員專用邏輯 ---
def delete_entire_room(room_name):
    """刪除整個房間的所有對話"""
    docs = db.collection(CHAT_COLLECTION).where("room_name", "==", room_name).stream()
    batch = db.batch()
    for doc in docs:
        batch.delete(doc.reference)
    batch.commit()

def delete_user_in_room(room_name, user_name):
    """刪除特定玩家在該房間的所有對話"""
    docs = db.collection(CHAT_COLLECTION).where("room_name", "==", room_name).where("user_name", "==", user_name).stream()
    batch = db.batch()
    for doc in docs:
        batch.delete(doc.reference)
    batch.commit()

def edit_message(doc_id, new_text):
    """修正特定訊息的內容"""
    db.collection(CHAT_COLLECTION).document(doc_id).update({"text": new_text})
    
from pypinyin import pinyin, Style

def check_idiom_connection(last_idiom, new_idiom, ignore_tone=False):
    """檢查成語接龍，支援『嚴格同調』與『忽略聲調(求生模式第一擊)』"""
    if not last_idiom or not new_idiom:
        return True
    
    last_char = last_idiom[-1]
    first_char = new_idiom[0]
    
    # 魔法在此：如果 ignore_tone 是 True，就用 Style.NORMAL (無聲調)；否則用 TONE3 (嚴格聲調)
    style = Style.NORMAL if ignore_tone else Style.TONE3
    
    last_char_pinyins = pinyin(last_char, style=style, heteronym=True)[0]
    first_char_pinyins = pinyin(first_char, style=style, heteronym=True)[0]
    
    return bool(set(last_char_pinyins).intersection(set(first_char_pinyins)))

def analyze_game_state(history):
    last_idiom = None
    sos_user = None
    sos_count = 0

    for msg in history:
        m_type = msg.get("type", "chat")
        text = msg.get("text", "")
        user = msg.get("user_name", "")

        if m_type == "system" and "題目為" in text:
            last_idiom = text.split("「**")[1].split("**」")[0]
            sos_user = None
            sos_count = 0
        elif m_type == "chat":
            last_idiom = text
            if sos_user == user:
                sos_count += 1
                if sos_count >= 3:  # 3 連擊達成，解除求生狀態
                    sos_user = None
                    sos_count = 0
            else:
                sos_user = None
                sos_count = 0
        elif m_type == "sos_start":
            sos_user = user
            sos_count = 0

    return last_idiom, sos_user, sos_count

# ==========================================
# 3. 彈窗邏輯
# ==========================================
@st.dialog("⚠️ 確定要重新開始嗎？")
def confirm_restart_dialog(room_name):
    st.write(f"這將會清空「{room_name}」房間的所有對話紀錄且無法復原。")
    if st.button("確定清空，重新開始", type="primary", use_container_width=True):
        clear_game_data(room_name)
        st.success("遊戲已重置！")
        time.sleep(1)
        st.rerun()

@st.dialog("🗑️ 刪除對話確認")
def confirm_delete_dialog(room_name, msg_text, timestamp):
    st.warning("確定要刪除這句話以及**之後的所有對話與 AI 紀錄**嗎？")
    st.info(f"**即將刪除：** {msg_text}")
    
    if st.button("✅ 確定刪除", type="primary", use_container_width=True):
        delete_messages_from(room_name, timestamp)
        st.success("對話已刪除！")
        time.sleep(1)
        st.rerun()
        
# --- 管理員密碼鎖彈窗 ---
ADMIN_PASSWORD = "0306"  # 👈 這裡設定你的管理員專屬密碼，你可以隨便改！

@st.dialog("🔐 權限驗證：刪除玩家對話")
def admin_delete_user_dialog(room_name, user_name):
    st.warning(f"即將刪除「{user_name}」在「{room_name}」的所有對話！")
    pwd = st.text_input("請輸入管理員密碼：", type="password", placeholder="輸入密碼...")
    
    if st.button("🚨 確認刪除", type="primary", use_container_width=True):
        if pwd == ADMIN_PASSWORD:
            delete_user_in_room(room_name, user_name)
            st.success(f"已清除 {user_name} 的訊息")
            time.sleep(1)
            st.rerun()
        else:
            st.error("❌ 密碼錯誤，拒絕存取！")

@st.dialog("🔐 權限驗證：毀滅式清空房間")
def admin_clear_room_dialog(room_name):
    st.error(f"⚠️ 嚴重警告：這將徹底清空「{room_name}」的所有資料，且無法復原！")
    pwd = st.text_input("請輸入管理員密碼：", type="password", key="pwd_clear", placeholder="輸入密碼...")
    
    if st.button("🧨 確定毀滅", type="primary", use_container_width=True):
        if pwd == ADMIN_PASSWORD:
            delete_entire_room(room_name)
            st.success("房間已徹底重置")
            time.sleep(1)
            st.rerun()
        else:
            st.error("❌ 密碼錯誤，拒絕存取！")

# ==========================================
# 4. 主畫面邏輯
# ==========================================
st.title("成語接龍🐉")

if 'room' not in st.session_state or 'player' not in st.session_state:
    with st.container(border=True):
        st.subheader("🚪 進入遊戲大廳")
        
        try:
            room_options = get_all_rooms()
        except Exception:
            room_options = []
        
        room_choice = st.selectbox("快速進入房間", options=["--- 建立新房間 ---"] + room_options)
        if room_choice == "--- 建立新房間 ---":
            final_room_name = st.text_input("輸入新房間名稱", key="new_room_input")
        else:
            final_room_name = room_choice

        st.divider()

        player_options = []
        player_avatars = {}
        if room_choice != "--- 建立新房間 ---" and final_room_name:
            records = get_room_history(final_room_name)
            for r in records:
                u = str(r.get("user_name", ""))
                if u and u not in ["System", "Referee (AI)"]:
                    a = str(r.get("avatar", ""))
                    player_avatars[u] = a if a else "😎"
            player_options = sorted(list(player_avatars.keys()))

        player_choice = st.selectbox("選擇身份", options=["--- 使用新名字 ---"] + player_options)
        
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

        st.write("")
        if st.button("🚀 確認進入", type="primary", use_container_width=True):
            if final_room_name and final_player_name:
                st.session_state['room'] = final_room_name
                st.session_state['player'] = final_player_name
                st.session_state['avatar'] = selected_avatar
                st.rerun()
            else:
                st.warning("請完整填寫房間與名字！")

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
            last_msg = next((m['text'] for m in reversed(chat_history) if m['type'] == 'chat'), None)
            if last_msg:
                with st.status("裁判審核中...", expanded=False):
                    try:
                        prompt = f"請判斷「{last_msg}」以在台灣教育部最具權威的《成語典》或《重編國語辭典修訂本》判斷是否為正確的中文成語。請用繁體中文回答：『✅ 是成語』或『❌ 不是成語』，並簡述解釋。"
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        save_message(current_room, "Referee (AI)", response.text.strip(), "referee")
                        st.rerun()
                    except Exception as e:
                        st.error(f"判斷失敗：{e}")
            else:
                st.toast("目前尚無訊息可判斷")

        if st.button("💡 獲取 AI 提示", use_container_width=True):
            last_rec = chat_history[-1] if chat_history else None
            
            if last_rec and last_rec.get("type") == "referee" and last_rec.get("hint_answer"):
                with st.status("AI 正在解析成語意思...", expanded=False):
                    try:
                        target_idiom = last_rec.get("hint_answer")
                        prompt = f"請解釋成語「{target_idiom}」的意思，但請注意：在解釋內容中絕對不能出現「{target_idiom}」這四個字中的任何一個字。請用繁體中文回答。"
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        save_message(current_room, "Referee (AI)", f"📖 意思提示：\n{response.text.strip()}", "referee")
                        st.rerun()
                    except Exception as e:
                        st.error(f"解析失敗：{e}")
            else:
                last_player_msg = next((m['text'] for m in reversed(chat_history) if m['type'] == 'chat'), None)
                if last_player_msg:
                    with st.status("翻閱典籍中...", expanded=False):
                        try:
                            last_char = last_player_msg[-1]
                            prompt = f"請給出一個以「{last_char}」開頭（或同音）的常見繁體中文四字成語。只需回傳該成語本身，不要標點。"
                            response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                            if response and response.text:
                                ai_idiom = response.text.strip()[:4]
                                hint_char = ai_idiom[-2]
                                save_message(
                                    current_room, 
                                    "Referee (AI)", 
                                    f"💡 字提示：下一句的倒數第二個字可以是「**{hint_char}**」", 
                                    "referee", 
                                    hint_answer=ai_idiom
                                )
                                st.rerun()
                        except Exception as e:
                            st.error(f"提示失敗：{e}")
                else:
                    st.toast("目前尚無訊息可提示")
                    
        if st.button("🆘 換聲調求生 (需連擊3次)", use_container_width=True):
            last_idiom, sos_user, sos_count = analyze_game_state(chat_history)
            if sos_user:
                st.warning(f"現在是 {sos_user} 的求生時間，你不能按！")
            elif not last_idiom:
                st.warning("遊戲還沒開始啦！")
            else:
                # 寫入一筆特殊的 sos_start 訊息
                save_message(current_room, current_player, f"🚨 發動了「換聲調求生」！必須連續接出 3 個成語！", "sos_start")
                st.rerun()
        
        st.divider()
        if st.button("🧹 清除遊戲重新開始", use_container_width=True):
            confirm_restart_dialog(current_room)

        if st.button("🚪 返回大廳", use_container_width=True):
            del st.session_state['room']
            del st.session_state['player']
            st.rerun()
            
        # 在側邊欄底部加入管理功能
        st.divider()
        with st.expander("🛠️ 進階管理區"):
            st.caption("僅限管理員操作")
            
            # 功能 1：刪除特定玩家
            target_user = st.text_input("要刪除的人名", placeholder="輸入完整名字")
            if st.button("🗑️ 刪除該員對話", use_container_width=True):
                if target_user:
                    # 改為呼叫密碼彈窗，而不是直接刪除
                    admin_delete_user_dialog(current_room, target_user)
                else:
                    st.warning("請先輸入人名")
            
            st.write("---")
            
            # 功能 2：清空房間
            if st.button("🧨 毀滅式清空房間", type="primary", use_container_width=True):
                # 改為呼叫密碼彈窗，而不是直接刪除
                admin_clear_room_dialog(current_room)
        
    # ==========================================
    # 6. 聊天室主畫面 (局部更新)
    # ==========================================
    @st.fragment(run_every=2)
    def display_chat_room(room_name, player_name):
        history = get_room_history(room_name)
        chat_container = st.container(height=500)
        with chat_container:
            if not history:
                st.info("趕快開始出題吧！")
            else:
                for msg in history:
                    msg_type = msg.get("type", "chat")
                    msg_user = msg.get("user_name", "")
                    msg_text = msg.get("text", "")
                    msg_avatar = msg.get("avatar")
                    if not msg_avatar: msg_avatar = "😎"

                    if msg_type == "system":
                        st.info(msg_text)
                    elif msg_type == "referee":
                        with st.chat_message("ai"):
                            st.write(msg_text)
                    elif msg_type == "sos_start": st.warning(f"**{msg_user}** {msg_text}")
                        
                    else:
                        is_self = (msg_user == player_name)
                        with st.chat_message("user", avatar=msg_avatar):
                            if is_self:
                                # 傳入 msg.get('timestamp') 作為刪除條件
                                if st.button(f"**{msg_user}**: {msg_text}", key=f"del_{msg.get('id')}", type="tertiary", help="點擊刪除此對話及後續所有紀錄"):
                                    confirm_delete_dialog(room_name, msg_text, msg.get("timestamp"))
                            else:
                                st.write(f"**{msg_user}**: {msg_text}")

    display_chat_room(current_room, current_player)

    user_input = st.chat_input("輸入你的成語...")
    if user_input:
        last_idiom, sos_user, sos_count = analyze_game_state(chat_history)

        # 狀況 A：別人正在求生，你不准吵！
        if sos_user and sos_user != current_player:
            st.error(f"🤫 噓！現在是 {sos_user} 的生死存亡關頭，請讓他完成 3 連擊！")
            st.stop() # 終止執行，不讓訊息送出

        # 決定能不能換聲調：只有在「自己是求生者」且「正要接第 1 個字 (sos_count == 0)」時才可以
        can_ignore_tone = (sos_user == current_player and sos_count == 0)

        # 開始驗證
        if check_idiom_connection(last_idiom, user_input, ignore_tone=can_ignore_tone):
            # 驗證成功，存入資料庫
            save_message(current_room, current_player, user_input, "chat", current_avatar)

            # 如果剛好完成第 3 擊，系統自動廣播慶祝
            if sos_user == current_player and sos_count == 2:
                save_message(current_room, "System", f"🎉 恭喜 **{current_player}** 成功完成 3 連擊，從地獄歸來！遊戲繼續！", "system")

            st.rerun()
        else:
            # 驗證失敗，跳出不同情境的警告
            if can_ignore_tone:
                st.toast("❌ 發動求生的第一擊，至少要跟上一個字同音（可換聲調）！", icon="🚨")
            elif sos_user == current_player:
                st.toast(f"❌ 連擊期間必須嚴格同音同調！請接續「{last_idiom[-1]}」", icon="🚨")
            else:
                st.toast(f"❌ 讀音不合！請接續「{last_idiom[-1]}」的同音同調字！", icon="🚨")