"""
💬 محادثات الطالب والمعلم (خارج الحصة)
- الإرسال بيمر من السيرفر: فحص الصلاحية + فلتر الأرقام والروابط + حد للسرعة
- القراءة الفورية من Firestore مباشرة (الرولز بتسمح للطرفين بالقراءة فقط)
- أنواع الرسائل: text | voice | image | file
"""
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from firebase_admin import firestore

from routers.session_router import db, get_current_user, CLOUDINARY_CLOUD, ALLOWED_RAW_EXT
from routers.chat_filter import check_message

router = APIRouter()

MAX_TEXT_LENGTH = 1000              # أقصى طول للرسالة النصية
MAX_VOICE_SECONDS = 120             # أقصى مدة للرسالة الصوتية (دقيقتين)
MAX_FILE_NAME_LENGTH = 100          # أقصى طول لاسم الملف
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
    type: str = "text"                 # text | voice | image | file
    text: Optional[str] = None
    audioUrl: Optional[str] = None
    duration: Optional[int] = None     # مدة الرسالة الصوتية بالثواني
    fileUrl: Optional[str] = None      # رابط الصورة أو الملف (Cloudinary)
    fileName: Optional[str] = None     # اسم الملف (للملفات فقط)
    fileSize: Optional[int] = None     # حجم الملف بالبايت (للعرض فقط)


def _bad_request(msg: str):
    raise HTTPException(status_code=400, detail=msg)


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

        if payload.type not in ('text', 'voice', 'image', 'file'):
            _bad_request("نوع الرسالة غير صالح.")

        _check_rate_limit(uid)

        cloud_base = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD}/"
        message = {
            'senderId': uid,
            'senderRole': role,
            'type': payload.type,
            'createdAt': firestore.SERVER_TIMESTAMP,
        }

        if payload.type == 'text':
            text = (payload.text or '').strip()
            if not text:
                _bad_request("الرسالة فاضية.")
            if len(text) > MAX_TEXT_LENGTH:
                _bad_request(f"الرسالة طويلة (الحد الأقصى {MAX_TEXT_LENGTH} حرف).")

            blocked = check_message(text)
            if blocked == 'contact':
                _bad_request("ممنوع مشاركة أرقام الهواتف أو حسابات التواصل (واتساب وغيره). لم يتم إرسال الرسالة.")
            if blocked == 'link':
                _bad_request("ممنوع الروابط إلا الروابط التعليمية (يوتيوب، جوجل درايف، ويكيبيديا، خان أكاديمي). لم يتم إرسال الرسالة.")

            message['text'] = text
            preview = text[:80]

        elif payload.type == 'voice':
            audio_url = payload.audioUrl or ''
            if not audio_url.startswith(cloud_base):
                _bad_request("رابط الرسالة الصوتية غير صالح.")
            duration = payload.duration or 0
            if duration < 1 or duration > MAX_VOICE_SECONDS:
                _bad_request(f"مدة الرسالة الصوتية لازم تكون بين ثانية و{MAX_VOICE_SECONDS} ثانية.")

            message['audioUrl'] = audio_url
            message['duration'] = int(duration)
            preview = '🎤 رسالة صوتية'

        elif payload.type == 'image':
            file_url = payload.fileUrl or ''
            if not file_url.startswith(cloud_base + "image/upload/"):
                _bad_request("رابط الصورة غير صالح.")

            message['fileUrl'] = file_url
            preview = '📷 صورة'

        else:  # file
            file_url = payload.fileUrl or ''
            name = (payload.fileName or '').strip()
            if not file_url.startswith(cloud_base + "raw/upload/"):
                _bad_request("رابط الملف غير صالح.")
            if not name or len(name) > MAX_FILE_NAME_LENGTH:
                _bad_request("اسم الملف غير صالح.")
            ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            if ext not in ALLOWED_RAW_EXT or not file_url.lower().endswith('.' + ext):
                _bad_request("نوع الملف غير مسموح (PDF أو Word أو PowerPoint أو Excel أو نص فقط).")
            # اسم الملف بيظهر للطرف الثاني، فبيمر على نفس فلتر الأرقام والحسابات
            if check_message(name) is not None:
                _bad_request("اسم الملف فيه رقم هاتف أو حساب تواصل أو رابط. غيّر اسم الملف وحاول مرة ثانية.")

            message['fileUrl'] = file_url
            message['fileName'] = name
            if payload.fileSize is not None and 0 <= payload.fileSize <= 50 * 1024 * 1024:
                message['fileSize'] = int(payload.fileSize)
            preview = '📎 ' + name[:40]

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