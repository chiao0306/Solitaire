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
        "isVerifyingLastMove": False, "verificationVotes": [],
        "isRefereeProcessing": False, "refereeCaller": None,
        "isHintProcessing": False, "hintCaller": None,
        "isRandomProcessing": False, "randomCaller": None # ✨ 新增隨機出題狀態預設值
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
        
        # ✨ 新增防護規則：不能跟上一句一模一樣
        if valid_target and req.text == valid_target:
            raise HTTPException(status_code=400, detail="不能輸入跟上一句一模一樣的成語！請重新輸入。")
        
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
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)
    chat_ref = db.collection(CHAT_COLLECTION).document()  
    meta_ref = db.collection("system_meta").document("active_rooms")  

    @firestore.transactional
    def process_system_transaction(transaction, room_doc_ref, chat_doc_ref, meta_doc_ref):
        # 💡 先讀取大廳名單，準備檢查房間數
        meta_doc = meta_doc_ref.get(transaction=transaction)
        meta_data = meta_doc.to_dict() if meta_doc.exists else {}

        doc = room_doc_ref.get(transaction=transaction)
        state = doc.to_dict() if doc.exists else get_default_state()
        changed = False

        if req.action_type == "join_room":
            # ✨ 後端防護 1：檢查房間數量
            if req.room_name not in meta_data and len(meta_data.keys()) >= 10:
                return {"error": "系統目前已達 10 個房間的上限，請加入現有房間或稍後再試！"}

            if req.user_name not in state.get("playersOrder", []):
                # ✨ 後端防護 2：檢查房間內人數
                if len(state.get("playersOrder", [])) >= 10:
                    return {"error": "此房間已滿 10 人，無法加入！"}

                # 如果都通過了，才開始初始化與加人
                if not state.get("playersOrder"): 
                    state["maxRounds"] = req.max_rounds

                state["playersOrder"].append(req.user_name)
                state["scores"][req.user_name] = 50
                changed = True
            
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
                
                active_players = state.get("playersOrder", [])
                if len(votes) >= len(active_players):
                    state["isVerifyingLastMove"] = False
                    state["isGameOver"] = True
            changed = True

        if changed:
            update_current_turn(state)
            state["updated_at"] = firestore.SERVER_TIMESTAMP
            transaction.set(room_doc_ref, state)

        display_text = req.text
        if req.action_type == "game_over":
            display_text = f"【系統】{req.user_name} 投降了 🏳️"

        transaction.set(chat_doc_ref, {
            "room_name": req.room_name, "user_name": req.user_name, "text": display_text, 
            "type": req.action_type, "avatar": req.avatar, "timestamp": firestore.SERVER_TIMESTAMP
        })
        return {"success": True}

    # 💡 執行交易並檢查有沒有錯誤回傳
    result = process_system_transaction(transaction_obj, room_ref, chat_ref, meta_ref)
    if result and "error" in result:
        # 如果有錯誤，丟回給前端顯示 (剛好會被我們前面的 apiCall 攔截並秀出 showModalAlert)
        raise HTTPException(status_code=400, detail=result["error"])
    
    return {"status": "success"}

@app.post("/call_referee")
async def call_referee(req: ActionRequest):
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)

    @firestore.transactional
    def lock_referee_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        curr_state = snapshot.to_dict() if snapshot.exists else get_default_state()
        
        # ✨ 檢查是否有人正在使用任何技能
        if curr_state.get("isRefereeProcessing") or curr_state.get("isHintProcessing") or curr_state.get("alreadyJudged"):
            return {"error": "系統忙碌中或已經判決過囉！"}
        
        # ✨ 鎖定並紀錄是「誰」按的
        transaction.update(doc_ref, {"isRefereeProcessing": True, "refereeCaller": req.user_name})
        return {"success": True, "state": curr_state}

    lock_res = lock_referee_transaction(transaction_obj, room_ref)
    if isinstance(lock_res, dict) and "error" in lock_res:
        raise HTTPException(status_code=403, detail=lock_res["error"])
    
    try:
        prompt = f"請判斷「{req.target_text}」是否為正確的中文成語。請用繁體中文回答：『✅ 是成語』或『❌ 不是成語』，並簡述解釋。"
        res = model.generate_content(prompt, safety_settings=custom_safety_settings)
        result_text = res.text.strip() if res and res.text else "⚠️ 裁判暫時無法回應"
        
        state = lock_res["state"]
        state["alreadyJudged"] = True
        
        if '❌' in result_text:
            state["rejected"] = True
            last_user = state.get("lastChatUser")
            if last_user and last_user in state.get("scores", {}):
                penalty = 40 if state.get("lastChatWasPerfectMatch") else 30
                prev_sos_user = state.get("lastChatPrevSosUser")
                prev_sos_count = state.get("lastChatPrevSosCount", 0)
                if last_user == prev_sos_user and prev_sos_count < 2:
                    penalty = 5  
                state["scores"][last_user] -= penalty
                if state["scores"][last_user] <= 0:
                    state["isGameOver"] = True
            state["sosUser"] = state.get("lastChatPrevSosUser")
            state["sosCount"] = state.get("lastChatPrevSosCount")
        elif '✅' in result_text:
            last_user = state.get("lastChatUser")
            if last_user and last_user in state.get("scores", {}):
                prev_sos_user = state.get("lastChatPrevSosUser")
                prev_sos_count = state.get("lastChatPrevSosCount", 0)
                if not (last_user == prev_sos_user and prev_sos_count < 2):
                    state["scores"][last_user] += 5
        
        # ✨ 判定完成，解除鎖定
        state["isRefereeProcessing"] = False
        state["refereeCaller"] = None
        
        update_current_turn(state)
        save_room_state(req.room_name, state)
        db.collection(CHAT_COLLECTION).add({
            "room_name": req.room_name, "user_name": "Referee (AI)", "text": result_text, 
            "type": "referee", "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
        })
        
    except Exception as e:
        db.collection(STATE_COLLECTION).document(req.room_name).update({"isRefereeProcessing": False, "refereeCaller": None})
        raise HTTPException(status_code=500, detail=f"AI 裁判出錯：{str(e)}")

    return {"status": "success"}

@app.post("/buy_hint")
async def buy_hint(req: ActionRequest):
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)

    # ✨ 將提示也包裝進交易鎖機制，防止同時按鈕的 Race Condition
    @firestore.transactional
    def lock_hint_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        curr_state = snapshot.to_dict() if snapshot.exists else get_default_state()

        if curr_state.get("isHintProcessing") or curr_state.get("isRefereeProcessing"):
            return {"error": "系統忙碌中，請稍候！"}

        transaction.update(doc_ref, {"isHintProcessing": True, "hintCaller": req.user_name})
        return {"success": True, "state": curr_state}

    lock_res = lock_hint_transaction(transaction_obj, room_ref)
    if isinstance(lock_res, dict) and "error" in lock_res:
        raise HTTPException(status_code=403, detail=lock_res["error"])

    try:
        # AI 生成邏輯保持不變 (已經是注音升級版)
        if req.action_type == "hint_1":
            target_char = req.target_text[-1]
            try: char_bopomofo = pinyin(target_char, style=Style.BOPOMOFO)[0][0]
            except: char_bopomofo = ""

            prompt = (
                f"你現在是嚴格的成語接龍小幫手。玩家上一個詞的結尾字是「{target_char}」"
                f"{f'（注音：{char_bopomofo}）' if char_bopomofo else ''}。\n"
                f"請提供「一個」合法的繁體中文四字成語，該成語的「第一個字」必須與「{target_char}」讀音完全相同（含聲調）或同字。\n"
                f"只需回傳該成語本身（四個字），絕對不要輸出任何標點符號或額外解釋。"
            )
            res = model.generate_content(prompt, safety_settings=custom_safety_settings)
            ans = res.text.strip()[:4]
            hint_char = ans[1] if len(ans) >= 2 else ans[-1]
            
            db.collection(CHAT_COLLECTION).add({
                "room_name": req.room_name, "user_name": "Referee (AI)", 
                "text": f"💡 第一次提示：下一句的第二個字可以是「**{hint_char}**」", 
                "type": "referee", "hint_answer": ans, "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
            })
            
        elif req.action_type == "hint_2":
            docs = list(db.collection(CHAT_COLLECTION)\
                .where("room_name", "==", req.room_name)\
                .where("requested_by", "==", req.user_name)\
                .where("type", "==", "referee")\
                .order_by("timestamp", direction=firestore.Query.DESCENDING)\
                .limit(1).stream())
            
            target_idiom = docs[0].to_dict().get("hint_answer") if docs else None
            if target_idiom:
                prompt = f"請解釋成語「{target_idiom}」的意思，但請注意：在解釋內容中絕對不能出現「{target_idiom}」這四個字中的任何一個字。請用繁體中文回答。"
                res = model.generate_content(prompt, safety_settings=custom_safety_settings)
                db.collection(CHAT_COLLECTION).add({
                    "room_name": req.room_name, "user_name": "Referee (AI)", 
                    "text": f"💡 第二次提示 (意思)：\n{res.text.strip()}", 
                    "type": "referee", "hint_answer": target_idiom, "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
                })
            else:
                raise HTTPException(status_code=400, detail="找不到前一次的提示紀錄！")

        # ✨ 為了安全，重新抓取最新的狀態再扣分，防止這 2 秒內有人講話被覆蓋
        fresh_snap = room_ref.get()
        state = fresh_snap.to_dict() if fresh_snap.exists else lock_res["state"]
        user = req.user_name

        state["scores"][user] = state.get("scores", {}).get(user, 50) - 5
        if state["scores"][user] <= 0: state["isGameOver"] = True
            
        state["roundHints"] = state.get("roundHints", {})
        state["roundHints"][user] = state["roundHints"].get(user, 0) + 1
        
        # ✨ 提示完成，解除鎖定
        state["isHintProcessing"] = False
        state["hintCaller"] = None
        
        update_current_turn(state)
        save_room_state(req.room_name, state)

    except Exception as e:
        room_ref.update({"isHintProcessing": False, "hintCaller": None})
        raise HTTPException(status_code=500, detail=f"AI 提示出錯：{str(e)}")

    return {"status": "success"}

@app.post("/random_topic")
async def random_topic(req: ActionRequest):
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)

    # ✨ 使用交易鎖：確保同一時間只有一個人能成功請求 AI 出題，並設定全局鎖定狀態
    @firestore.transactional
    def lock_random_transaction(transaction, doc_ref):
        snapshot = doc_ref.get(transaction=transaction)
        curr_state = snapshot.to_dict() if snapshot.exists else get_default_state()
        
        # 如果系統已經在處理任何 AI 動作（裁判、提示、出題），就擋下
        if curr_state.get("isRandomProcessing") or curr_state.get("isRefereeProcessing") or curr_state.get("isHintProcessing"):
            return {"error": "系統忙碌中，請稍候！"}
        
        # 搶鎖成功，寫入出題中標記與是誰按的
        transaction.update(doc_ref, {"isRandomProcessing": True, "randomCaller": req.user_name})
        return {"success": True, "state": curr_state}

    lock_res = lock_random_transaction(transaction_obj, room_ref)
    if isinstance(lock_res, dict) and "error" in lock_res:
        raise HTTPException(status_code=403, detail=lock_res["error"])
    
    try:
        # 開始讓 Gemini 生成隨機成語
        prompt = "請給出一個隨機的繁體中文四字成語，只需回傳成語本身。"
        res = model.generate_content(prompt, safety_settings=custom_safety_settings)
        
        if res and res.text:
            idiom = res.text.strip()[:4]
            
            # 重新獲取最新的狀態，避免在 AI 生成的這兩秒內房間人員發生異動
            fresh_snap = room_ref.get()
            state = fresh_snap.to_dict() if fresh_snap.exists else lock_res["state"]
            
            # 回合制名單鎖定
            if not state.get("trackedPlayers"):
                state["trackedPlayers"] = state.get("playersOrder", []).copy()
                
            state["lastIdiom"] = idiom
            state["pendingIdiom"] = idiom
            state["lastChatUser"] = req.user_name  
            state["rejected"] = False
            state["sosUser"] = None
            state["sosCount"] = 0
            state["roundHints"] = {}
            
            # ✨ 出題完成，解除全局出題鎖定狀態
            state["isRandomProcessing"] = False
            state["randomCaller"] = None
            
            update_current_turn(state)
            save_room_state(req.room_name, state)

            db.collection(CHAT_COLLECTION).add({
                "room_name": req.room_name, "user_name": "System", 
                "text": f"【系統】遊戲開始！題目為「**{idiom}**」", 
                "type": "system", "timestamp": firestore.SERVER_TIMESTAMP
            })
        else:
            raise Exception("AI 回傳的題目內容為空")
            
    except Exception as e:
        # 發生任何非預期錯誤，務必將鎖解除，以免房間卡死
        db.collection(STATE_COLLECTION).document(req.room_name).update({"isRandomProcessing": False, "randomCaller": None})
        raise HTTPException(status_code=500, detail=f"AI 出題失敗：{str(e)}")

    return {"status": "success"}

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
    
    # 準備 Transaction 與 Document References
    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)
    meta_ref = db.collection("system_meta").document("active_rooms")
    sys_msg_ref = db.collection(CHAT_COLLECTION).document() 
    
    if req.action_type == "delete_user" and req.target_user:
        target = req.target_user

        # 定義刪除玩家的 Transaction
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

        # 執行交易
        is_empty = process_delete_user_transaction(transaction_obj, room_ref, meta_ref, sys_msg_ref)
        
        # ✨ 交易結束後，處理歷史訊息的清理
        if is_empty:
            # 如果房間空了，直接整間刪掉
            delete_messages_safe(req.room_name)
            room_ref.delete()
        else:
            # ✨ 新增防護機制：如果房間沒空，去找這個人的「加入房間」訊息並刪除
            # (因為這裡只有 where 篩選，沒有 order_by 排序，所以不會觸發索引 Bug，非常安全！)
            try:
                join_docs = db.collection(CHAT_COLLECTION)\
                    .where("room_name", "==", req.room_name)\
                    .where("user_name", "==", target)\
                    .where("type", "==", "join_room")\
                    .stream()
                for d in join_docs:
                    d.reference.delete()
            except Exception as e:
                print(f"清理加入訊息時發生錯誤: {e}")
            
    elif req.action_type == "clear_room":
        # 毀滅式清空房間
        delete_messages_safe(req.room_name)
        
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
    # ✨ 1. 直接從前端接收對話 ID (target_text)，不需再做複雜的資料庫查詢，完美避開索引錯誤！
    if not req.target_text:
        raise HTTPException(status_code=400, detail="找不到要收回的發言 ID！")
        
    doc_to_delete_ref = db.collection(CHAT_COLLECTION).document(req.target_text)

    transaction_obj = db.transaction()
    room_ref = db.collection(STATE_COLLECTION).document(req.room_name)
    sys_msg_ref = db.collection(CHAT_COLLECTION).document()

    @firestore.transactional
    def process_revoke_transaction(transaction, room_doc_ref, sys_doc_ref, del_doc_ref):
        doc_snap = room_doc_ref.get(transaction=transaction)
        state = doc_snap.to_dict() if doc_snap.exists else get_default_state()

        # 防護：確認狀態是否允許收回
        if state.get("lastChatUser") != req.user_name:
            return {"error": "只能收回自己最新的發言！或者已經有人接下去了！"}
        
        if state.get("alreadyJudged"):
            return {"error": "裁判已經判決，無法收回！"}

        # 計算扣分
        is_perfect_match = state.get("lastChatWasPerfectMatch", False)
        penalty = 25 if is_perfect_match else 15

        # 更新分數
        current_score = state.get("scores", {}).get(req.user_name, 50)
        state["scores"][req.user_name] = current_score - penalty
        if state["scores"][req.user_name] <= 0:
            state["isGameOver"] = True

        # 回合補正
        user = req.user_name
        if user in state.get("trackedPlayers", []):
            state["playerRounds"] = state.get("playerRounds", {})
            state["playerRounds"][user] = max(0, state["playerRounds"].get(user, 0) - 1)
            tracked = state["trackedPlayers"]
            if tracked:
                state["currentRound"] = min([state["playerRounds"].get(p, 0) for p in tracked])

        # 狀態退回
        state["rejected"] = True
        state["sosUser"] = state.get("lastChatPrevSosUser")
        state["sosCount"] = state.get("lastChatPrevSosCount")

        update_current_turn(state)
        state["updated_at"] = firestore.SERVER_TIMESTAMP

        # 寫入狀態與系統訊息
        transaction.set(room_doc_ref, state)
        transaction.set(sys_doc_ref, {
            "room_name": req.room_name, "user_name": "System", 
            "text": f"【系統】**{req.user_name}** 收回了發言！扣除 {penalty} 分", 
            "type": "system", "timestamp": firestore.SERVER_TIMESTAMP
        })
        
        # 安全刪除對話
        if del_doc_ref:
            transaction.delete(del_doc_ref)
        
        return {"success": True}

    # ✨ 執行交易
    result = process_revoke_transaction(transaction_obj, room_ref, sys_msg_ref, doc_to_delete_ref)
    
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return {"status": "success"}