import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore
from pypinyin import pinyin, Style
from google.cloud.firestore_v1 import DELETE_FIELD

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

if not firebase_admin._apps:
    try:
        cred_json = json.loads(os.environ.get("FIREBASE_KEY", "{}"))
        cred = credentials.Certificate(cred_json)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Firebase 初始化失敗: {e}")

db = firestore.client()
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-3.1-flash-lite')

custom_safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

CHAT_COLLECTION = "chat_messages"
STATE_COLLECTION = "room_states"  
ADMIN_PASSWORD = "0306"

class ChatRequest(BaseModel):
    room_name: str; user_name: str; text: str; avatar: str; last_idiom: Optional[str] = None; ignore_tone: bool = False

class ActionRequest(BaseModel):
    room_name: str; user_name: str; action_type: str; text: str; avatar: str; target_text: Optional[str] = None
    max_rounds: Optional[int] = 0  # ✨ 新增：用來接收回合制設定

class AdminRequest(BaseModel):
    room_name: str; admin_pwd: str; action_type: str; target_user: Optional[str] = None; user_name: Optional[str] = None

# ✨ 台灣讀音補丁
from pypinyin import load_single_dict
taiwan_pronunciation_patch = {
    ord('惜'): 'xí', ord('息'): 'xí', ord('媳'): 'xí', ord('擊'): 'jí', 
    ord('期'): 'qí', ord('框'): 'kuāng', ord('誰'): 'shéi',
}
load_single_dict(taiwan_pronunciation_patch)

def check_idiom_connection(last_idiom, new_idiom, ignore_tone=False):
    if not last_idiom or not new_idiom: return True
    style = Style.NORMAL if ignore_tone else Style.TONE3
    last_p = pinyin(last_idiom[-1], style=style, heteronym=True)[0]
    first_p = pinyin(new_idiom[0], style=style, heteronym=True)[0]
    return bool(set(last_p).intersection(set(first_p)))

# ==========================================
# 後端狀態引擎
# ==========================================
def get_default_state():
    return {
        "scores": {}, "playersOrder": [], "currentTurn": None,
        "lastIdiom": None, "pendingIdiom": None, "rejected": False,
        "sosUser": None, "sosCount": 0, "lastChatUser": None,
        "lastChatPrevSosUser": None, "lastChatPrevSosCount": 0,
        "lastChatWasPerfectMatch": False, "isGameOver": False,
        "alreadyJudged": False, "roundHints": {},
        "surrenderUser": None,
        "maxRounds": 0, "currentRound": 0, 
        "trackedPlayers": [], "playerRounds": {},
        "isVerifyingLastMove": False, "verificationVotes": [] # ✨ 新增：最終驗證模式相關變數
    }

def update_current_turn(state):
    if state.get("sosUser"):
        state["currentTurn"] = state["sosUser"]
        return
    players = state.get("playersOrder", [])
    if not players:
        state["currentTurn"] = None
        return
    last_user = state.get("lastChatUser")
    if state.get("rejected"):
        state["currentTurn"] = last_user
        return
    if last_user in players:
        idx = players.index(last_user)
        state["currentTurn"] = players[(idx + 1) % len(players)]
    else:
        state["currentTurn"] = players[0]  

def get_room_state(room_name):
    doc = db.collection(STATE_COLLECTION).document(room_name).get()
    return doc.to_dict() if doc.exists else get_default_state()

def save_room_state(room_name, state):
    state["updated_at"] = firestore.SERVER_TIMESTAMP
    db.collection(STATE_COLLECTION).document(room_name).set(state)

def delete_messages_safe(room_name, target_user=None):
    query = db.collection(CHAT_COLLECTION).where("room_name", "==", room_name)
    if target_user:
        query = query.where("user_name", "==", target_user)
    docs = list(query.stream())
    for i in range(0, len(docs), 400):
        batch = db.batch()
        for doc in docs[i:i+400]:
            batch.delete(doc.reference)
        batch.commit()

# ==========================================
# 遊戲 API 路由
# ==========================================

@app.post("/send_chat")
async def send_chat(req: ChatRequest):
    # ✨ 1. 準備 Transaction 與所需的 Document References
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)
    chat_ref = db.collection(CHAT_COLLECTION).document() # 預先生成一筆對話的 ID

    # ✨ 2. 定義 Transaction 內容
    @firestore.transactional
    def process_chat_transaction(transaction, room_doc_ref, chat_doc_ref):
        doc = room_doc_ref.get(transaction=transaction)
        state = doc.to_dict() if doc.exists else get_default_state()
        
        # --- 之前的驗證邏輯 ---
        current_turn = state.get("currentTurn")
        has_started = bool(state.get("lastIdiom") or state.get("pendingIdiom"))
        players = state.get("playersOrder", [])
        
        if has_started and len(players) > 1 and current_turn and current_turn != req.user_name:
            raise HTTPException(status_code=403, detail="現在不是你的回合喔！請不要作弊 😠")
        
        if len(req.text) > 15:
            raise HTTPException(status_code=400, detail="成語或輸入文字太長囉！")
        
        targetForBonus = state.get("lastIdiom") if state.get("rejected") else state.get("pendingIdiom")
        valid_target = targetForBonus if targetForBonus else req.last_idiom
        
        if not check_idiom_connection(valid_target, req.text, req.ignore_tone):
            raise HTTPException(status_code=400, detail="拼音或聲調不符！請重新輸入。")
            
        isPerfectMatch = False
        if targetForBonus and req.text and targetForBonus[-1] == req.text[0]:
            isPerfectMatch = True

        # --- 狀態更新邏輯 ---
        is_first_move = not state.get("lastIdiom") and not state.get("pendingIdiom")
        if is_first_move and not state.get("trackedPlayers"):
            state["trackedPlayers"] = state.get("playersOrder", []).copy()

        if not state.get("rejected"):
            state["lastIdiom"] = state.get("pendingIdiom")
        state["pendingIdiom"] = req.text
        state["rejected"] = False

        state["lastChatPrevSosUser"] = state.get("sosUser")
        state["lastChatPrevSosCount"] = state.get("sosCount")
        state["lastChatUser"] = req.user_name
        state["lastChatWasPerfectMatch"] = isPerfectMatch
        state["alreadyJudged"] = False
        state["roundHints"] = {}

        user = req.user_name
        user_finished_turn = False

        if state.get("sosUser") == user:
            state["sosCount"] = state.get("sosCount", 0) + 1
            if state["sosCount"] >= 3:
                state["scores"][user] = state["scores"].get(user, 50) + (15 if isPerfectMatch else 10)
                state["sosUser"] = None
                state["sosCount"] = 0
                user_finished_turn = True
        else:
            state["scores"][user] = state["scores"].get(user, 50) + (15 if isPerfectMatch else 10)
            state["sosUser"] = None
            state["sosCount"] = 0
            user_finished_turn = True

        # 回合推進與對話框倒數提醒
        system_msg = None
        if user_finished_turn and user in state.get("trackedPlayers", []):
            state["playerRounds"] = state.get("playerRounds", {})
            state["playerRounds"][user] = state["playerRounds"].get(user, 0) + 1
            
            tracked = state["trackedPlayers"]
            if tracked:
                old_round = state.get("currentRound", 0)
                min_round = min([state["playerRounds"].get(p, 0) for p in tracked])
                state["currentRound"] = min_round
                
                if state.get("maxRounds", 0) > 0:
                    if state["currentRound"] >= state["maxRounds"]:
                        state["isVerifyingLastMove"] = True
                        state["verificationVotes"] = []
                    elif min_round > old_round:
                        remaining = state["maxRounds"] - min_round
                        if 0 < remaining <= 3:
                            # 準備發送系統訊息
                            system_msg = f"【系統】🚨 比賽進入最後倒數，剩下 **{remaining}** 回合！"

        update_current_turn(state)
        state["updated_at"] = firestore.SERVER_TIMESTAMP

        # ✨ 3. 將資料寫入 (狀態、對話紀錄、系統訊息全部一起寫入)
        transaction.set(room_doc_ref, state)
        transaction.set(chat_doc_ref, {
            "room_name": req.room_name, "user_name": req.user_name, "text": req.text, 
            "type": "chat", "avatar": req.avatar, "timestamp": firestore.SERVER_TIMESTAMP
        })
        
        # 如果剛好跨回合且快結束了，一起寫入系統提示訊息
        if system_msg:
            sys_msg_ref = db.collection(CHAT_COLLECTION).document()
            transaction.set(sys_msg_ref, {
                "room_name": req.room_name, "user_name": "System", 
                "text": system_msg, "type": "system", "timestamp": firestore.SERVER_TIMESTAMP
            })

    # ✨ 4. 觸發執行
    process_chat_transaction(transaction_obj, room_ref, chat_ref)
    
    return {"status": "success"}

@app.post("/system_action")
async def system_action(req: ActionRequest):
    # ✨ 1. 準備 Transaction 與 Document References
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)
    chat_ref = db.collection(CHAT_COLLECTION).document()  # 預先生成聊天對話的 ID
    meta_ref = db.collection("system_meta").document("active_rooms")  # 大廳房間名單

    # ✨ 2. 定義 Transaction 內容
    @firestore.transactional
    def process_system_transaction(transaction, room_doc_ref, chat_doc_ref, meta_doc_ref):
        doc = room_doc_ref.get(transaction=transaction)
        state = doc.to_dict() if doc.exists else get_default_state()
        changed = False

        if req.action_type == "join_room":
            # 剛開房時設定最大回合數
            if not state.get("playersOrder"): 
                state["maxRounds"] = req.max_rounds

            if req.user_name not in state.get("playersOrder", []):
                state["playersOrder"].append(req.user_name)
                state["scores"][req.user_name] = 50
                changed = True
            
            # 將大廳的人員名單更新也包在交易裡執行
            transaction.set(meta_doc_ref, {
                req.room_name: { req.user_name: req.avatar }
            }, merge=True)

        elif req.action_type == "sos_start":
            state["sosUser"] = req.user_name
            state["sosCount"] = 0
            changed = True
            
        elif req.action_type == "game_over":
            state["isGameOver"] = True
            state["surrenderUser"] = req.user_name
            changed = True
            
        elif req.action_type == "final_vote_no":
            if state.get("isVerifyingLastMove"):
                votes = state.get("verificationVotes", [])
                if req.user_name not in votes:
                    votes.append(req.user_name)
                    state["verificationVotes"] = votes
                
                # 檢查是否所有房間內的人都投了「不用」
                active_players = state.get("playersOrder", [])
                if len(votes) >= len(active_players):
                    state["isVerifyingLastMove"] = False
                    state["isGameOver"] = True # 全票通過，正式結算！
            changed = True

        # 更新狀態並準備寫入
        if changed:
            update_current_turn(state)
            state["updated_at"] = firestore.SERVER_TIMESTAMP
            transaction.set(room_doc_ref, state)

        # 處理要顯示的對話框文字
        display_text = req.text
        if req.action_type == "game_over":
            display_text = f"【系統】{req.user_name} 投降了 🏳️"

        # 將系統對話紀錄一併寫入
        transaction.set(chat_doc_ref, {
            "room_name": req.room_name, "user_name": req.user_name, "text": display_text, 
            "type": req.action_type, "avatar": req.avatar, "timestamp": firestore.SERVER_TIMESTAMP
        })

    # ✨ 3. 觸發執行
    process_system_transaction(transaction_obj, room_ref, chat_ref, meta_ref)
    
    return {"status": "success"}

@app.post("/call_referee")
async def call_referee(req: ActionRequest):
    prompt = f"請判斷「{req.target_text}」以在台灣教育部最具權威的《成語典》或《重編國語辭典修訂本》判斷是否為正確的中文成語。請用繁體中文回答：『✅ 是成語』或『❌ 不是成語』，並簡述解釋。"
    res = model.generate_content(prompt, safety_settings=custom_safety_settings)
    result_text = res.text.strip()
    
    state = get_room_state(req.room_name)
    state["alreadyJudged"] = True
    
    if '❌' in result_text:
        state["rejected"] = True
        last_user = state.get("lastChatUser")
        if last_user and last_user in state.get("scores", {}):
            penalty = 40 if state.get("lastChatWasPerfectMatch") else 30
            
            # ✨ 整合 1：求生連擊判錯，前兩擊只小扣 5 分
            prev_sos_user = state.get("lastChatPrevSosUser")
            prev_sos_count = state.get("lastChatPrevSosCount", 0)
            
            if last_user == prev_sos_user and prev_sos_count < 2:
                penalty = 5  
                
            state["scores"][last_user] -= penalty
            if state["scores"][last_user] <= 0:
                state["isGameOver"] = True
                
        state["sosUser"] = state.get("lastChatPrevSosUser")
        state["sosCount"] = state.get("lastChatPrevSosCount")
        
        if last_user and last_user in state.get("trackedPlayers", []):
            state["playerRounds"] = state.get("playerRounds", {})
            state["playerRounds"][last_user] = max(0, state["playerRounds"].get(last_user, 0) - 1)
            tracked = state["trackedPlayers"]
            if tracked:
                state["currentRound"] = min([state["playerRounds"].get(p, 0) for p in tracked])
        
        if state.get("isVerifyingLastMove"):
            state["isVerifyingLastMove"] = False 
            
    elif '✅' in result_text:
        last_user = state.get("lastChatUser")
        if last_user and last_user in state.get("scores", {}):
            
            # ✨ 整合 2：求生連擊判對，前兩擊不給 5 分清白獎勵
            prev_sos_user = state.get("lastChatPrevSosUser")
            prev_sos_count = state.get("lastChatPrevSosCount", 0)
            
            # 只有在「不是」求生連擊前兩擊的情況下，才加 5 分
            if not (last_user == prev_sos_user and prev_sos_count < 2):
                state["scores"][last_user] += 5
            
        if state.get("isVerifyingLastMove"):
            state["isVerifyingLastMove"] = False
            state["isGameOver"] = True

    # 容錯機制：如果 AI 發神經，沒有給出明確的符號
    else:
        state["alreadyJudged"] = False 
        db.collection(CHAT_COLLECTION).add({
            "room_name": req.room_name, "user_name": "Referee (AI)", 
            "text": f"⚠️ 裁判陷入了深思，沒有給出明確的 ✅ 或 ❌，請再試一次！\n(AI 回覆原文：{result_text})", 
            "type": "referee", "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
        })
        save_room_state(req.room_name, state)
        return {"status": "success"}
            
    update_current_turn(state)
    save_room_state(req.room_name, state)

    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": "Referee (AI)", "text": result_text, 
        "type": "referee", "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}

@app.post("/buy_hint")
async def buy_hint(req: ActionRequest):
    state = get_room_state(req.room_name)
    user = req.user_name
    
    state["scores"][user] = state.get("scores", {}).get(user, 50) - 5
    if state["scores"][user] <= 0:
        state["isGameOver"] = True
    
    hints = state.get("roundHints", {})
    hints[user] = hints.get(user, 0) + 1
    state["roundHints"] = hints
    
    update_current_turn(state)
    save_room_state(req.room_name, state)

    if req.action_type == "hint_1":
        prompt = f"請給出一個以「{req.target_text[-1]}」開頭（或同音）的常見繁體中文四字成語。只需回傳該成語本身，不要標點。"
        res = model.generate_content(prompt, safety_settings=custom_safety_settings)
        ans = res.text.strip()[:4]
        hint_char = ans[2] if len(ans) >= 3 else ans[-1]
        db.collection(CHAT_COLLECTION).add({
            "room_name": req.room_name, "user_name": "Referee (AI)", 
            "text": f"💡 第一次提示：下一句的第三個字可以是「**{hint_char}**」", 
            "type": "referee", "hint_answer": ans, "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
        })
        return {"status": "success"}
    
    elif req.action_type == "hint_2":
        docs = db.collection(CHAT_COLLECTION).where("room_name", "==", req.room_name).where("requested_by", "==", req.user_name).where("type", "==", "referee").stream()
        hint_records = [d.to_dict() for d in docs if d.to_dict().get("hint_answer") and d.to_dict().get("timestamp")]
        
        target_idiom = None
        if hint_records:
            hint_records.sort(key=lambda x: x.get("timestamp"))
            target_idiom = hint_records[-1].get("hint_answer")
            
        if target_idiom:
            prompt = f"請解釋成語「{target_idiom}」的意思，但請注意：在解釋內容中絕對不能出現「{target_idiom}」這四個字中的任何一個字。請用繁體中文回答。"
            res = model.generate_content(prompt, safety_settings=custom_safety_settings)
            db.collection(CHAT_COLLECTION).add({
                "room_name": req.room_name, "user_name": "Referee (AI)", 
                "text": f"💡 第二次提示 (意思)：\n{res.text.strip()}", 
                "type": "referee", "hint_answer": target_idiom, "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
            })
            return {"status": "success"}
        else:
            raise HTTPException(status_code=400, detail="找不到前一次的提示紀錄！")

@app.post("/random_topic")
async def random_topic(req: ActionRequest):
    prompt = "請給出一個常見的繁體中文四字成語，只需回傳成語本身。"
    res = model.generate_content(prompt, safety_settings=custom_safety_settings)
    
    if res and res.text:
        idiom = res.text.strip()[:4]
        state = get_room_state(req.room_name)
        
        # ✨ 回合制：AI出題視同開局，若名單尚未鎖定，立刻鎖定玩家
        if not state.get("trackedPlayers"):
            state["trackedPlayers"] = state.get("playersOrder", []).copy()
            
        state["lastIdiom"] = idiom
        state["pendingIdiom"] = idiom
        state["lastChatUser"] = req.user_name  
        state["rejected"] = False
        state["sosUser"] = None
        state["sosCount"] = 0
        state["roundHints"] = {}
        update_current_turn(state)
        save_room_state(req.room_name, state)

        db.collection(CHAT_COLLECTION).add({
            "room_name": req.room_name, "user_name": "System", 
            "text": f"【系統】遊戲開始！題目為「**{idiom}**」", 
            "type": "system", "timestamp": firestore.SERVER_TIMESTAMP
        })
        return {"status": "success"}
    else:
        raise HTTPException(status_code=500, detail="AI 出題失敗，請重試！")

@app.post("/restart_game")
async def restart_game(req: ActionRequest):
    state = get_room_state(req.room_name)
    players = state.get("playersOrder", [])
    
    new_state = get_default_state()
    new_state["playersOrder"] = players
    new_state["maxRounds"] = req.max_rounds # ✨ 寫入新的回合設定
    
    for p in players:
        new_state["scores"][p] = 50
    
    new_state["surrenderUser"] = None
    
    update_current_turn(new_state)
    save_room_state(req.room_name, new_state)

    docs = list(db.collection(CHAT_COLLECTION).where("room_name", "==", req.room_name).stream())
    seen_users = set()
    to_delete = []
    
    for doc in docs:
        data = doc.to_dict()
        if data.get("type") == "join_room" and data.get("user_name") not in seen_users:
            seen_users.add(data.get("user_name"))
        else:
            to_delete.append(doc.reference)
            
    for i in range(0, len(to_delete), 400):
        batch = db.batch()
        for ref in to_delete[i:i+400]:
            batch.delete(ref)
        batch.commit()
        
    return {"status": "success"}
    
@app.post("/admin_action")
async def admin_action(req: AdminRequest):
    is_self_delete = (req.action_type == "delete_user" and req.target_user == req.user_name)
    if req.admin_pwd != ADMIN_PASSWORD and not is_self_delete:
        raise HTTPException(status_code=403, detail="權限不足！")
    
    # ✨ 1. 準備 Transaction 與 Document References
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)
    meta_ref = db.collection("system_meta").document("active_rooms")
    sys_msg_ref = db.collection(CHAT_COLLECTION).document() # 預先生成系統訊息 ID
    
    if req.action_type == "delete_user" and req.target_user:
        target = req.target_user

        # ✨ 2. 定義刪除玩家的 Transaction
        @firestore.transactional
        def process_delete_user_transaction(transaction, room_doc_ref, meta_doc_ref, sys_doc_ref):
            doc = room_doc_ref.get(transaction=transaction)
            state = doc.to_dict() if doc.exists else get_default_state()
            
            meta_doc = meta_doc_ref.get(transaction=transaction)
            meta_data = meta_doc.to_dict() if meta_doc.exists else {}

            # 同步大廳名單
            if req.room_name in meta_data and target in meta_data[req.room_name]:
                del meta_data[req.room_name][target]
                if not meta_data[req.room_name]: 
                    del meta_data[req.room_name]
                transaction.set(meta_doc_ref, meta_data)

            # 修改遊戲狀態
            if target in state.get("playersOrder", []):
                state["playersOrder"].remove(target)
            if target in state.get("scores", {}):
                del state["scores"][target]
                
            if state.get("sosUser") == target:
                state["sosUser"] = None
                state["sosCount"] = 0

            # 回合制計算更新
            if target in state.get("trackedPlayers", []):
                state["trackedPlayers"].remove(target)
                if target in state.get("playerRounds", {}):
                    del state["playerRounds"][target]
                tracked = state["trackedPlayers"]
                if tracked:
                    state["currentRound"] = min([state["playerRounds"].get(p, 0) for p in tracked])
                    if state.get("maxRounds", 0) > 0 and state["currentRound"] >= state["maxRounds"]:
                        state["isGameOver"] = True

            # 判斷這是不是房間裡的最後一個人？
            is_empty_room = len(state.get("playersOrder", [])) == 0

            if not is_empty_room:
                update_current_turn(state)
                state["updated_at"] = firestore.SERVER_TIMESTAMP
                transaction.set(room_doc_ref, state)
                transaction.set(sys_doc_ref, {
                    "room_name": req.room_name, "user_name": "System", 
                    "text": f"({target}) 離開了遊戲", 
                    "type": "system", "timestamp": firestore.SERVER_TIMESTAMP
                })
                
            return is_empty_room # 將結果傳到交易外部

        # ✨ 3. 執行交易
        is_empty = process_delete_user_transaction(transaction_obj, room_ref, meta_ref, sys_msg_ref)
        
        # ✨ 4. 交易結束後，如果判斷房間已空，在交易外執行大量刪除動作
        if is_empty:
            delete_messages_safe(req.room_name)
            room_ref.delete()
            
    elif req.action_type == "clear_room":
        # 毀滅式清空房間 (不需要防搶拍，直接執行即可)
        delete_messages_safe(req.room_name)
        
        # 確保大廳名單安全刪除
        @firestore.transactional
        def process_clear_meta(transaction, meta_doc_ref):
            meta_doc = meta_doc_ref.get(transaction=transaction)
            if meta_doc.exists:
                meta_data = meta_doc.to_dict()
                if req.room_name in meta_data:
                    del meta_data[req.room_name]
                    transaction.set(meta_doc_ref, meta_data)
                    
        process_clear_meta(transaction_obj, meta_ref)
        room_ref.delete()
        
    return {"status": "success"}
    
@app.get("/get_rooms")
async def get_rooms():
    doc = db.collection("system_meta").document("active_rooms").get()
    return {"status": "success", "data": doc.to_dict() if doc.exists else {}}

@app.post("/revoke_chat")
async def revoke_chat(req: ActionRequest):
    # ✨ 1. (在交易外) 先找出玩家最新的一筆發言紀錄，取得準備要刪除的 Document Reference
    docs = list(db.collection(CHAT_COLLECTION)\
        .where("room_name", "==", req.room_name)\
        .where("user_name", "==", req.user_name)\
        .where("type", "==", "chat")\
        .stream())
        
    valid_docs = [d for d in docs if d.to_dict().get("timestamp") is not None]
    doc_to_delete_ref = None
    if valid_docs:
        valid_docs.sort(key=lambda d: d.to_dict().get("timestamp"), reverse=True)
        doc_to_delete_ref = db.collection(CHAT_COLLECTION).document(valid_docs[0].id)

    # ✨ 2. 準備 Transaction 與其他的 Document References
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)
    sys_msg_ref = db.collection(CHAT_COLLECTION).document() # 預先生成系統訊息 ID

    # ✨ 3. 定義 Transaction 內容
    @firestore.transactional
    def process_revoke_transaction(transaction, room_doc_ref, sys_doc_ref, del_doc_ref):
        doc = room_doc_ref.get(transaction=transaction)
        state = doc.to_dict() if doc.exists else get_default_state()

        # 防護 1：確認現在最新發言的還是不是這個人（防剛好有人接下去的極限時間差）
        if state.get("lastChatUser") != req.user_name:
            raise HTTPException(status_code=400, detail="只能收回自己最新的發言！或者已經有人接下去了！")
        
        # 防護 2：確認裁判還沒判決
        if state.get("alreadyJudged"):
            raise HTTPException(status_code=400, detail="裁判已經判決，無法收回！")

        is_perfect_match = state.get("lastChatWasPerfectMatch", False)
        penalty = 25 if is_perfect_match else 15

        # 扣分處理
        state["scores"][req.user_name] = state["scores"].get(req.user_name, 50) - penalty
        if state["scores"][req.user_name] <= 0:
            state["isGameOver"] = True

        # 回合制：收回發言，個人計數器 -1
        user = req.user_name
        if user in state.get("trackedPlayers", []):
            state["playerRounds"] = state.get("playerRounds", {})
            state["playerRounds"][user] = max(0, state["playerRounds"].get(user, 0) - 1)
            tracked = state["trackedPlayers"]
            if tracked:
                state["currentRound"] = min([state["playerRounds"].get(p, 0) for p in tracked])

        state["rejected"] = True
        state["sosUser"] = state.get("lastChatPrevSosUser")
        state["sosCount"] = state.get("lastChatPrevSosCount")

        update_current_turn(state)
        state["updated_at"] = firestore.SERVER_TIMESTAMP

        # ✨ 4. 將狀態寫入、加入系統提示、並刪除那筆發言
        transaction.set(room_doc_ref, state)
        transaction.set(sys_doc_ref, {
            "room_name": req.room_name, "user_name": "System", 
            "text": f"【系統】**{req.user_name}** 覺得不妥，收回了發言！扣除 {penalty} 分 (原地 -5 分)", 
            "type": "system", "timestamp": firestore.SERVER_TIMESTAMP
        })
        
        # 如果有找到那筆要刪除的紀錄，就在交易裡把它刪掉
        if del_doc_ref:
            transaction.delete(del_doc_ref)

    # ✨ 5. 觸發執行
    process_revoke_transaction(transaction_obj, room_ref, sys_msg_ref, doc_to_delete_ref)

    return {"status": "success"}