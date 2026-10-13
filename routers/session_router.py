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
    if awarded >= STARS_PER_SESSION_LIMIT:
        raise HTTPException(status_code=429, detail=f"وصلت للحد الأقصى ({STARS_PER_SESSION_LIMIT} نجوم) لهذه الحصة.")

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


@router.post("/api/session/award-star/{sessionId}")
async def award_star(sessionId: str, uid: str = Depends(get_current_user)):
    try:
        session_ref = db.collection('sessions').document(sessionId)
        session_snap = session_ref.get()
        if not session_snap.exists:
            raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

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

        # إشعار للطالب عند الكأس
        if milestone == 'cup':
            try:
                lang = (student_ref.get().to_dict() or {}).get('preferredLanguage', 'ar')
                if lang == 'en':
                    title, body = 'Congratulations! You earned a cup 🏆', 'You completed 10 stars. Keep shining!'
                else:
                    title, body = 'مبروك! حصلت على كأس 🏆', 'أكملت 10 نجوم، واصل تألقك!'
                student_ref.collection('notifications').add({
                    'title': title,
                    'body': body,
                    'read': False,
                    'createdAt': firestore.SERVER_TIMESTAMP
                })
            except Exception as e:
                print(f"Failed to send cup notification: {e}")

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
                "url": "https://labeeb-wep.onrender.com/api/payment/webhook"
            },
            "redirect": {
                "url": "https://labeeb-wep.onrender.com/payment-success"
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