from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from pydantic import BaseModel
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore, auth
import cloudinary
import cloudinary.uploader
import os
import requests
import uuid
from dotenv import load_dotenv
from typing import Optional, List
import json 
from datetime import datetime, timezone
import httpx
import asyncio
import re
from urllib.parse import unquote

load_dotenv()

router = APIRouter()
security = HTTPBearer()
router = APIRouter()
security = HTTPBearer()

# 💰 جدول الباقات المعتمد (المصدر الوحيد للأسعار): { عدد النقاط: السعر }
# لازم يطابق الباقات المعروضة بـ student_dashboard.html (6 نقاط = ساعة)
PRICING_PACKAGES = {
    'ar': {6: 50, 60: 450, 120: 850, 180: 1200},
    'en': {6: 80, 60: 800, 120: 1500, 180: 2100},
}

# ⭐ نظام النجوم والمكافآت
STARS_PER_SESSION_LIMIT = 3   # أقصى عدد نجوم يمنحها المعلم بالحصة الواحدة
STAR_COOLDOWN_SECONDS = 10    # الفاصل بين كل نجمة والتي بعدها
STARS_PER_BASKET = 10         # كل 10 نجوم = كأس وبتفضى السلة


# 🎓 المقابلة التجريبية للمعلم
TRIAL_INTERVIEW_SECONDS = 900   # 15 دقيقة بالضبط
TRIAL_MIN_SECONDS_TO_COMPLETE = 870   # حد أدنى للقبول (فيه سماح 30 ثانية لفرق الساعات)
CLOUDINARY_CLOUD = os.getenv("CLOUDINARY_CLOUD_NAME", "f4t8ayoq")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://labbeeb-wep.onrender.com").rstrip("/")
# إعداد Cloudinary باستخدام متغيرات البيئة (أكثر أماناً)
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# تهيئة Firebase Admin
if not firebase_admin._apps:
    cred_dict = None
    cred_path = "serviceAccountKey.json"

    # 1️⃣ محاولة القراءة محلياً من الملف (على جهازك)
    if os.path.exists(cred_path):
        try:
            with open(cred_path, 'r', encoding='utf-8') as f:
                cred_dict = json.load(f)
            print("📂 Loaded Firebase keys from local serviceAccountKey.json")
        except Exception as e:
            print(f"❌ Error reading local JSON: {e}")

    # 2️⃣ إذا لم يوجد الملف محلياً (على Render)، اقرأ من متغير البيئة
    elif os.environ.get("FIREBASE_CREDENTIALS"):
        try:
            raw_env = os.environ.get("FIREBASE_CREDENTIALS")
            cred_dict = json.loads(raw_env)
            print("🌐 Loaded Firebase keys from Environment Variable")
        except Exception as e:
            print(f"❌ Error parsing FIREBASE_CREDENTIALS env var: {e}")

    # 3️⃣ معالجة المفتاح وتنظيف الـ Private Key وتفعيل Firebase
    if cred_dict:
        try:
            if "private_key" in cred_dict:
                pk = cred_dict["private_key"]
                pk = pk.replace("\\\\n", "\n").replace("\\n", "\n").replace("\\r", "").strip()
                cred_dict["private_key"] = pk

            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            print("✅ Firebase initialized successfully.")
        except Exception as e:
            print(f"❌ Error initializing Firebase SDK: {e}")
    else:
        print("⚠️ Warning: No Firebase credentials found locally or in environment!")

db = firestore.client()

# ==========================================
# 🔒 Middleware للتحقق من المستخدم (Bearer Token)
# ==========================================
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        decoded_token = auth.verify_id_token(token)
        return decoded_token['uid']
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="يجب تسجيل الدخول أولاً لإنشاء طلب."
        )

# ==========================================
# 💰 إدارة الجلسات والخصم الآمن
# ==========================================

@router.post("/api/session/create")
async def create_session_request(
    studentName: str = Form(...),
    subject: str = Form(...),
    topic: str = Form(...),
    grade: str = Form("غير محدد"),  # ✨ تمت إضافة المرحلة الدراسية
    language: str = Form("ar"),  # ✨ لغة الدراسة المطلوبة (ar أو en)
    isFreeTrial: bool = Form(False),  # ✨ إضافة حقل الحصة التجريبية
    notes: str = Form(""),  # ملاحظات الطالب للمعلم
    preferredTeacherId: str = Form(""),  # معلم معين (اختياري)
    preferredTeacherName: str = Form(""),
    uid: str = Depends(get_current_user)
):
    try:
        user_ref = db.collection('users').document(uid)
        user_snap = user_ref.get()

        if not user_snap.exists:
            raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")

        user_data = user_snap.to_dict()

        # ✨ التحقق من الحصة التجريبية
        if isFreeTrial:
            if user_data.get('hasUsedFreeTrial', False):
                raise HTTPException(status_code=403, detail="لقد قمت باستخدام حصتك التجريبية المجانية مسبقاً.")

            # 🔒 منع طلب حصة تجريبية ثانية طالما في واحدة لسا ما انتهت
            existing_trials = db.collection('sessions') \
                .where('studentId', '==', uid) \
                .where('isFreeTrial', '==', True) \
                .stream()
            for s in existing_trials:
                if s.to_dict().get('status') in ('pending', 'accepted', 'active', 'interrupted'):
                    raise HTTPException(status_code=403, detail="لديك حصة تجريبية قيد الانتظار أو التنفيذ بالفعل.")
        else:
            raw_points = user_data.get('points', 0)
            current_points = raw_points if isinstance(raw_points, (int, float)) else 0
            if current_points <= 0:
                raise HTTPException(status_code=400, detail="لا يوجد لديك نقاط كافية.")

        # 🔒 لو الطالب طلب معلم معين: لازم يكون موجود ومعتمد
        if preferredTeacherId:
            pref_snap = db.collection('users').document(preferredTeacherId).get()
            pref_data = pref_snap.to_dict() if pref_snap.exists else {}
            if pref_data.get('role') != 'teacher' or pref_data.get('status') != 'approved':
                raise HTTPException(status_code=400, detail="المعلم المختار غير متاح حالياً.")

        session_ref = db.collection('sessions').document()
        session_ref.set({
            'studentId': uid,
            'studentName': studentName,
            'teacherId': None,
            'assignedTeacherId': None,
            'teacherName': None,
            'teacher': None,
            'instructorName': None,
            'subject': subject,
            'topic': topic,
            'grade': grade,
            'language': language,
            'status': 'pending',
            'studentJoinedAt': None,
            'teacherJoinedAt': None,
            'timerStartedAt': None,
            'pointsDeducted': 0,
            'lastDeductionAt': None,
            'bookingType': 'now',  
            'createdAt': firestore.SERVER_TIMESTAMP,
            'audioRecordingUrl': None,
            'boardPdfUrl': None,
            'isFreeTrial': isFreeTrial,  # ✨ حفظ علم الحصة التجريبية
            'notes': notes.strip()[:500],
            'preferredTeacherId': preferredTeacherId or None,
            'preferredTeacherName': preferredTeacherName or None,
        })

        return {"sessionId": session_ref.id, "message": "تم إنشاء الطلب بنجاح"}

    except HTTPException:
        raise # إعادة رمي أخطاء HTTP المخصصة (مثل خطأ الرصيد 0) لتصل للمستخدم مباشرة

    # ✨ هذا هو الكود الذي يلتقط الخطأ من Render ويعرضه بشكل مفهوم
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in create_session: {error_str}")
        if "429" in error_str or "Quota" in error_str:
            raise HTTPException(status_code=429, detail="تم تجاوز الحد المجاني لعمليات قاعدة البيانات اليومية. يرجى المحاولة غداً أو ترقية الباقة.")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")


@router.post("/api/session/join/{sessionId}")
async def join_session(sessionId: str, uid: str = Depends(get_current_user)):
    try:
        session_ref = db.collection('sessions').document(sessionId)
        session_snap = session_ref.get()

        if not session_snap.exists:
            raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

        session_data = session_snap.to_dict()

        is_student = session_data.get('studentId') == uid
        is_teacher = session_data.get('teacherId') == uid

        if not is_student and not is_teacher:
            raise HTTPException(status_code=403, detail="غير مصرح لك بدخول هذه الجلسة.")

        update_data = {}

        if is_student and session_data.get('studentJoinedAt') is None:
            update_data['studentJoinedAt'] = firestore.SERVER_TIMESTAMP

        if is_teacher and session_data.get('teacherJoinedAt') is None:
            update_data['teacherJoinedAt'] = firestore.SERVER_TIMESTAMP

        if update_data:
            session_ref.update(update_data)

        # إعادة قراءة الوثيقة بعد التحديث للتأكد من حالة الطرفين
        session_snap = session_ref.get()
        session_data = session_snap.to_dict()

        student_joined = session_data.get('studentJoinedAt') is not None
        teacher_joined = session_data.get('teacherJoinedAt') is not None

        if student_joined and teacher_joined and session_data.get('timerStartedAt') is None:
            session_ref.update({
                'timerStartedAt': firestore.SERVER_TIMESTAMP,
                'status': 'active'
            })
        elif session_data.get('status') == 'interrupted' and session_data.get('timerStartedAt') is not None:
            # رجوع بعد Refresh: استأنف الحصة والخصم
            session_ref.update({
                'status': 'active',
                'interruptedBy': firestore.DELETE_FIELD
            })

        # ⭐ بيانات النجوم للواجهة (السلة عند الطالب والمعلم)
        student_stars = 0
        student_id_for_stars = session_data.get('studentId')
        if student_id_for_stars:
            stars_snap = db.collection('users').document(student_id_for_stars).get()
            if stars_snap.exists:
                student_stars = int(stars_snap.to_dict().get('totalStars', 0))

        return {
            "message": "تم تسجيل الدخول",
            "studentStars": student_stars,
            "starsAwarded": int(session_data.get('starsAwarded', 0)),
            "starsLimit": STARS_PER_SESSION_LIMIT
        }

    except HTTPException:
        raise
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in join_session: {error_str}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")


@router.post("/api/session/deduct/{sessionId}")
async def deduct_session_points(sessionId: str, uid: str = Depends(get_current_user)):
    try:
        session_ref = db.collection('sessions').document(sessionId)
        session_snap = session_ref.get()

        if not session_snap.exists:
            raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

        session_data = session_snap.to_dict()

        if session_data.get('studentId') != uid and session_data.get('teacherId') != uid:
            raise HTTPException(status_code=403, detail="غير مصرح لك بهذا الإجراء.")

        # ✨ إيقاف الخصم فوراً إذا كانت الحصة تجريبية مجانية
        if session_data.get('isFreeTrial'):
            return {"message": "جلسة تجريبية مجانية، لا يتم الخصم", "pointsDeducted": 0}

        # 🎓 مقابلة تجريبية للمعلم: ما في طالب ولا خصم
        if session_data.get('isTrialInterview'):
            return {"message": "مقابلة تجريبية للمعلم، لا يتم الخصم", "pointsDeducted": 0}

        now_utc = datetime.now(timezone.utc)
        log_ts = now_utc.strftime('%H:%M:%S')

        timer_started_at = session_data.get('timerStartedAt')
        if timer_started_at is None or session_data.get('status') != 'active':
            print(f"🕐 [{log_ts}] session={sessionId} DEDUCT SKIPPED → "
                  f"timerStartedAt={timer_started_at} status={session_data.get('status')}")
            return {"message": "التايمر لم يبدأ بعد", "pointsDeducted": session_data.get('pointsDeducted', 0)}

        # عدد الفواصل (كل 5 دقائق) التي يجب أن تكون قد تم خصمها حتى الآن
        elapsed_seconds = (now_utc - timer_started_at).total_seconds()
        intervals_due = int(elapsed_seconds // 300)  # كل 300 ثانية = 5 دقائق

        already_deducted = session_data.get('pointsDeducted', 0)
        intervals_already_done = int(round(already_deducted / 0.5))  # كل 5 دقائق = نص نقطة

        intervals_to_deduct = intervals_due - intervals_already_done

        print(f"🕐 [{log_ts}] session={sessionId} elapsed={elapsed_seconds:.0f}s "
              f"intervals_due={intervals_due} intervals_already_done={intervals_already_done} "
              f"already_deducted={already_deducted} intervals_to_deduct={intervals_to_deduct}")

        if intervals_to_deduct <= 0:
            print(f"🕐 [{log_ts}] session={sessionId} → لا يوجد خصم مستحق الآن")
            return {"message": "لا يوجد خصم مستحق الآن", "pointsDeducted": already_deducted}

        points_to_deduct = intervals_to_deduct * 0.5  # نص نقطة لكل 5 دقائق
        student_id = session_data.get('studentId')
        student_ref = db.collection('users').document(student_id)
        student_snap = student_ref.get()

        if not student_snap.exists:
            print(f"🕐 [{log_ts}] session={sessionId} ❌ student {student_id} not found")
            raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")

        student_data = student_snap.to_dict()
        current_points = student_data.get('points', 0)
        new_points = max(0, current_points - points_to_deduct)

        student_ref.update({'points': new_points})

        session_update = {
            'pointsDeducted': already_deducted + points_to_deduct,
            'lastDeductionAt': firestore.SERVER_TIMESTAMP
        }

        # ⛔ إنهاء الحصة تلقائياً لما النقاط تنفد
        session_ended = False
        if new_points <= 0:
            session_update['status'] = 'completed'
            session_update['completedAt'] = firestore.SERVER_TIMESTAMP
            session_update['endReason'] = 'out_of_points'
            session_ended = True

        session_ref.update(session_update)

        print(f"🕐 [{log_ts}] session={sessionId} ✅ DEDUCTED {points_to_deduct} → "
              f"totalDeducted={already_deducted + points_to_deduct} studentPointsBefore={current_points} "
              f"studentPointsAfter={new_points} sessionEnded={session_ended}")

        return {
            "message": "تم الخصم",
            "pointsDeducted": already_deducted + points_to_deduct,
            "studentPoints": new_points,
            "sessionEnded": session_ended
        }

    except HTTPException:
        raise
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in deduct_session_points: {error_str}")
        if "429" in error_str or "Quota" in error_str:
            raise HTTPException(status_code=429, detail="تم تجاوز الحد المجاني لعمليات قاعدة البيانات اليومية. يرجى المحاولة لاحقاً.")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")


@router.post("/api/session/leave/{sessionId}")
async def leave_session(sessionId: str, uid: str = Depends(get_current_user)):
    try:
        session_ref = db.collection('sessions').document(sessionId)
        session_snap = session_ref.get()

        if not session_snap.exists:
            raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

        session_data = session_snap.to_dict()

        if session_data.get('studentId') != uid and session_data.get('teacherId') != uid:
            raise HTTPException(status_code=403, detail="غير مصرح لك بإنهاء هذه الجلسة.")

        if session_data.get('status') not in ('completed', 'cancelled'):
            session_ref.update({
                'status': 'completed',
                'completedAt': firestore.SERVER_TIMESTAMP
            })  
            # ✨ تحديث حالة الحصة التجريبية للطالب عند المغادرة
            if session_data.get('isFreeTrial'):
                student_id_for_trial = session_data.get('studentId')
                if student_id_for_trial:
                    db.collection('users').document(student_id_for_trial).update({
                        'hasUsedFreeTrial': True
                    })

        return {"message": "تم إنهاء الجلسة"}

    except HTTPException:
        raise
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in leave_session: {error_str}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")


@router.post("/api/session/interrupt/{sessionId}")
async def interrupt_session(sessionId: str, uid: str = Depends(get_current_user)):
    try:
        session_ref = db.collection('sessions').document(sessionId)
        session_snap = session_ref.get()

        if not session_snap.exists:
            return {"message": "الجلسة غير موجودة، تم التجاهل"}

        session_data = session_snap.to_dict()

        is_student = session_data.get('studentId') == uid
        is_teacher = session_data.get('teacherId') == uid

        if not is_student and not is_teacher:
            raise HTTPException(status_code=403, detail="غير مصرح لك بهذا الإجراء.")

        if session_data.get('status') == 'completed':
            return {"message": "الجلسة منتهية بالفعل"}

        session_ref.update({
            'status': 'interrupted',
            'interruptedBy': 'teacher' if is_teacher else 'student'
        })

        # ✨ تحديث حالة الحصة التجريبية للطالب عند الانقطاع المفاجئ
        if session_data.get('isFreeTrial'):
            student_id_for_trial = session_data.get('studentId')
            if student_id_for_trial:
                db.collection('users').document(student_id_for_trial).update({
                    'hasUsedFreeTrial': True
                })

        return {"message": "تم تسجيل الانقطاع"}

    except HTTPException:
        raise
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in interrupt_session: {error_str}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")



@firestore.transactional
def _award_star_txn(transaction, session_ref, student_ref, teacher_uid):
    """منح نجمة بشكل ذري: كل الفحوصات والتحديثات بنفس العملية عشان ما يصير تكرار أو تجاوز للحد."""
    session_snap = session_ref.get(transaction=transaction)
    if not session_snap.exists:
        raise HTTPException(status_code=404, detail="الجلسة غير موجودة")
    s = session_snap.to_dict()

    if s.get('teacherId') != teacher_uid:
        raise HTTPException(status_code=403, detail="فقط معلم الحصة يستطيع منح النجوم.")
    if s.get('status') != 'active':
        raise HTTPException(status_code=400, detail="يمكن منح النجوم فقط أثناء الحصة النشطة.")

    awarded = int(s.get('starsAwarded', 0))
    last_star_at = s.get('lastStarAt')
    if last_star_at is not None:
        elapsed = (datetime.now(timezone.utc) - last_star_at).total_seconds()
        if elapsed < STAR_COOLDOWN_SECONDS:
            raise HTTPException(status_code=429, detail="انتظر ثواني قبل منح نجمة أخرى.")

    student_snap = student_ref.get(transaction=transaction)
    if not student_snap.exists:
        raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")
    total = int(student_snap.to_dict().get('totalStars', 0)) + 1

    transaction.update(student_ref, {'totalStars': total})
    transaction.update(session_ref, {
        'starsAwarded': awarded + 1,
        'lastStarAt': firestore.SERVER_TIMESTAMP
    })
    return total, awarded + 1

@firestore.transactional
def _award_star_trial_txn(transaction, session_ref, teacher_uid):
    """نجمة تجريبية بالمقابلة: بتنعد بالجلسة بس، بدون أي طالب"""
    snap = session_ref.get(transaction=transaction)
    if not snap.exists:
        raise HTTPException(status_code=404, detail="الجلسة غير موجودة")
    s = snap.to_dict() or {}

    if s.get('teacherId') != teacher_uid or not s.get('isTrialInterview'):
        raise HTTPException(status_code=403, detail="غير مصرح لك بهذا الإجراء.")
    if s.get('status') != 'active':
        raise HTTPException(status_code=400, detail="المقابلة غير نشطة.")

    awarded = int(s.get('starsAwarded', 0))
    if awarded >= STARS_PER_SESSION_LIMIT:
        raise HTTPException(status_code=429, detail=f"وصلت للحد الأقصى ({STARS_PER_SESSION_LIMIT} نجوم) لهذه الحصة.")

    last_star_at = s.get('lastStarAt')
    if last_star_at is not None:
        elapsed = (datetime.now(timezone.utc) - last_star_at).total_seconds()
        if elapsed < STAR_COOLDOWN_SECONDS:
            raise HTTPException(status_code=429, detail="انتظر ثواني قبل منح نجمة أخرى.")

    transaction.update(session_ref, {
        'starsAwarded': awarded + 1,
        'lastStarAt': firestore.SERVER_TIMESTAMP
    })
    return awarded + 1

@router.post("/api/session/award-star/{sessionId}")
async def award_star(sessionId: str, uid: str = Depends(get_current_user)):
    try:
        session_ref = db.collection('sessions').document(sessionId)
        session_snap = session_ref.get()
        if not session_snap.exists:
            raise HTTPException(status_code=404, detail="الجلسة غير موجودة")


        # 🎓 مقابلة تجريبية: النجوم بتنعد بالجلسة بس (ما في طالب حقيقي)
        if (session_snap.to_dict() or {}).get('isTrialInterview'):
            awarded = _award_star_trial_txn(db.transaction(), session_ref, uid)
            t_pos = awarded % STARS_PER_BASKET
            t_milestone = 'cup' if t_pos == 0 else ('gold' if t_pos == 6 else ('silver' if t_pos == 3 else None))
            session_ref.collection('rewards').add({
                'total': awarded,
                'milestone': t_milestone,
                'teacherId': uid,
                'createdAt': firestore.SERVER_TIMESTAMP
            })
            return {
                "total": awarded,
                "starsAwarded": awarded,
                "starsLimit": STARS_PER_SESSION_LIMIT,
                "milestone": t_milestone
            }

        student_id = session_snap.to_dict().get('studentId')
        if not student_id:
            raise HTTPException(status_code=400, detail="لا يوجد طالب بهذه الحصة.")
        
        student_ref = db.collection('users').document(student_id)

        total, awarded = _award_star_txn(db.transaction(), session_ref, student_ref, uid)

        # 3 = ميدالية فضية، 6 = ميدالية ذهبية، 10 = كأس (بكل دورة سلة)
        position = total % STARS_PER_BASKET
        milestone = None
        if position == 0:
            milestone = 'cup'
        elif position == 6:
            milestone = 'gold'
        elif position == 3:
            milestone = 'silver'

        # إشارة الأنيميشن للطرفين (المعلم والطالب)
        session_ref.collection('rewards').add({
            'total': total,
            'milestone': milestone,
            'teacherId': uid,
            'createdAt': firestore.SERVER_TIMESTAMP
        })

        # 🔔 إشعارات الطالب: نجمة جديدة، وإذا اكتملت ميدالية أو كأس بيوصله إشعار ثاني
        try:
            lang = (student_ref.get().to_dict() or {}).get('preferredLanguage', 'ar')
            raw_name = ((session_snap.to_dict() or {}).get('teacherName') or '').strip()
            teacher_name = re.sub(r'^(أ\.|الأستاذ|Mr\.)\s*', '', raw_name).strip()

            if lang == 'en':
                star_body = f"Teacher {teacher_name} awarded you a star." if teacher_name else "Your teacher awarded you a star."
                texts = {
                    'star': ('New star ⭐', star_body),
                    'silver': ('Silver Medal 🥈', 'You earned a Silver Medal! You collected 3 stars.'),
                    'gold': ('Gold Medal 🥇', 'You earned a Gold Medal! You collected 6 stars.'),
                    'cup': ('Champion Cup 🏆', 'You earned the Cup! You collected 10 stars.'),
                }
            else:
                star_body = f"تم منحك نجمة من الأستاذ {teacher_name}." if teacher_name else "تم منحك نجمة من معلمك."
                texts = {
                    'star': ('نجمة جديدة ⭐', star_body),
                    'silver': ('ميدالية فضية 🥈', 'لقد حصلت على ميدالية فضية! أكملت 3 نجوم.'),
                    'gold': ('ميدالية ذهبية 🥇', 'لقد حصلت على ميدالية ذهبية! أكملت 6 نجوم.'),
                    'cup': ('كأس البطل 🏆', 'لقد حصلت على كأس! أكملت 10 نجوم.'),
                }

            to_send = [texts['star']]
            if milestone:
                to_send.append(texts[milestone])

            for title, body in to_send:
                student_ref.collection('notifications').add({
                    'title': title,
                    'body': body,
                    'read': False,
                    'createdAt': firestore.SERVER_TIMESTAMP
                })
        except Exception as e:
            print(f"Failed to send reward notification: {e}")

        return {
            "total": total,
            "starsAwarded": awarded,
            "starsLimit": STARS_PER_SESSION_LIMIT,
            "milestone": milestone
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in award_star: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")
    
@router.post("/api/session/cancel/{sessionId}")
async def cancel_session_request(sessionId: str, uid: str = Depends(get_current_user)):
    try:
        session_ref = db.collection('sessions').document(sessionId)
        session_snap = session_ref.get()

        if not session_snap.exists:
            raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

        session_data = session_snap.to_dict()
        
        # التأكد أن الجلسة لا تزال معلقة
        if session_data.get('status') != 'pending':
            raise HTTPException(status_code=400, detail="لا يمكن إلغاء هذا الطلب لأنه لم يعد معلقاً.")

        # التأكد أن من يطلب الإلغاء هو صاحب الطلب
        if session_data.get('studentId') != uid:
            raise HTTPException(status_code=403, detail="غير مصرح لك بإلغاء هذا الطلب.")

        session_ref.update({
            'status': 'cancelled',
            'cancelledAt': firestore.SERVER_TIMESTAMP
        })

        return {"message": "تم إلغاء الطلب بنجاح"}

    except HTTPException:
        raise # إعادة رمي أخطاء HTTP المخصصة
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in cancel_session: {error_str}")
        if "429" in error_str or "Quota" in error_str:
            raise HTTPException(status_code=429, detail="تم تجاوز الحد المجاني لعمليات قاعدة البيانات اليومية.")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")




@router.post("/api/session/accept/{sessionId}")
async def accept_session(
    sessionId: str, 
    teacherId: str = Form(...), 
    teacherName: str = Form(...),
    uid: str = Depends(get_current_user)
):
    if uid != teacherId:
        raise HTTPException(status_code=403, detail="إجراء غير مصرح به: بيانات المعلم غير متطابقة.")

    session_ref = db.collection('sessions').document(sessionId)
    session_snap = session_ref.get()

    if not session_snap.exists:
        raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

    session_data = session_snap.to_dict()
    
    current_status = session_data.get('status')
    if current_status == 'pending':
        # 🔒 لو الطالب طلب معلم معين، ما حدا غيره بيقبل الطلب
        preferred_teacher = session_data.get('preferredTeacherId')
        if preferred_teacher and preferred_teacher != uid:
            raise HTTPException(status_code=403, detail="هذا الطلب موجه لمعلم آخر.")
        # 🛡️ فلترة ذكية (Smart Routing Security): التأكد من أن المعلم يدرس هذه المادة والمرحلة
        user_ref = db.collection('users').document(uid)
        user_snap = user_ref.get()
        if (not user_snap.exists
                or user_snap.to_dict().get('role') != 'teacher'
                or user_snap.to_dict().get('status') != 'approved'):
            raise HTTPException(status_code=403, detail="حسابك غير معتمد لقبول الطلبات بعد.")

        if user_snap.exists:
            teacher_data = user_snap.to_dict()
            teacher_subjects = teacher_data.get('subjects', [])
            teacher_ages = teacher_data.get('targetAge', [])
            
            session_subject = session_data.get('subject')
            session_grade = session_data.get('grade')
            session_language = session_data.get('language', 'ar')
            teacher_teaching_language = teacher_data.get('teachingLanguage', 'العربية')  # افتراضي: عربي فقط للمعلمين القدامى

            def teacher_supports_language(teaching_language: str, target_lang: str) -> bool:
                if teaching_language == 'كلاهما':
                    return True
                if teaching_language == 'الإنجليزية (انترناشونال)':
                    return target_lang == 'en'
                return target_lang == 'ar'
            
            if session_subject and session_subject not in teacher_subjects:
                raise HTTPException(status_code=403, detail="لا يمكنك قبول هذا الطلب لأنه لا يطابق تخصصاتك.")
            
            if session_grade and session_grade not in teacher_ages:
                raise HTTPException(status_code=403, detail="لا يمكنك قبول هذا الطلب لأنه لا يطابق المراحل الدراسية التي تدرسها.")

            if not teacher_supports_language(teacher_teaching_language, session_language):
                raise HTTPException(status_code=403, detail="لا يمكنك قبول هذا الطلب لأنه لا يطابق لغة التدريس المتاحة لديك.")
        
        session_ref.update({
            'teacherId': teacherId,
            'assignedTeacherId': teacherId,
            'status': 'accepted',
            'acceptedAt': firestore.SERVER_TIMESTAMP,
            'teacherName': teacherName,
            'teacher': teacherName,
            'instructorName': teacherName,
        })

                # ✨ إرسال إشعار للطالب بقبول الطلب
        student_id = session_data.get('studentId')
        if student_id:
            db.collection('users').document(student_id).collection('notifications').add({
                'title': 'تم قبول طلبك! 🎉',
                'body': f'تم إسنادك إلى المعلم {teacherName} في مادة {session_data.get("subject", "")}. اضغط للدخول للجلسة.',
                'read': False,
                'createdAt': firestore.SERVER_TIMESTAMP
            })
        return {"message": "تم قبول الطلب بنجاح"}
    else:
        raise HTTPException(status_code=400, detail="تعذر قبول الطلب، قد يكون قد تم قبوله من معلم آخر.")

@router.get("/api/session/pending")
async def listen_to_pending_sessions(uid: str = Depends(get_current_user)):
    sessions_ref = db.collection('sessions').where('status', '==', 'pending')
    docs = sessions_ref.stream()
    
    pending_list = []
    for doc in docs:
        data = doc.to_dict()
        data['id'] = doc.id
        if 'createdAt' in data and data['createdAt']:
            data['createdAt'] = data['createdAt'].isoformat()
        pending_list.append(data)
        
    return {"sessions": pending_list}

# ==========================================
# 🧑‍🏫 تسجيل المعلم (رفع الشهادة)
# ==========================================
@router.post("/api/teacher/register")
async def register_teacher(
    subject: str = Form(...),
    experience: int = Form(...),
    targetAge: str = Form(...),
    teachingLanguage: str = Form("العربية"),
    ownedDevices: str = Form(""),  # ✨ مفصولة بفاصلة عربية "،" مثل subject/targetAge
    academicDegree: str = Form("لا أملك"),  # ✨ جديد
    certificateUrl: str = Form(...),
    certificateName: str = Form(...),
    advancedDegreeCertificateUrl: str = Form(""),  # ✨ اختياري، فقط لو ماجستير/دكتوراه
    uid: str = Depends(get_current_user)
):
    try:
        update_data = {
            'subject': subject,
            'subjects': [s.strip() for s in subject.split('،')],
            'role': 'teacher',
            'experienceYears': experience,
            'targetAge': [a.strip() for a in targetAge.split('،')],
            'teachingLanguage': teachingLanguage,
            'ownedDevices': [d.strip() for d in ownedDevices.split('،') if d.strip()],
            'academicDegree': academicDegree,
            'certificates': [certificateUrl],
            'certificateUrl': certificateUrl,
            'certificateName': certificateName,
            'status': 'pending_approval',
            'updatedAt': firestore.SERVER_TIMESTAMP
        }

        if advancedDegreeCertificateUrl:
            update_data['advancedDegreeCertificateUrl'] = advancedDegreeCertificateUrl

        db.collection('users').document(uid).set(update_data, merge=True)
        
        return {"message": "تم إرسال بياناتك وشهادتك للإدارة بنجاح!"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"حدث خطأ أثناء الحفظ: {str(e)}")

# ==========================================
# 🎓 المقابلة التجريبية للمعلم (15 دقيقة، طالب وهمي، تسجيل فيديو)
# ==========================================
def _get_trial_session(session_id: str, teacher_uid: str):
    """يرجع (مرجع الجلسة، بياناتها) بشرط إنها مقابلة تجريبية تخص هالمعلم"""
    ref = db.collection('sessions').document(session_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="الجلسة غير موجودة")
    data = snap.to_dict() or {}
    if not data.get('isTrialInterview') or data.get('teacherId') != teacher_uid:
        raise HTTPException(status_code=403, detail="غير مصرح لك بهذا الإجراء.")
    return ref, data


class TrialCompleteRequest(BaseModel):
    videoUrl: str


@router.post("/api/teacher/trial/start")
async def start_trial_interview(uid: str = Depends(get_current_user)):
    """إنشاء جلسة مقابلة تجريبية للمعلم (بدون طالب)"""
    try:
        teacher_ref = db.collection('users').document(uid)
        snap = teacher_ref.get()
        if not snap.exists:
            raise HTTPException(status_code=404, detail="الحساب غير موجود.")
        teacher = snap.to_dict() or {}
        if teacher.get('role') != 'teacher':
            raise HTTPException(status_code=403, detail="هذا الإجراء للمعلمين فقط.")
        if teacher.get('status') != 'trial':
            raise HTTPException(status_code=400, detail="المقابلة متاحة فقط للمعلم التجريبي.")

        # أي محاولة سابقة ما انتهت بتنلغي
        old_sessions = db.collection('sessions') \
            .where('teacherId', '==', uid) \
            .where('isTrialInterview', '==', True) \
            .stream()
        for old in old_sessions:
            if (old.to_dict() or {}).get('status') in ('active', 'interrupted'):
                old.reference.update({'status': 'cancelled', 'cancelledAt': firestore.SERVER_TIMESTAMP})

        name = teacher.get('name') or 'معلم'
        session_ref = db.collection('sessions').document()
        session_ref.set({
            'studentId': None,
            'studentName': 'طالب وهمي',
            'teacherId': uid,
            'assignedTeacherId': uid,
            'teacherName': name,
            'teacher': name,
            'instructorName': name,
            'subject': 'مقابلة تجريبية',
            'topic': 'مقابلة تجريبية',
            'grade': 'غير محدد',
            'language': 'ar',
            'status': 'active',
            'isTrialInterview': True,
            'isFreeTrial': False,
            'studentJoinedAt': None,
            'teacherJoinedAt': None,
            'timerStartedAt': None,
            'pointsDeducted': 0,
            'lastDeductionAt': None,
            'bookingType': 'now',
            'starsAwarded': 0,
            'createdAt': firestore.SERVER_TIMESTAMP,
            'audioRecordingUrl': None,
            'boardPdfUrl': None,
        })
        return {"sessionId": session_ref.id, "studentName": "طالب وهمي"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in start_trial_interview: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")


@router.post("/api/teacher/trial/begin/{sessionId}")
async def begin_trial_interview(sessionId: str, uid: str = Depends(get_current_user)):
    """بدء عدّاد الـ 15 دقيقة (بوقت السيرفر). بيشتغل مرة وحدة لكل مقابلة."""
    try:
        session_ref, s = _get_trial_session(sessionId, uid)
        if s.get('status') != 'active':
            raise HTTPException(status_code=400, detail="هذه المقابلة لم تعد نشطة، ابدأ مقابلة جديدة.")
        if s.get('timerStartedAt') is not None:
            raise HTTPException(status_code=400, detail="بدأت هذه المقابلة مسبقاً، ابدأ مقابلة جديدة.")
        session_ref.update({'timerStartedAt': firestore.SERVER_TIMESTAMP})
        return {"durationSeconds": TRIAL_INTERVIEW_SECONDS}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in begin_trial_interview: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")


@router.post("/api/teacher/trial/complete/{sessionId}")
async def complete_trial_interview(sessionId: str, payload: TrialCompleteRequest, uid: str = Depends(get_current_user)):
    """حفظ المقابلة بعد رفع الفيديو: المعلم بيصير 'interviewed'"""
    try:
        session_ref, s = _get_trial_session(sessionId, uid)
        teacher_ref = db.collection('users').document(uid)
        teacher = (teacher_ref.get().to_dict() or {})

        # لو انحفظت مسبقاً (إعادة إرسال) منرجع نجاح
        if teacher.get('status') == 'interviewed' and teacher.get('interviewSessionId') == sessionId:
            return {"message": "المقابلة محفوظة مسبقاً"}
        if teacher.get('status') != 'trial':
            raise HTTPException(status_code=400, detail="لا يمكن حفظ المقابلة بحالة حسابك الحالية.")

        started = s.get('timerStartedAt')
        if started is None:
            raise HTTPException(status_code=400, detail="لم تبدأ المقابلة بعد.")
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed < TRIAL_MIN_SECONDS_TO_COMPLETE:
            raise HTTPException(status_code=400, detail="لم تكتمل مدة المقابلة (15 دقيقة) بعد.")

        video_url = (payload.videoUrl or '').strip()
        if not video_url.startswith(f"https://res.cloudinary.com/{CLOUDINARY_CLOUD}/video/"):
            raise HTTPException(status_code=400, detail="رابط الفيديو غير صالح.")

        batch = db.batch()
        batch.update(teacher_ref, {
            'status': 'interviewed',
            'interviewVideoUrl': video_url,
            'interviewSessionId': sessionId,
            'interviewCompletedAt': firestore.SERVER_TIMESTAMP,
        })
        batch.update(session_ref, {
            'status': 'completed',
            'completedAt': firestore.SERVER_TIMESTAMP,
        })
        batch.commit()
        return {"message": "تم حفظ المقابلة بنجاح"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in complete_trial_interview: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")


@router.post("/api/teacher/trial/cancel/{sessionId}")
async def cancel_trial_interview(sessionId: str, uid: str = Depends(get_current_user)):
    """إلغاء المقابلة (بدون حفظ). المعلم بيضل 'trial' وبيعيدها."""
    try:
        session_ref, s = _get_trial_session(sessionId, uid)
        if s.get('status') in ('active', 'interrupted'):
            session_ref.update({'status': 'cancelled', 'cancelledAt': firestore.SERVER_TIMESTAMP})
        return {"message": "تم إلغاء المقابلة"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in cancel_trial_interview: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")
    
    # ==========================================
# 💰 طلبات شحن الرصيد: الإدارة (admin) أو المسؤول المالي (finance)
# ==========================================
VALID_RECHARGE_POINTS = {6, 60, 120, 180}   # لازم تطابق الباقات المعروضة للطالب


class RechargeRejectRequest(BaseModel):
    reason: str = ""


async def get_current_staff(uid: str = Depends(get_current_user)):
    """مستخدم من الإدارة: admin (كل الصلاحيات) أو finance (طلبات الشحن فقط).
    الدور بيجي من Firestore (مش من المتصفح)، وبنتأكد إنه الحساب مش معطّل."""
    try:
        fb_user = auth.get_user(uid)
    except Exception:
        raise HTTPException(status_code=401, detail="يجب تسجيل الدخول.")
    if fb_user.disabled:
        raise HTTPException(status_code=403, detail="هذا الحساب معطّل.")

    snap = db.collection('users').document(uid).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    role = data.get('role')
    allowed = role == 'finance' or (role == 'admin' and data.get('canRecharge') is True)
    if not allowed:
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية على طلبات الشحن.")
    return {"uid": uid, "role": role, "name": data.get('name', '')}


def _notify_recharge_result(student_id: str, approved: bool, points: int = 0, reason: str = ""):
    """إشعار للطالب بنتيجة طلب الشحن (بلغته)"""
    try:
        student_ref = db.collection('users').document(student_id)
        lang = (student_ref.get().to_dict() or {}).get('preferredLanguage', 'ar')
        if approved:
            if lang == 'en':
                title, body = 'Balance topped up ✅', f'{points} points were added to your wallet after reviewing your transfer receipt.'
            else:
                title, body = 'تم شحن رصيدك ✅', f'تمت إضافة {points} نقطة إلى محفظتك بعد مراجعة إيصال التحويل.'
        else:
            if lang == 'en':
                title = 'Top-up request not accepted'
                body = 'Your top-up request was not accepted.' + (f' Reason: {reason}' if reason else ' Please contact support.')
            else:
                title = 'تعذر قبول طلب الشحن'
                body = 'لم يتم قبول طلب الشحن.' + (f' السبب: {reason}' if reason else ' يرجى التواصل مع الدعم الفني.')
        student_ref.collection('notifications').add({
            'title': title,
            'body': body,
            'read': False,
            'createdAt': firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        print(f"Failed to send recharge notification: {e}")


@firestore.transactional
def _approve_recharge_txn(transaction, req_ref, staff_uid):
    """قبول ذري: ما بيتكرر، وبيضيف النقاط بنفس العملية"""
    req_snap = req_ref.get(transaction=transaction)
    if not req_snap.exists:
        raise HTTPException(status_code=404, detail="الطلب غير موجود.")
    req = req_snap.to_dict() or {}
    if req.get('status') != 'pending':
        raise HTTPException(status_code=400, detail="تمت معالجة هذا الطلب مسبقاً.")

    points = req.get('pointsCount')
    if not isinstance(points, (int, float)) or int(points) not in VALID_RECHARGE_POINTS:
        raise HTTPException(status_code=400, detail="عدد النقاط بالطلب غير صالح. ارفض الطلب وتواصل مع الطالب.")
    points = int(points)

    student_id = req.get('studentId')
    if not student_id:
        raise HTTPException(status_code=400, detail="الطلب بدون طالب.")
    student_ref = db.collection('users').document(student_id)
    student_snap = student_ref.get(transaction=transaction)
    if not student_snap.exists:
        raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")

    current = student_snap.to_dict().get('points', 0) or 0
    transaction.update(student_ref, {'points': current + points})
    transaction.update(req_ref, {
        'status': 'approved',
        'approvedAt': firestore.SERVER_TIMESTAMP,
        'approvedBy': staff_uid,
    })
    return student_id, points


@firestore.transactional
def _reject_recharge_txn(transaction, req_ref, staff_uid, reason):
    req_snap = req_ref.get(transaction=transaction)
    if not req_snap.exists:
        raise HTTPException(status_code=404, detail="الطلب غير موجود.")
    req = req_snap.to_dict() or {}
    if req.get('status') != 'pending':
        raise HTTPException(status_code=400, detail="تمت معالجة هذا الطلب مسبقاً.")
    transaction.update(req_ref, {
        'status': 'rejected',
        'rejectedAt': firestore.SERVER_TIMESTAMP,
        'rejectedBy': staff_uid,
        'rejectReason': reason,
    })
    return req.get('studentId')


@router.post("/api/admin/recharge/{requestId}/approve")
async def approve_recharge(requestId: str, staff: dict = Depends(get_current_staff)):
    try:
        req_ref = db.collection('recharge_requests').document(requestId)
        student_id, points = _approve_recharge_txn(db.transaction(), req_ref, staff['uid'])
        _notify_recharge_result(student_id, True, points=points)
        return {"message": "تم قبول الطلب وإضافة النقاط", "points": points}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in approve_recharge: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")


@router.post("/api/admin/recharge/{requestId}/reject")
async def reject_recharge(requestId: str, payload: RechargeRejectRequest, staff: dict = Depends(get_current_staff)):
    try:
        reason = (payload.reason or '').strip()[:300]
        req_ref = db.collection('recharge_requests').document(requestId)
        student_id = _reject_recharge_txn(db.transaction(), req_ref, staff['uid'], reason)
        if student_id:
            _notify_recharge_result(student_id, False, reason=reason)
        return {"message": "تم رفض الطلب"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in reject_recharge: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")
    

    # ==========================================
# 👑 إدارة المعلمين (للأدمن فقط): قبول التسجيل، توثيق، إعادة المقابلة، حذف
# ==========================================
async def get_current_admin(uid: str = Depends(get_current_user)):
    """الأدمن فقط (مش المسؤول المالي)"""
    try:
        fb_user = auth.get_user(uid)
    except Exception:
        raise HTTPException(status_code=401, detail="يجب تسجيل الدخول.")
    if fb_user.disabled:
        raise HTTPException(status_code=403, detail="هذا الحساب معطّل.")

    claims = fb_user.custom_claims or {}
    snap = db.collection('users').document(uid).get()
    doc_role = (snap.to_dict() or {}).get('role') if snap.exists else None
    if not (claims.get('admin') is True or claims.get('role') == 'admin' or doc_role == 'admin'):
        raise HTTPException(status_code=403, detail="هذا الإجراء للإدارة فقط.")
    return {"uid": uid}


def _teacher_or_404(teacher_id: str):
    ref = db.collection('users').document(teacher_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="حساب المعلم غير موجود.")
    data = snap.to_dict() or {}
    if data.get('role') != 'teacher':
        raise HTTPException(status_code=400, detail="هذا الحساب ليس لمعلم.")
    return ref, data


def _cloudinary_destroy(url: str) -> bool:
    """حذف ملف من Cloudinary من رابطه (محاولة فقط، ما بتوقف العملية لو فشلت)"""
    try:
        import cloudinary.uploader
        m = re.search(r'/(image|video|raw)/upload/(?:v\d+/)?(.+)$', url or '')
        if not m:
            return False
        rtype, public_id = m.group(1), unquote(m.group(2))
        if rtype != 'raw':
            public_id = re.sub(r'\.[A-Za-z0-9]+$', '', public_id)
        res = cloudinary.uploader.destroy(public_id, resource_type=rtype, invalidate=True)
        return res.get('result') == 'ok'
    except Exception as e:
        print(f"Cloudinary destroy failed for {url}: {e}")
        return False


def _delete_doc_tree(ref):
    """حذف وثيقة مع كل مجموعاتها الفرعية"""
    try:
        db.recursive_delete(ref)
        return
    except Exception as e:
        print(f"recursive_delete failed, falling back: {e}")
    for sub in ('strokes', 'messages', 'animations', 'rewards', 'live', 'notifications'):
        try:
            for d in ref.collection(sub).stream():
                d.reference.delete()
        except Exception as e:
            print(f"Failed deleting subcollection {sub}: {e}")
    ref.delete()


@router.post("/api/admin/teacher/{teacherId}/approve-registration")
async def admin_approve_registration(teacherId: str, admin: dict = Depends(get_current_admin)):
    """قبول تسجيل معلم جديد: بيصير 'معلم تجريبي' وبيعمل المقابلة"""
    try:
        ref, t = _teacher_or_404(teacherId)
        if t.get('status') != 'pending_approval':
            raise HTTPException(status_code=400, detail="حالة المعلم الحالية لا تسمح بقبول التسجيل.")
        ref.update({
            'status': 'trial',
            'approvedAt': firestore.SERVER_TIMESTAMP,
            'approvedBy': admin['uid'],
        })
        return {"message": "تم قبول التسجيل، المعلم الآن معلم تجريبي"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in admin_approve_registration: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")


@router.post("/api/admin/teacher/{teacherId}/certify")
async def admin_certify_teacher(teacherId: str, admin: dict = Depends(get_current_admin)):
    """توثيق المعلم بعد مراجعة المقابلة: بيصير 'معلم معتمد' وبتفتح له كل الخيارات"""
    try:
        ref, t = _teacher_or_404(teacherId)
        if t.get('status') != 'interviewed':
            raise HTTPException(status_code=400, detail="يمكن توثيق المعلم فقط بعد إتمامه للمقابلة.")
        ref.update({
            'status': 'approved',
            'accountStatus': 'approved',
            'isAvailable': True,
            'isApproved': True,
            'certifiedAt': firestore.SERVER_TIMESTAMP,
            'certifiedBy': admin['uid'],
        })
        return {"message": "تم توثيق المعلم، الآن هو معلم معتمد لدى منصة لبيب"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in admin_certify_teacher: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")


@router.post("/api/admin/teacher/{teacherId}/reset-interview")
async def admin_reset_interview(teacherId: str, admin: dict = Depends(get_current_admin)):
    """إعادة المقابلة: بيمسح الفيديو وبيرجع المعلم 'تجريبي' ليعيدها"""
    try:
        ref, t = _teacher_or_404(teacherId)
        if t.get('status') != 'interviewed':
            raise HTTPException(status_code=400, detail="إعادة المقابلة متاحة فقط للمعلم الذي أنهى مقابلته.")

        video_url = t.get('interviewVideoUrl')
        session_id = t.get('interviewSessionId')

        ref.update({
            'status': 'trial',
            'interviewVideoUrl': firestore.DELETE_FIELD,
            'interviewCompletedAt': firestore.DELETE_FIELD,
            'interviewSessionId': firestore.DELETE_FIELD,
            'interviewResetAt': firestore.SERVER_TIMESTAMP,
            'interviewResetBy': admin['uid'],
        })

        if video_url:
            _cloudinary_destroy(video_url)
        if session_id:
            _delete_doc_tree(db.collection('sessions').document(session_id))
        return {"message": "تمت إعادة المقابلة، المعلم الآن يستطيع إعادتها"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in admin_reset_interview: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")


@router.post("/api/admin/teacher/{teacherId}/delete")
async def admin_delete_teacher(teacherId: str, admin: dict = Depends(get_current_admin)):
    """حذف المعلم نهائياً (كأنه ما سجّل): حساب الدخول، البيانات، المقابلة، والملفات"""
    try:
        ref, t = _teacher_or_404(teacherId)
        if t.get('status') not in ('pending_approval', 'trial', 'interviewed'):
            raise HTTPException(status_code=400, detail="لا يمكن حذف معلم معتمد من هنا.")

        # كل روابط الملفات المرفوعة
        urls = []
        for k in ('certificateUrl', 'identityUrl', 'experienceCertificateUrl',
                  'advancedDegreeCertificateUrl', 'interviewVideoUrl', 'photoURL'):
            if t.get(k):
                urls.append(t[k])
        urls.extend(t.get('certificates') or [])
        urls = list(dict.fromkeys(u for u in urls if isinstance(u, str) and 'res.cloudinary.com' in u))

        # 1) حساب الدخول أول (عشان ما يقدر يدخل)
        try:
            auth.delete_user(teacherId)
        except auth.UserNotFoundError:
            pass

        # 2) جلسات المقابلة
        old = db.collection('sessions') \
            .where('teacherId', '==', teacherId) \
            .where('isTrialInterview', '==', True) \
            .stream()
        for s in old:
            _delete_doc_tree(s.reference)

        # 3) وثيقة المعلم
        _delete_doc_tree(ref)

        # 4) ملفات Cloudinary
        deleted_files = sum(1 for u in urls if _cloudinary_destroy(u))

        return {"message": "تم حذف المعلم نهائياً", "deletedFiles": deleted_files, "totalFiles": len(urls)}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in admin_delete_teacher: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")
    
# ==========================================
# 🔑 استرجاع كلمة مرور المعلم عبر SMS
# ==========================================
class ResetPrecheckRequest(BaseModel):
    email: str
    phone: str


class ResetPasswordRequest(BaseModel):
    email: str
    phoneIdToken: str
    newPassword: str


# عدّاد محاولات فاشلة بالذاكرة (بيتصفّر لما السيرفر يعيد التشغيل): 5 محاولات كل 15 دقيقة لكل إيميل
_reset_failures = {}
RESET_MAX_FAILURES = 5
RESET_WINDOW_SECONDS = 900


def _reset_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _reset_blocked(email: str) -> bool:
    key = email.strip().lower()
    recent = [t for t in _reset_failures.get(key, []) if _reset_now() - t < RESET_WINDOW_SECONDS]
    _reset_failures[key] = recent
    return len(recent) >= RESET_MAX_FAILURES


def _reset_register_failure(email: str):
    key = email.strip().lower()
    _reset_failures.setdefault(key, []).append(_reset_now())


def _norm_phone(p) -> str:
    return re.sub(r'[\s\-()]', '', (p or '').strip())


def _find_teacher_by_email(email: str):
    """يرجع (uid, بيانات الحساب) لو الإيميل لمعلم، وإلا None"""
    try:
        user_record = auth.get_user_by_email(email.strip().lower())
    except (auth.UserNotFoundError, ValueError):
        return None
    snap = db.collection('users').document(user_record.uid).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    if data.get('role') != 'teacher':
        return None
    return user_record.uid, data


@router.post("/api/teacher/reset-precheck")
async def reset_precheck(payload: ResetPrecheckRequest):
    """بيتأكد إنه الإيميل والرقم لنفس حساب المعلم، قبل ما نبعت أي رسالة SMS (بيوفر تكلفة الرسائل)"""
    if _reset_blocked(payload.email):
        raise HTTPException(status_code=429, detail="محاولات كثيرة. حاول بعد 15 دقيقة.")
    try:
        found = _find_teacher_by_email(payload.email)
        ok = False
        if found:
            stored = _norm_phone(found[1].get('phone'))
            ok = bool(stored) and stored == _norm_phone(payload.phone)
        if not ok:
            _reset_register_failure(payload.email)
        return {"ok": ok}
    except Exception as e:
        print(f"❌ ERROR in reset_precheck: {e}")
        return {"ok": False}


@router.post("/api/teacher/reset-password")
async def reset_teacher_password(payload: ResetPasswordRequest):
    """تغيير كلمة المرور بعد التحقق من رمز SMS (توكن Firebase للهاتف)"""
    def fail():
        _reset_register_failure(payload.email)
        return HTTPException(status_code=400, detail="تعذر تغيير كلمة المرور. تأكد من البيانات وأعد المحاولة.")

    if _reset_blocked(payload.email):
        raise HTTPException(status_code=429, detail="محاولات كثيرة. حاول بعد 15 دقيقة.")

    if not re.match(r'^(?=.*[0-9])(?=.*[a-zA-Z]).{8,}$', payload.newPassword):
        raise HTTPException(status_code=400, detail="كلمة المرور يجب أن تتكون من 8 خانات على الأقل وتحتوي على أحرف وأرقام.")

    try:
        # 1) التوكن لازم يكون حقيقي، من تسجيل دخول بالهاتف، وحديث (10 دقائق)
        try:
            decoded = auth.verify_id_token(payload.phoneIdToken)
        except Exception:
            raise fail()
        if decoded.get('firebase', {}).get('sign_in_provider') != 'phone':
            raise fail()
        verified_phone = decoded.get('phone_number')
        if not verified_phone or (_reset_now() - decoded.get('auth_time', 0)) > 600:
            raise fail()

        # 2) الحساب لازم يكون معلم، ورقمه المسجل هو نفسه الرقم اللي تم التحقق منه
        found = _find_teacher_by_email(payload.email)
        if not found:
            raise fail()
        teacher_uid, teacher_data = found
        if _norm_phone(teacher_data.get('phone')) != _norm_phone(verified_phone):
            raise fail()

        # 3) تغيير كلمة المرور وإنهاء كل الجلسات القديمة
        auth.update_user(teacher_uid, password=payload.newPassword)
        auth.revoke_refresh_tokens(teacher_uid)

        # 4) سجل تدقيق (ما حدا بيقراه من المتصفح، بس من لوحة Firebase أو الأدمن)
        db.collection('password_resets').add({
            'teacherId': teacher_uid,
            'teacherName': teacher_data.get('name', ''),
            'email': payload.email.strip().lower(),
            'method': 'sms',
            'createdAt': firestore.SERVER_TIMESTAMP
        })

        _reset_failures.pop(payload.email.strip().lower(), None)
        return {"message": "تم تغيير كلمة المرور بنجاح"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in reset_teacher_password: {e}")
        raise HTTPException(status_code=500, detail="خطأ داخلي في السيرفر.")

# ==========================================
# ✏️ طلب تعديل بيانات المعلم (يحتاج موافقة الإدارة)
# ==========================================
class ProfileUpdateRequest(BaseModel):
    name: str
    country: str
    city: str
    subjects: List[str]
    targetAge: List[str]
    teachingLanguage: str
    ownedDevices: List[str]
    academicDegree: str
    certificateUrl: Optional[str] = None
    advancedDegreeCertificateUrl: Optional[str] = None
    identityUrl: Optional[str] = None
    experienceCertificateUrl: Optional[str] = None


@router.post("/api/teacher/request-profile-update")
async def request_profile_update(payload: ProfileUpdateRequest, uid: str = Depends(get_current_user)):
    try:
        user_ref = db.collection('users').document(uid)
        user_snap = user_ref.get()

        if not user_snap.exists:
            raise HTTPException(status_code=404, detail="الحساب غير موجود.")

        user_data = user_snap.to_dict()
        if user_data.get('role') != 'teacher':
            raise HTTPException(status_code=403, detail="هذا الإجراء مخصص للمعلمين فقط.")
        if user_data.get('status') != 'approved':
            raise HTTPException(status_code=400, detail="لا يمكن تقديم تعديلات إلا بعد اعتماد الحساب.")
        if user_data.get('pendingProfileUpdates'):
            raise HTTPException(status_code=400, detail="لديك تعديلات قيد المراجعة بالفعل.")

        # 🔒 التحقق من القيم
        if payload.teachingLanguage not in ('العربية', 'الإنجليزية (انترناشونال)', 'كلاهما'):
            raise HTTPException(status_code=400, detail="لغة التدريس غير صالحة.")
        if payload.academicDegree not in ('لا أملك', 'ماجستير', 'دكتوراه'):
            raise HTTPException(status_code=400, detail="الدرجة العلمية غير صالحة.")
        if not payload.name.strip() or not payload.subjects or not payload.targetAge or not payload.ownedDevices:
            raise HTTPException(status_code=400, detail="بيانات ناقصة.")
        if len(payload.name) > 120 or len(payload.subjects) > 30 or len(payload.targetAge) > 30:
            raise HTTPException(status_code=400, detail="بيانات غير صالحة.")

        updates = payload.dict(exclude_none=True)
        updates['name'] = payload.name.strip()

        # روابط الملفات لازم تكون من Cloudinary فقط
        for key in ('certificateUrl', 'advancedDegreeCertificateUrl', 'identityUrl', 'experienceCertificateUrl'):
            url = updates.get(key)
            if url and not url.startswith('https://res.cloudinary.com/'):
                raise HTTPException(status_code=400, detail="رابط ملف غير صالح.")

        user_ref.update({
            'pendingProfileUpdates': updates,
            'status': 'pending_profile_update',
            'profileUpdateRequestedAt': firestore.SERVER_TIMESTAMP
        })

        return {"message": "تم إرسال التعديلات للإدارة"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ ERROR in request_profile_update: {e}")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {e}")
    
# ==========================================
# 💳 بوابة الدفع (Tap Payments)
# ==========================================

@router.post("/api/payment/create-checkout")
async def create_tap_checkout(
    amount: float = Form(0),  # يُتجاهل: السعر الفعلي بيتحسب من جدول PRICING_PACKAGES
    pointsCount: int = Form(...),
    sourceId: str = Form("src_all"),
    customPhone: str = Form(""),  # ✨ استقبال رقم الهاتف المخصص للدفع
    uid: str = Depends(get_current_user)
):
    """إنشاء رابط دفع آمن عبر بوابة Tap Payments"""
    try:
        # جلب المفتاح السري من متغيرات البيئة
        secret_key = os.getenv("TAP_SECRET_KEY")
        base_url = os.getenv("TAP_API_BASE_URL", "https://api.tap.company/v2")
        
        if not secret_key:
            raise HTTPException(status_code=500, detail="لم يتم إعداد مفاتيح بوابة الدفع بشكل صحيح.")

        # جلب اسم الطالب ورقم هاتفه لعرضهما في صفحة الدفع
        user_ref = db.collection('users').document(uid)
        user_snap = user_ref.get()

        if not user_snap.exists:
            raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")

        # 🔒 السعر بيتحدد هون حسب لغة الطالب وعدد النقاط، مش حسب اللي بعته المتصفح
        student_lang = user_snap.to_dict().get('preferredLanguage', 'ar')
        price_table = PRICING_PACKAGES.get(student_lang, PRICING_PACKAGES['ar'])
        if pointsCount not in price_table:
            raise HTTPException(status_code=400, detail="الباقة المطلوبة غير صالحة.")
        amount = float(price_table[pointsCount])

        student_name = "طالب لبيب"
        student_phone = "00000000"
        if user_snap.exists:
            user_data = user_snap.to_dict()
            student_name = user_data.get('name', student_name)
            # ✨ جلب رقم الهاتف وإزالة مقدمة الدولة بشكل آمن
            raw_phone = user_data.get('phone', '00000000')
            for code in ['+974', '+966', '+971', '+965', '+962', '+970', '+20']:
                if raw_phone.startswith(code):
                    raw_phone = raw_phone[len(code):]
                    break
            student_phone = raw_phone

        # ✨ استخدام الرقم المخصص للدفع وتنظيفه إذا أدخله الطالب
        if customPhone:
            for code in ['+974', '+966', '+971', '+965', '+962', '+970', '+20']:
                if customPhone.startswith(code):
                    customPhone = customPhone[len(code):]
                    break
            student_phone = customPhone

        # تجهيز بيانات الدفع لإرسالها لـ Tap
        payload = {
            "amount": amount,
                   "currency": "QAR",  # ✨ تغيير العملة للريال القطري لتتوافق مع NAPS وقطر
            "threeDSecure": True,
            "save_card_later": False,
            "description": f"شحن محفظة منصة لبيب ({pointsCount} نقطة)",
            "statement_descriptor": "Labeeb Platform",
            "metadata": {
                "studentId": uid,
                "studentName": student_name,
                "pointsCount": pointsCount
            },
            "reference": {
                "transaction": str(uuid.uuid4()),
                "order": str(uuid.uuid4())
            },
            "receipt": {
                "email": False,
                "sms": True
            },
            "customer": {
                "first_name": student_name,
                "middle_name": "",
                "last_name": "",
                "email": "",
                "phone": {
                    "country_code": "+974",
                    "number": student_phone  # ✨ استخدام رقم الطالب الحقيقي
                }
            },
            "source": {
                "id": sourceId  # ✨ استخدام طريقة الدفع التي اختارها الطالب
            },
            "post": {
                "url": f"{PUBLIC_BASE_URL}/api/payment/webhook"
            },
            "redirect": {
                "url": f"{PUBLIC_BASE_URL}/payment-success"
            }
        }

        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json"
        }

               # إرسال الطلب إلى سيرفر Tap (بشكل غير متزامن Async)
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{base_url}/charges", json=payload, headers=headers)
            
            if response.status_code in [200, 201]:
                data = response.json()
                checkout_url = data.get("transaction", {}).get("url")
                if checkout_url:
                    return {"checkoutUrl": checkout_url}
                else:
                    raise HTTPException(status_code=500, detail="تعذر الحصول على رابط الدفع من البوابة.")
            else:
                print("Tap Error Response:", response.text)
                # ✨ استخراج رسالة الخطأ الحقيقية من Tap لعرضها للطالب
                error_detail = "فشل إنشاء طلب الدفع."
                try:
                    error_json = response.json()
                    if "errors" in error_json and len(error_json["errors"]) > 0:
                        error_detail = error_json["errors"][0].get("description", error_detail)
                except:
                    pass
                raise HTTPException(status_code=400, detail=f"فشل الدفع: {error_detail}")
            
    except HTTPException:
        raise # إعادة رمي أخطاء HTTP المخصصة
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in create_tap_checkout: {error_str}")
        if "429" in error_str or "Quota" in error_str:
            raise HTTPException(status_code=429, detail="تم تجاوز الحد المجاني لعمليات قاعدة البيانات اليومية. يرجى المحاولة غداً.")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")

@firestore.transactional
def _apply_topup(transaction, charge_ref, user_ref, points_count, record):
    """شحن الرصيد وتسجيل العملية بشكل ذري. لو العملية مسجلة قبل، ما بيشحن مرة ثانية."""
    charge_snap = charge_ref.get(transaction=transaction)
    if charge_snap.exists:
        return 'duplicate'
    user_snap = user_ref.get(transaction=transaction)
    if not user_snap.exists:
        return 'no_student'
    current_points = user_snap.to_dict().get('points', 0)
    transaction.update(user_ref, {'points': current_points + points_count})
    transaction.set(charge_ref, record)
    return 'ok'

@router.post("/api/payment/webhook")
async def tap_webhook(request: Request):
    """استقبال إشعار الدفع من Tap وتحديث رصيد الطالب تلقائياً مع التحقق الأمني"""
    try:
        # استقبال البيانات القادمة من سيرفر Tap
        payload = await request.json()
        print("📥 Tap Webhook Received:", payload)

        # 1. استخراج رقم العملية (Charge ID)
        charge_id = payload.get("id")
        if not charge_id:
            print("❌ Webhook Error: Missing charge ID.")
            return {"status": "error", "message": "Missing charge ID"}

              # 2. التحقق الأمني (Verification): سؤال Tap إذا كانت العملية دي حقيقية ومكتملة
        secret_key = os.getenv("TAP_SECRET_KEY")
        base_url = os.getenv("TAP_API_BASE_URL", "https://api.tap.company/v2")
        headers = {"Authorization": f"Bearer {secret_key}"}
        
        verify_url = f"{base_url}/charges/{charge_id}"
        
        # استخدام httpx Async للتحقق
        async with httpx.AsyncClient() as client:
            verify_response = await client.get(verify_url, headers=headers)
            verified_data = verify_response.json()

        # 3. التأكد أن Tap أكدت العملية بنجاح (CAPTURED)
        if verify_response.status_code == 200 and verified_data.get("status") == "CAPTURED":
            metadata = verified_data.get("metadata", {})
            student_id = metadata.get("studentId")
            points_count_str = metadata.get("pointsCount", "0")

            # التأكد من صحة البيانات قبل شحن المحفظة
            points_count_str = str(points_count_str)
            if student_id and points_count_str.isdigit() and int(points_count_str) > 0:
                points_count = int(points_count_str)

                user_ref = db.collection('users').document(student_id)
                # رقم العملية من Tap هو اسم الوثيقة => نفس العملية ما بتنسجل مرتين
                charge_ref = db.collection('recharge_requests').document(charge_id)
                record = {
                    'studentId': student_id,
                    'studentName': metadata.get("studentName", "طالب"),
                    'packageTitle': f"دفع إلكتروني ({points_count} نقطة)",
                    'pointsCount': points_count,
                    'referenceNumber': charge_id,
                    'receiptImageUrl': '',
                    'status': 'approved',
                    'createdAt': firestore.SERVER_TIMESTAMP,
                    'approvedAt': firestore.SERVER_TIMESTAMP,
                    'paymentMethod': 'Tap Gateway'
                }

                result = _apply_topup(db.transaction(), charge_ref, user_ref, points_count, record)
                if result == 'ok':
                    print(f"✅ Wallet topped up for {student_id}: +{points_count} points (charge {charge_id})")
                elif result == 'duplicate':
                    print(f"ℹ️ Webhook ignored: charge {charge_id} was already processed.")
                else:
                    print("❌ Webhook Error: Student not found.")
            else:
                print("❌ Webhook Error: Missing or invalid metadata.")
        else:
            print(f"ℹ️ Payment not captured or verification failed: {verified_data.get('status')}")

        # يجب دائماً الرد بـ 200 OK لسيرفر Tap لكي لا يعيد إرسال الطلب
        return {"status": "success"}

    except Exception as e:
        print(f"❌ Webhook Exception: {str(e)}")
        # نرجع 200 حتى لو في خطأ عشان Tap ما يعملش Spam على السيرفر بتاعنا، بس نسجل الخطأ
        return {"status": "error", "message": str(e)}


# ==========================================
# ☁️ رفع الملفات إلى Cloudinary وإنهاء الجلسة
# ==========================================

@router.post("/api/cloudinary/upload")
async def upload_to_cloudinary(
    file: UploadFile = File(...), 
    folder: str = Form(""), 
    resource_type: str = Form("auto"),
    uid: str = Depends(get_current_user)
):
    """رفع ملف عام إلى Cloudinary باستخدام المفاتيح الآمنة (Signed)"""
    try:
        file_contents = await file.read()
        
        upload_result = cloudinary.uploader.upload(
            file_contents,
            resource_type=resource_type,
            folder=folder if folder else None
        )
        return {"url": upload_result.get("secure_url")}
            
    except Exception as e:
        print("Upload Exception:", str(e))
        raise HTTPException(status_code=500, detail=f"Cloudinary upload exception: {str(e)}")



class SessionCompleteRequest(BaseModel):
    audioUrl: Optional[str] = None


@router.post("/api/session/complete/{sessionId}")
async def complete_session(
    sessionId: str,
    payload: SessionCompleteRequest,
    uid: str = Depends(get_current_user)
):
    """
    إنهاء الجلسة وحفظ روابط الملفات (التي تم رفعها مباشرة من المتصفح لـ Cloudinary).
    لا يستقبل ملفات ثقيلة إطلاقاً - فقط روابط نصية - لضمان سرعة الاستجابة.
    """
    session_ref = db.collection('sessions').document(sessionId)
    session_snap = session_ref.get()

    if not session_snap.exists:
        raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

    session_data = session_snap.to_dict()
    if session_data.get('studentId') != uid and session_data.get('teacherId') != uid:
        raise HTTPException(status_code=403, detail="غير مصرح لك بإنهاء هذه الجلسة.")

    update_data = {
        'status': 'completed',
        'completedAt': firestore.SERVER_TIMESTAMP
    }
    if payload.audioUrl:
        update_data['audioRecordingUrl'] = payload.audioUrl

    session_ref.update(update_data)
    print("Session completed successfully in Firestore!")

    # ✨ تحديث حالة الحصة التجريبية للطالب لمنع تكرارها
    if session_data.get('isFreeTrial'):
        student_id_for_trial = session_data.get('studentId')
        if student_id_for_trial:
            db.collection('users').document(student_id_for_trial).update({
                'hasUsedFreeTrial': True
            })

    # ✨ إرسال إشعار للطالب بتوفر ملخص الحصة
    student_id = session_data.get('studentId')
    if student_id:
        try:
            db.collection('users').document(student_id).collection('notifications').add({
                'title': 'انتهت الحصة ✅',
                'body': f'يمكنك الآن مراجعة ملخص السبورة لحصة {session_data.get("subject", "")}.',
                'read': False,
                'createdAt': firestore.SERVER_TIMESTAMP
            })
        except Exception as e:
            print(f"Failed to send notification: {e}")

    return {"message": "تم إنهاء الجلسة بنجاح", "audioUrl": payload.audioUrl}