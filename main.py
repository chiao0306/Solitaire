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
ADMIN_PASSWORD = "0306" # 👈 你的管理員密碼

class ChatRequest(BaseModel):
    room_name: str; user_name: str; text: str; avatar: str; last_idiom: Optional[str] = None; ignore_tone: bool = False
class ActionRequest(BaseModel):
    room_name: str; user_name: str; action_type: str; text: str; avatar: str; target_text: Optional[str] = None
class AdminRequest(BaseModel):
    room_name: str; admin_pwd: str; action_type: str; target_user: Optional[str] = None

# 💡 新增：台灣讀音補丁 (修正兩岸讀音差異)
from pypinyin import load_single_dict

# 這裡列出常見兩岸發音不同的字，統一改為台灣標準 (二聲 xí 等)
taiwan_pronunciation_patch = {
    ord('惜'): 'xí',
    ord('息'): 'xí',
    ord('媳'): 'xí',
    ord('擊'): 'jí', # 台灣唸ㄐㄧˊ，對岸唸ㄐㄧ
    ord('期'): 'qí', # 台灣唸ㄑㄧˊ，對岸唸ㄑㄧ
    ord('框'): 'kuāng',
    ord('誰'): 'shéi',
    # 你之後若發現還有哪個字被擋，就在這裡補上： ord('字'): '拼音'
}
load_single_dict(taiwan_pronunciation_patch)

def check_idiom_connection(last_idiom, new_idiom, ignore_tone=False):
    if not last_idiom or not new_idiom: return True
    style = Style.NORMAL if ignore_tone else Style.TONE3
    last_p = pinyin(last_idiom[-1], style=style, heteronym=True)[0]
    first_p = pinyin(new_idiom[0], style=style, heteronym=True)[0]
    return bool(set(last_p).intersection(set(first_p)))

@app.post("/send_chat")
async def send_chat(req: ChatRequest):
    if not check_idiom_connection(req.last_idiom, req.text, req.ignore_tone):
        raise HTTPException(status_code=400, detail="拼音或聲調不符！請重新輸入。")
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": req.user_name, "text": req.text, 
        "type": "chat", "avatar": req.avatar, "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}

# 引入 firestore 的 ArrayUnion 來更新陣列
from google.cloud.firestore_v1 import ArrayUnion

# 💡 確保這行在檔案最上方
from google.cloud.firestore import DELETE_FIELD

@app.post("/system_action")
async def system_action(req: ActionRequest):
    # 1. 儲存對話紀錄
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": req.user_name, "text": req.text, 
        "type": req.action_type, "avatar": req.avatar, "timestamp": firestore.SERVER_TIMESTAMP
    })
    
    # 💡 修改：將 ArrayUnion 改為 Map 結構，紀錄「玩家名: 頭像」
    if req.action_type == "join_room":
        doc_ref = db.collection("system_meta").document("active_rooms")
        doc_ref.set({
            req.room_name: { req.user_name: req.avatar }
        }, merge=True)
        
    return {"status": "success"}
    
# ====== 修改：重新開始 (每個玩家只保留一筆加入房間的紀錄) ======
@app.post("/restart_game")
async def restart_game(req: ActionRequest):
    # 抓出房間內所有對話
    docs = db.collection(CHAT_COLLECTION).where("room_name", "==", req.room_name).stream()
    batch = db.batch()
    seen_users = set()

    for doc in docs:
        data = doc.to_dict()
        m_type = data.get("type")
        user_name = data.get("user_name")

        # 💡 判斷邏輯：如果是 join_room 訊息，且這個人還沒被保留過，我們就放過它
        if m_type == "join_room" and user_name not in seen_users:
            seen_users.add(user_name)
            # 不執行刪除，保留這筆當作角色的「存在證明」
        else:
            # 其他的所有對話、多餘的加入通知，全部無情刪除
            batch.delete(doc.reference)

    batch.commit()
    return {"status": "success"}

@app.post("/call_referee")
async def call_referee(req: ActionRequest):
    prompt = f"請判斷「{req.target_text}」以在台灣教育部最具權威的《成語典》或《重編國語辭典修訂本》判斷是否為正確的中文成語。請用繁體中文回答：『✅ 是成語』或『❌ 不是成語』，並簡述解釋。"
    # 👇 這裡加上 safety_settings
    res = model.generate_content(prompt, safety_settings=custom_safety_settings)
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": "Referee (AI)", "text": res.text.strip(), 
        "type": "referee", "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}

@app.post("/buy_hint")
async def buy_hint(req: ActionRequest):
    # 💡 第一階段提示：給出第三個字
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
    
    # 💡 第二階段提示：解釋成語意思
    elif req.action_type == "hint_2":
        # 💡 避開 Firebase 討厭的索引限制：我們只做簡單篩選，把資料抓出來後用 Python 自己找最後一個
        docs = db.collection(CHAT_COLLECTION)\
                 .where("room_name", "==", req.room_name)\
                 .where("requested_by", "==", req.user_name)\
                 .where("type", "==", "referee")\
                 .stream()
        
        # 把有提示解答的紀錄挑出來
        hint_records = []
        for doc in docs:
            data = doc.to_dict()
            if data.get("hint_answer") and data.get("timestamp"):
                hint_records.append(data)
                
        target_idiom = None
        if hint_records:
            # 用 Python 自己根據時間排序，抓最後一筆 (也就是最新買的那個)
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
        db.collection(CHAT_COLLECTION).add({
            "room_name": req.room_name, 
            "user_name": "System", 
            "text": f"【系統】遊戲開始！題目為「**{idiom}**」", 
            "type": "system", 
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        return {"status": "success"}
    else:
        raise HTTPException(status_code=500, detail="AI 出題失敗，請重試！")

# 💡 記得在檔案最上方或是這個 function 裡面引入 ArrayRemove
from google.cloud.firestore_v1 import ArrayRemove, DELETE_FIELD

@app.post("/admin_action")
async def admin_action(req: AdminRequest):
    if req.admin_pwd != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="密碼錯誤！")
    
    batch = db.batch()
    doc_ref_meta = db.collection("system_meta").document("active_rooms")
    
    if req.action_type == "delete_user" and req.target_user:
        # 刪除對話
        docs = db.collection(CHAT_COLLECTION).where("room_name", "==", req.room_name).where("user_name", "==", req.target_user).stream()
        for doc in docs: batch.delete(doc.reference)
        # 💡 修改：從 Map 中移除特定玩家
        batch.update(doc_ref_meta, { f"{req.room_name}.{req.target_user}": DELETE_FIELD })
        
    elif req.action_type == "clear_room":
        docs = db.collection(CHAT_COLLECTION).where("room_name", "==", req.room_name).stream()
        for doc in docs: batch.delete(doc.reference)
        # 徹底移除房間
        batch.update(doc_ref_meta, { req.room_name: DELETE_FIELD })
        
    batch.commit()
    return {"status": "success"}
    
# ====== 把這段加在 main.py 的最下面 ======

@app.get("/get_rooms")
async def get_rooms():
    # 💡 現代科技魔法：直接讀取這張簽到表。花費讀取次數：永遠 1 次！
    doc_ref = db.collection("system_meta").document("active_rooms")
    doc = doc_ref.get()
    
    if doc.exists:
        # 回傳長這樣：{"房間A": ["玩家1", "玩家2"], "房間B": ["玩家3"]}
        return {"status": "success", "data": doc.to_dict()}
    
    return {"status": "success", "data": {}}
