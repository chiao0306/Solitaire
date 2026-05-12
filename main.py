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

# 允許所有網域連線 (讓你的 GitHub Pages 可以連過來)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. 初始化 Firebase 與 Gemini (從 GCP 環境變數讀取)
if not firebase_admin._apps:
    try:
        # 將 GCP 環境變數中的字串轉回 JSON 格式
        cred_json = json.loads(os.environ.get("FIREBASE_KEY", "{}"))
        cred = credentials.Certificate(cred_json)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        print(f"Firebase 初始化失敗: {e}")

db = firestore.client()
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-3.1-flash-lite')
CHAT_COLLECTION = "chat_messages"

# 2. 定義接收資料的格式
class ChatRequest(BaseModel):
    room_name: str
    user_name: str
    text: str
    avatar: str
    last_idiom: Optional[str] = None
    ignore_tone: bool = False

class ActionRequest(BaseModel):
    room_name: str
    user_name: str
    action_type: str 
    text: str
    avatar: str
    target_text: Optional[str] = None

# 3. 核心 API 路由
def check_idiom_connection(last_idiom, new_idiom, ignore_tone=False):
    if not last_idiom or not new_idiom: return True
    last_char = last_idiom[-1]
    first_char = new_idiom[0]
    style = Style.NORMAL if ignore_tone else Style.TONE3
    last_char_pinyins = pinyin(last_char, style=style, heteronym=True)[0]
    first_char_pinyins = pinyin(first_char, style=style, heteronym=True)[0]
    return bool(set(last_char_pinyins).intersection(set(first_char_pinyins)))

@app.post("/send_chat")
async def send_chat(req: ChatRequest):
    if not check_idiom_connection(req.last_idiom, req.text, req.ignore_tone):
        raise HTTPException(status_code=400, detail="拼音或聲調不符！請重新輸入。")
    
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": req.user_name,
        "text": req.text, "type": "chat", "avatar": req.avatar,
        "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}

@app.post("/system_action")
async def system_action(req: ActionRequest):
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": req.user_name,
        "text": req.text, "type": req.action_type, "avatar": req.avatar,
        "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}

@app.post("/call_referee")
async def call_referee(req: ActionRequest):
    prompt = f"請判斷「{req.target_text}」以在台灣教育部最具權威的《成語典》或《重編國語辭典修訂本》判斷是否為正確的中文成語。請用繁體中文回答：『✅ 是成語』或『❌ 不是成語』，並簡述解釋。"
    res = model.generate_content(prompt)
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": "Referee (AI)",
        "text": res.text.strip(), "type": "referee",
        "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}

@app.post("/buy_hint")
async def buy_hint(req: ActionRequest):
    prompt = f"請給出一個以「{req.target_text[-1]}」開頭（或同音）的常見繁體中文四字成語。只需回傳該成語本身，不要標點。"
    res = model.generate_content(prompt)
    ans = res.text.strip()[:4]
    hint_char = ans[2] if len(ans) >= 3 else ans[-1]
    
    db.collection(CHAT_COLLECTION).add({
        "room_name": req.room_name, "user_name": "Referee (AI)",
        "text": f"💡 第一次提示：下一句的第三個字可以是「**{hint_char}**」", 
        "type": "referee", "hint_answer": ans, "requested_by": req.user_name,
        "timestamp": firestore.SERVER_TIMESTAMP
    })
    return {"status": "success"}