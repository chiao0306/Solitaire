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

@app.post("/system_action")
async def system_action(req: ActionRequest):
    # 儲存動作訊息 (包含 join_room, sos_start, 以及我們要新增的 game_over)
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": req.user_name, "text": req.text, 
        "type": req.action_type, "avatar": req.avatar, "timestamp": firestore.SERVER_TIMESTAMP
    })
    
    if req.action_type == "join_room":
        doc_ref = db.collection("system_meta").document("active_rooms")
        doc_ref.set({ req.room_name: ArrayUnion([req.user_name]) }, merge=True)
        
    return {"status": "success"}
    
    # 💡 現代科技魔法：如果動作是「加入房間」，就去更新中控簽到表！
    if req.action_type == "join_room":
        # 我們建一個新的集合叫 system_meta，裡面放一張 active_rooms 文件
        doc_ref = db.collection("system_meta").document("active_rooms")
        # 使用 set(merge=True) 加上 ArrayUnion，確保房間存在，且名字不重複寫入
        doc_ref.set({
            req.room_name: ArrayUnion([req.user_name])
        }, merge=True)
        
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
    prompt = f"請給出一個以「{req.target_text[-1]}」開頭（或同音）的常見繁體中文四字成語。只需回傳該成語本身，不要標點。"
    # 👇 這裡加上 safety_settings
    res = model.generate_content(prompt, safety_settings=custom_safety_settings)
    ans = res.text.strip()[:4]
    hint_char = ans[2] if len(ans) >= 3 else ans[-1]
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": "Referee (AI)", 
        "text": f"💡 第一次提示：下一句的第三個字可以是「**{hint_char}**」", 
        "type": "referee", "hint_answer": ans, "requested_by": req.user_name, "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}

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

# 💡 新增：管理員專用路由
@app.post("/admin_action")
async def admin_action(req: AdminRequest):
    if req.admin_pwd != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="密碼錯誤！拒絕存取。")
    
    query = db.collection(CHAT_COLLECTION).where("room_name", "==", req.room_name)
    if req.action_type == "delete_user" and req.target_user:
        query = query.where("user_name", "==", req.target_user)
        
    docs = query.stream()
    batch = db.batch()
    for doc in docs:
        batch.delete(doc.reference)
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
