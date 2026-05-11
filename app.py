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

# 💡 增加 requested_by 參數，記錄是誰花錢買了提示
def save_message(room_name, user, text, msg_type="chat", avatar="", hint_answer="", requested_by=None):
    data = {
        "room_name": room_name,
        "user_name": user,
        "text": text,
        "type": msg_type,
        "avatar": avatar,
        "hint_answer": hint_answer,
        "requested_by": requested_by,  # 👈 關鍵新增
        "timestamp": firestore.SERVER_TIMESTAMP 
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
    # 加上防護：如果剛好遇到還沒產生時間戳的瞬間，就不執行避免崩潰
    if not target_timestamp:
        st.toast("資料同步中，請稍後一秒再刪除！", icon="⏳")
        return
        
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

def get_game_state(history):
    """終極狀態引擎：支援 AI 提示扣分、次數統計與退件機制"""
    STARTING_HP = 50  
    
    scores = {}
    is_game_over = False
    loser = None  
    sos_user = None
    sos_count = 0
    
    valid_idiom = None   
    pending_idiom = None 
    rejected = False     
    
    last_chat_user = None  
    players_order = []
    
    # 💡 新增：追蹤當前回合每個人用了幾次提示 { "玩家名": 次數 }
    current_round_hints = {}
    last_chat_prev_sos_user = None
    last_chat_prev_sos_count = 0

    for msg in history:
        m_type = msg.get("type", "chat")
        user = msg.get("user_name", "")
        text = msg.get("text", "")
        
        if m_type == "game_over":
            is_game_over = True
        elif m_type == "system" and "重新開始" in text:
            is_game_over = False 
            scores.clear()
            loser = None
            players_order.clear()
            valid_idiom = None
            pending_idiom = None
            rejected = False
            current_round_hints.clear()

        if user and user not in ["System", "Referee (AI)"]:
            if user not in scores:
                scores[user] = STARTING_HP
            if user not in players_order:
                players_order.append(user)

        if m_type == "system" and "題目為" in text:
            try:
                valid_idiom = text.split("「**")[1].split("**」")[0]
            except:
                valid_idiom = text[-4:]
            pending_idiom = valid_idiom
            rejected = False
            sos_user = None
            sos_count = 0
            current_round_hints.clear() # 💡 換題了，提示次數歸零

        elif m_type == "sos_start":
            sos_user = user
            sos_count = 0
            
        elif m_type == "chat" and user not in ["System", "Referee (AI)"]:
            if not rejected:
                valid_idiom = pending_idiom
            
            pending_idiom = text
            rejected = False
            last_chat_user = user
            last_chat_prev_sos_user = sos_user
            last_chat_prev_sos_count = sos_count
            
            # 💡 只要有人成功接龍，提示次數就重置
            current_round_hints.clear() 
            
            if sos_user == user:
                sos_count += 1
                if sos_count == 3:
                    scores[user] += 10
                    sos_user = None
                    sos_count = 0
            else:
                scores[user] += 10
                sos_user = None
                sos_count = 0
                
        elif m_type == "referee":
            # 判錯扣 20 分
            if "❌" in text and "不是成語" in text:
                rejected = True
                if last_chat_user and last_chat_user in scores:
                    scores[last_chat_user] -= 20
                    sos_user = last_chat_prev_sos_user
                    sos_count = last_chat_prev_sos_count
                    if scores[last_chat_user] <= 0:
                        is_game_over = True
                        loser = last_chat_user
            
            # 💡 AI 提示扣 5 分邏輯
            if "💡" in text:
                req_user = msg.get("requested_by")
                if req_user and req_user in scores:
                    scores[req_user] -= 5
                    current_round_hints[req_user] = current_round_hints.get(req_user, 0) + 1
                    # 檢查是否買提示買到破產出局
                    if scores[req_user] <= 0:
                        is_game_over = True
                        loser = req_user

    target_idiom = valid_idiom if rejected else pending_idiom

    if sos_user:
        current_turn = sos_user  
    elif not players_order:
        current_turn = None      
    elif last_chat_user in players_order:
        if rejected:
            current_turn = last_chat_user
        else:
            idx = players_order.index(last_chat_user)
            current_turn = players_order[(idx + 1) % len(players_order)]
    else:
        current_turn = players_order[0]

    sorted_scores = dict(sorted(scores.items(), key=lambda item: item[1], reverse=True))
    
    return {
        "scores": sorted_scores,
        "is_game_over": is_game_over,
        "loser": loser,
        "sos_user": sos_user,
        "sos_count": sos_count,
        "last_idiom": target_idiom, 
        "players_order": players_order,
        "current_turn": current_turn,
        "rejected": rejected,
        "round_hints": current_round_hints # 👈 傳出提示次數給 UI
    }

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
                
                # 💡 報到機制：檢查是否為第一次進入，是的話發送報到訊息排隊
                history = get_room_history(final_room_name)
                is_new = True
                for msg in history:
                    if msg.get("user_name") == final_player_name:
                        is_new = False
                        break
                
                if is_new:
                    save_message(final_room_name, final_player_name, "加入了房間 👋", "join_room", selected_avatar)
                
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
        st.header("🏆 戰況排行榜")
        state = get_game_state(chat_history)
        
        if state["scores"]:
            current_rank = 0
            prev_score = -1
            
            for player, score in state["scores"].items():
                # 已經出局的人不參與獎牌排名
                if score <= 0:
                    st.markdown(f"💀 **{player}**：出局")
                    continue
                
                # 💡 並列排名邏輯：只有當分數跟上一個人不一樣時，名次才會往下掉
                if score != prev_score:
                    current_rank += 1
                    prev_score = score
                    
                # 根據名次發放獎牌
                if current_rank == 1:
                    st.markdown(f"🥇 **{player}**：{score} 分")
                elif current_rank == 2:
                    st.markdown(f"🥈 **{player}**：{score} 分")
                elif current_rank == 3:
                    st.markdown(f"🥉 **{player}**：{score} 分")
                else:
                    st.markdown(f"🏅 **{player}**：{score} 分")
        else:
            st.info("尚無得分，趕快開始吧！")
            
        st.divider()
      
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

        # --- 裁判按鈕加入 Toast 小提示 ---
        if st.button("⚖️ AI 裁判判斷", use_container_width=True):
            last_msg = next((m['text'] for m in reversed(chat_history) if m['type'] == 'chat'), None)
            if last_msg:
                with st.status("裁判審核中...", expanded=False):
                    try:
                        prompt = f"請判斷「{last_msg}」以在台灣教育部最具權威的《成語典》或《重編國語辭典修訂本》判斷是否為正確的中文成語。請用繁體中文回答：『✅ 是成語』或『❌ 不是成語』，並簡述解釋。"
                        response = model.generate_content(prompt, safety_settings=custom_safety_settings)
                        ans = response.text.strip()
                        save_message(current_room, "Referee (AI)", ans, "referee")
                        
                        # 💡 判斷結果，跳出對應的加扣分提示！
                        if "❌" in ans and "不是成語" in ans:
                            st.toast("📉 完蛋了！裁判抓包，扣 20 分！", icon="💥")
                        else:
                            st.toast("✅ 裁判驗證通過！", icon="⚖️")
                        
                        # 👇 加上這行：強迫停頓 2 秒，讓泡泡顯示出來
                        time.sleep(2)    
                            
                        st.rerun()
                    except Exception as e:
                        st.error(f"判斷失敗：{e}")
                    
        st.divider()
        
        # --- 投降按鈕與結果顯示 (安全確認版) ---
        if not state["is_game_over"]:
            # 💡 使用 popover 製作確認視窗，既美觀又不會導致閃退
            with st.popover("🏳️ 我想不出來了 (投降)", use_container_width=True):
                st.warning("確定要結束這場遊戲並結算分數嗎？")
                
                if st.button("✅ 確定投降", key="final_surrender", type="primary", use_container_width=True):
                    # 確保帶入頭像與 game_over 類型
                    save_message(current_room, current_player, f"舉白旗投降了！遊戲結束！", "game_over", current_avatar)
                    st.rerun()
        else:
            if state["loser"]:
                st.error(f"💀 遊戲結算：**{state['loser']}** 分數扣光出局啦！")
            else:
                st.error("🏁 遊戲已結算！")
        
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
        
        locked_msg_ids = set()
        for i in range(len(history) - 1):
            if history[i].get("type") == "chat" and history[i+1].get("type") == "referee" and "❌" in history[i+1].get("text"):
                locked_msg_ids.add(history[i].get("id"))

        last_chat_msg_id = None
        for msg in reversed(history):
            if msg.get("type") == "chat":
                last_chat_msg_id = msg.get("id")
                break

        with chat_container:
            if not history:
                st.info("趕快開始出題吧！")
            else:
                for msg in history:
                    msg_type = msg.get("type", "chat")
                    msg_user = msg.get("user_name", "")
                    msg_text = msg.get("text", "")
                    msg_avatar = str(msg.get("avatar", "")).strip()
                    if msg_avatar not in AVATAR_LIST:
                        msg_avatar = "😎"

                    if msg_type == "system":
                        st.info(msg_text)
                    elif msg_type == "referee":
                        with st.chat_message("ai"):
                            st.write(msg_text)
                    elif msg_type == "sos_start":
                        st.warning(f"🚨 **{msg_user}** {msg_text}")
                    elif msg_type == "join_room":
                        st.caption(f"👋 **{msg_user}** {msg_text}")
                    else:
                        is_self = (msg_user == player_name)
                        is_locked = msg.get("id") in locked_msg_ids 
                        
                        with st.chat_message("user", avatar=msg_avatar):
                            st.markdown(f"**{msg_user}**")
                            with st.popover(msg_text, use_container_width=True):
                                if is_self:
                                    is_last_chat = (msg.get("id") == last_chat_msg_id)
                                    
                                    if is_locked:
                                        st.error("🔒 已被裁判扣分，無法消滅證據！")
                                    elif not is_last_chat:
                                        st.error("🔒 回合已過，無法刪除歷史紀錄！")
                                    else:
                                        unlock = st.checkbox("解鎖刪除功能", key=f"chk_del_{msg.get('id')}")
                                        if unlock:
                                            # 💡 拔除退費後門：只刪除這句話，前面買過的提示與扣分永遠存在！
                                            if st.button("🗑️ 確定刪除", key=f"btn_del_{msg.get('id')}", type="primary", use_container_width=True):
                                                delete_messages_from(room_name, msg.get("timestamp"))
                                                st.rerun()
                                else:
                                    st.caption("🚫 這是對手的發言，無法操作")

    display_chat_room(current_room, current_player)

    # 獲取最新狀態
    state = get_game_state(chat_history)
    
    # --- 1. 迷你輪次顯示器 & 專屬操作列 ---
    if state["is_game_over"]:
        st.markdown("<div style='text-align: center; color: red; font-size: 13px; margin: 5px 0;'>🏁 遊戲已結算</div>", unsafe_allow_html=True)
    elif state["current_turn"]:
        sos_suffix = ""
        if state["sos_user"] == state["current_turn"]:
            remaining = 3 - state["sos_count"]
            sos_suffix = f" (🚨 求生連擊中：剩下 {remaining} 個成語)"

        if state["current_turn"] == current_player:
            if state.get("rejected"):
                st.markdown(f"<div style='text-align: center; color: #f44336; font-size: 13px; margin: 5px 0; font-weight: bold;'>🚨 被裁判退件！請重新接續「{state['last_idiom']}」{sos_suffix}</div>", unsafe_allow_html=True)
            else:
                st.markdown(f"<div style='text-align: center; color: #4CAF50; font-size: 13px; margin: 5px 0;'>🟢 現在輪到你發言！{sos_suffix}</div>", unsafe_allow_html=True)
            
            # 💡 你的回合專屬操作列：求生與提示左右並排！
            if state["last_idiom"]:
                col1, col2 = st.columns(2)
                
                with col1:
                    if not state["sos_user"]:
                        with st.popover("🆘 發動求生", use_container_width=True):
                            st.info("⚠️ 必須連續接出 3 個成語才能過關！")
                            if st.button("🚨 確定發動", type="primary", key="btn_sos", use_container_width=True):
                                save_message(current_room, current_player, f"發動了「換聲調求生」！", "sos_start", current_avatar)
                                st.rerun()
                    else:
                        st.button("🚨 連擊進行中", disabled=True, use_container_width=True)

                with col2:
                    user_hint_count = state["round_hints"].get(current_player, 0)
                    my_score = state["scores"].get(current_player, 0)
                    
                    if user_hint_count >= 2:
                        st.button("🚫 提示已用盡", disabled=True, use_container_width=True)
                    elif my_score <= 5:
                        st.button("🚫 分數不足 5 分", disabled=True, use_container_width=True)
                    else:
                        hint_label = "💡 第一次 AI 提示" if user_hint_count == 0 else "💡 第二次 AI 提示"
                        with st.popover(hint_label, use_container_width=True):
                            if user_hint_count == 0:
                                st.warning("第一次提示：將顯示「第三個字」是什麼。")
                                st.caption("💰 消耗：5 分")
                                if st.button("✅ 確認獲取提示 (1/2)", key="hint_1", use_container_width=True):
                                    with st.spinner("查閱字典中..."):
                                        last_char = state["last_idiom"][-1]
                                        prompt = f"請給出一個以「{last_char}」開頭（或同音）的常見繁體中文四字成語。只需回傳該成語本身，不要標點。"
                                        res = model.generate_content(prompt, safety_settings=custom_safety_settings)
                                        ans = res.text.strip()[:4]
                                        # 抓取第三個字 (陣列 index 2)
                                        hint_char = ans[2] if len(ans) >= 3 else ans[-1]
                                        save_message(current_room, "Referee (AI)", f"💡 第一次提示：下一句的第三個字可以是「**{hint_char}**」", "referee", hint_answer=ans, requested_by=current_player)
                                        st.toast("已扣除 5 分！", icon="💰")
                                        time.sleep(1)
                                        st.rerun()
                            else:
                                st.warning("第二次提示：將解釋成語的意思。")
                                st.caption("💰 消耗：5 分")
                                if st.button("✅ 確認獲取提示 (2/2)", key="hint_2", use_container_width=True):
                                    with st.spinner("AI 解析中..."):
                                        target_idiom = None
                                        # 往回找自己上一次買的解答
                                        for m in reversed(chat_history):
                                            if m.get("type") == "referee" and m.get("requested_by") == current_player and m.get("hint_answer"):
                                                target_idiom = m.get("hint_answer")
                                                break
                                                
                                        if target_idiom:
                                            prompt = f"請解釋成語「{target_idiom}」的意思，但請注意：在解釋內容中絕對不能出現「{target_idiom}」這四個字中的任何一個字。請用繁體中文回答。"
                                            res = model.generate_content(prompt, safety_settings=custom_safety_settings)
                                            save_message(current_room, "Referee (AI)", f"💡 第二次提示 (意思)：\n{res.text.strip()}", "referee", hint_answer=target_idiom, requested_by=current_player)
                                            st.toast("已扣除 5 分！", icon="💰")
                                            time.sleep(1)
                                            st.rerun()
                                        else:
                                            st.error("找不到前一次的提示紀錄！")
        else:
            st.markdown(f"<div style='text-align: center; color: gray; font-size: 13px; margin: 5px 0;'>⏳ 目前輪到：<b>{state['current_turn']}</b>{sos_suffix}</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div style='text-align: center; color: gray; font-size: 13px; margin: 5px 0;'>🟢 遊戲剛開始，任何人皆可出題！</div>", unsafe_allow_html=True)

    # --- 2. 霸道輸入框 ---
    if state["is_game_over"]:
        st.chat_input("遊戲已結束，請點擊「清除遊戲重新開始」！", disabled=True)
        if chat_history and chat_history[-1].get("type") in ["game_over", "referee"]:
            st.balloons()
    else:
        is_my_turn = False
        # 💡 徹底移除特權：只有「真的輪到你」或「房間剛開沒人講過話」才能打字
        if state["current_turn"] is None or state["current_turn"] == current_player:
            is_my_turn = True 
            
        # 💡 輸入框的浮水印也跟著動態改變，提示這是第幾個求生詞
        if is_my_turn:
            if state.get("rejected"):
                input_placeholder = f"請重新接續「{state['last_idiom']}」..."
            elif state["sos_user"] == current_player:
                input_placeholder = f"請輸入第 {state['sos_count'] + 1} 個求生成語..."
            else:
                input_placeholder = "輸入你的成語..."
        else:
            input_placeholder = f"請等候 {state['current_turn']} 發言..."
            
        user_input = st.chat_input(input_placeholder, disabled=not is_my_turn)
        
        if user_input:
            can_ignore_tone = (state["sos_user"] == current_player and state["sos_count"] == 0)

            # 驗證接龍
            if check_idiom_connection(state["last_idiom"], user_input, ignore_tone=can_ignore_tone):
                save_message(current_room, current_player, user_input, "chat", current_avatar)
                
                # 💡 成功時跳出得分 Toast
                if state["sos_user"] == current_player and state["sos_count"] == 2:
                    save_message(current_room, "System", f"🎉 恭喜 **{current_player}** 完成 3 連擊！", "system")
                    st.toast("🎉 3 連擊逃生成功！+10 分", icon="🏆")
                elif not can_ignore_tone:
                    st.toast("✅ 接龍成功！+10 分", icon="✨")
                
                # 👇 加上這行：強迫停頓 2 秒，讓加分泡泡華麗現身
                time.sleep(2)    
                st.rerun()
                
            else:
                if can_ignore_tone:
                    st.toast("❌ 求生第一擊，至少要跟上一個字同音！", icon="🚨")
                elif state["sos_user"] == current_player:
                    st.toast(f"❌ 連擊期間需同音同調！接續「{state['last_idiom'][-1]}」", icon="🚨")
                else:
                    st.toast(f"❌ 讀音不合！請接續「{state['last_idiom'][-1]}」", icon="🚨")