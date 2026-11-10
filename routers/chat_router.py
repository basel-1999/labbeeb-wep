"""
💬 محادثات الطالب والمعلم (خارج الحصة)
- الإرسال بيمر من السيرفر: فحص الصلاحية + فلتر الأرقام والروابط + حد للسرعة
- القراءة الفورية من Firestore مباشرة (الرولز بتسمح للطرفين بالقراءة فقط)
"""
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from firebase_admin import firestore

from routers.session_router import db, get_current_user, CLOUDINARY_CLOUD
from routers.chat_filter import check_message

router = APIRouter()

MAX_TEXT_LENGTH = 1000              # أقصى طول للرسالة النصية
MAX_VOICE_SECONDS = 120             # أقصى مدة للرسالة الصوتية (دقيقتين)
RATE_LIMIT_MESSAGES = 20            # أقصى عدد رسائل...
RATE_LIMIT_WINDOW_SECONDS = 60      # ...خلال هالمدة

_recent_sends = defaultdict(deque)


def _check_rate_limit(uid: str):
    now = time.time()
    q = _recent_sends[uid]
    while q and now - q[0] > RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_LIMIT_MESSAGES:
        raise HTTPException(status_code=429, detail="رسائل كثيرة بسرعة، انتظر قليلاً وحاول مرة ثانية.")
    q.append(now)


def ensure_chat(student_id: str, teacher_id: str, student_name: str = "", teacher_name: str = "") -> str:
    """بتنشئ محادثة الطالب والمعلم لو مش موجودة (بتنادى لما المعلم يقبل أول حصة بينهم)"""
    chat_id = f"{student_id}_{teacher_id}"
    chat_ref = db.collection('chats').document(chat_id)
    if chat_ref.get().exists:
        return chat_id
    chat_ref.set({
        'studentId': student_id,
        'teacherId': teacher_id,
        'studentName': (student_name or '')[:80],
        'teacherName': (teacher_name or '')[:80],
        'createdAt': firestore.SERVER_TIMESTAMP,
        'lastMessage': '',
        'lastMessageAt': firestore.SERVER_TIMESTAMP,
        'lastSenderId': '',
        'unreadStudent': 0,
        'unreadTeacher': 0,
    })
    return chat_id


class ChatSendRequest(BaseModel):
    chatId: str
    type: str = "text"                 # text | voice
    text: Optional[str] = None
    audioUrl: Optional[str] = None
    duration: Optional[int] = None     # مدة الرسالة الصوتية بالثواني


@router.post("/api/chat/send")
async def send_chat_message(payload: ChatSendRequest, uid: str = Depends(get_current_user)):
    try:
        chat_ref = db.collection('chats').document(payload.chatId)
        chat_snap = chat_ref.get()
        if not chat_snap.exists:
            raise HTTPException(status_code=404, detail="المحادثة غير موجودة.")
        chat = chat_snap.to_dict() or {}

        # 🔒 لازم تكون أحد طرفي المحادثة
        if uid == chat.get('studentId'):
            role, other_unread = 'student', 'unreadTeacher'
        elif uid == chat.get('teacherId'):
            role, other_unread = 'teacher', 'unreadStudent'
        else:
            raise HTTPException(status_code=403, detail="غير مصرح لك بهذه المحادثة.")

        if payload.type not in ('text', 'voice'):
            raise HTTPException(status_code=400, detail="نوع الرسالة غير صالح.")

        _check_rate_limit(uid)

        message = {
            'senderId': uid,
            'senderRole': role,
            'type': payload.type,
            'createdAt': firestore.SERVER_TIMESTAMP,
        }

        if payload.type == 'text':
            text = (payload.text or '').strip()
            if not text:
                raise HTTPException(status_code=400, detail="الرسالة فاضية.")
            if len(text) > MAX_TEXT_LENGTH:
                raise HTTPException(status_code=400, detail=f"الرسالة طويلة (الحد الأقصى {MAX_TEXT_LENGTH} حرف).")

            blocked = check_message(text)
            if blocked == 'contact':
                raise HTTPException(status_code=400, detail="ممنوع مشاركة أرقام الهواتف أو حسابات التواصل (واتساب وغيره). لم يتم إرسال الرسالة.")
            if blocked == 'link':
                raise HTTPException(status_code=400, detail="ممنوع الروابط إلا الروابط التعليمية (يوتيوب، جوجل درايف، ويكيبيديا، خان أكاديمي). لم يتم إرسال الرسالة.")

            message['text'] = text
            preview = text[:80]
        else:
            audio_url = payload.audioUrl or ''
            if not audio_url.startswith(f"https://res.cloudinary.com/{CLOUDINARY_CLOUD}/"):
                raise HTTPException(status_code=400, detail="رابط الرسالة الصوتية غير صالح.")
            duration = payload.duration or 0
            if duration < 1 or duration > MAX_VOICE_SECONDS:
                raise HTTPException(status_code=400, detail=f"مدة الرسالة الصوتية لازم تكون بين ثانية و{MAX_VOICE_SECONDS} ثانية.")

            message['audioUrl'] = audio_url
            message['duration'] = int(duration)
            preview = '🎤 رسالة صوتية'

        msg_ref = chat_ref.collection('messages').document()
        batch = db.batch()
        batch.set(msg_ref, message)
        batch.update(chat_ref, {
            'lastMessage': preview,
            'lastMessageAt': firestore.SERVER_TIMESTAMP,
            'lastSenderId': uid,
            other_unread: firestore.Increment(1),
        })
        batch.commit()

        return {"id": msg_ref.id}

    except HTTPException:
        raise
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in send_chat_message: {error_str}")
        if "429" in error_str or "Quota" in error_str:
            raise HTTPException(status_code=429, detail="تم تجاوز الحد المجاني لعمليات قاعدة البيانات اليومية. يرجى المحاولة لاحقاً.")
        raise HTTPException(status_code=500, detail="تعذر إرسال الرسالة، حاول مرة ثانية.")