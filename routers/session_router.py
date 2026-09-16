from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import firebase_admin
from firebase_admin import credentials, firestore, auth
import cloudinary
import cloudinary.uploader
import os
import requests
import uuid
from dotenv import load_dotenv
from typing import Optional
import json 
from datetime import datetime, timezone
import httpx
import asyncio

load_dotenv()

router = APIRouter()
security = HTTPBearer()

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
    uid: str = Depends(get_current_user)
):
    try:
        user_ref = db.collection('users').document(uid)
        user_snap = user_ref.get()

        if not user_snap.exists:
            raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")

        user_data = user_snap.to_dict()
        raw_points = user_data.get('points', 0)
        current_points = raw_points if isinstance(raw_points, (int, float)) else 0

        if current_points <= 0:
            raise HTTPException(status_code=400, detail="لا يوجد لديك نقاط كافية.")

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

        return {"message": "تم تسجيل الدخول"}

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
        intervals_already_done = int(round(already_deducted / 0.5))

        intervals_to_deduct = intervals_due - intervals_already_done

        print(f"🕐 [{log_ts}] session={sessionId} elapsed={elapsed_seconds:.0f}s "
              f"intervals_due={intervals_due} intervals_already_done={intervals_already_done} "
              f"already_deducted={already_deducted} intervals_to_deduct={intervals_to_deduct}")

        if intervals_to_deduct <= 0:
            print(f"🕐 [{log_ts}] session={sessionId} → لا يوجد خصم مستحق الآن")
            return {"message": "لا يوجد خصم مستحق الآن", "pointsDeducted": already_deducted}

        points_to_deduct = intervals_to_deduct * 0.5
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
        # 🛡️ فلترة ذكية (Smart Routing Security): التأكد من أن المعلم يدرس هذه المادة والمرحلة
        user_ref = db.collection('users').document(uid)
        user_snap = user_ref.get()
        if user_snap.exists:
            teacher_data = user_snap.to_dict()
            teacher_subjects = teacher_data.get('subjects', [])
            teacher_ages = teacher_data.get('targetAge', [])
            
            session_subject = session_data.get('subject')
            session_grade = session_data.get('grade')
            
            if session_subject and session_subject not in teacher_subjects:
                raise HTTPException(status_code=403, detail="لا يمكنك قبول هذا الطلب لأنه لا يطابق تخصصاتك.")
            
            if session_grade and session_grade not in teacher_ages:
                raise HTTPException(status_code=403, detail="لا يمكنك قبول هذا الطلب لأنه لا يطابق المراحل الدراسية التي تدرسها.")
        
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
    certificateUrl: str = Form(...),
    certificateName: str = Form(...),
    uid: str = Depends(get_current_user)
):
    try:
        db.collection('users').document(uid).set({
            'subject': subject,
            'subjects': [s.strip() for s in subject.split('،')],
            'role': 'teacher',
            'experienceYears': experience,
            'targetAge': [a.strip() for a in targetAge.split('،')],
            'certificates': [certificateUrl],
            'certificateUrl': certificateUrl,
            'certificateName': certificateName,
            'status': 'pending_approval',
            'updatedAt': firestore.SERVER_TIMESTAMP
        }, merge=True)
        
        return {"message": "تم إرسال بياناتك وشهادتك للإدارة بنجاح!"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"حدث خطأ أثناء الحفظ: {str(e)}")


# ==========================================
# 💳 بوابة الدفع (Tap Payments)
# ==========================================

@router.post("/api/payment/create-checkout")
async def create_tap_checkout(
    amount: float = Form(...),
    pointsCount: int = Form(...),
    uid: str = Depends(get_current_user)
):
    """إنشاء رابط دفع آمن عبر بوابة Tap Payments"""
    try:
        # جلب المفتاح السري من متغيرات البيئة
        secret_key = os.getenv("TAP_SECRET_KEY")
        base_url = os.getenv("TAP_API_BASE_URL", "https://api.tap.company/v2")
        
        if not secret_key:
            raise HTTPException(status_code=500, detail="لم يتم إعداد مفاتيح بوابة الدفع بشكل صحيح.")

        # جلب اسم الطالب لعرضه في صفحة الدفع
        user_ref = db.collection('users').document(uid)
        user_snap = user_ref.get()
        student_name = "طالب لبيب"
        if user_snap.exists:
            student_name = user_snap.to_dict().get('name', student_name)

        # تجهيز بيانات الدفع لإرسالها لـ Tap
        payload = {
            "amount": amount,
                   "currency": "SAR",
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
                    "number": "00000000"
                }
            },
            "source": {
                "id": "src_all"
            },
            "redirect": {
                "url": f"https://labeeb-wep.onrender.com/payment-success" 
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
                raise HTTPException(status_code=400, detail="فشل إنشاء طلب الدفع. تأكد من البيانات.")
            
    except HTTPException:
        raise # إعادة رمي أخطاء HTTP المخصصة
    except Exception as e:
        error_str = str(e)
        print(f"❌ ERROR in create_tap_checkout: {error_str}")
        if "429" in error_str or "Quota" in error_str:
            raise HTTPException(status_code=429, detail="تم تجاوز الحد المجاني لعمليات قاعدة البيانات اليومية. يرجى المحاولة غداً.")
        raise HTTPException(status_code=500, detail=f"خطأ داخلي في السيرفر: {error_str}")


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
            if student_id and points_count_str.isdigit():
                points_count = int(points_count_str)
                if points_count > 0:
                    # شحن رصيد الطالب في Firestore
                    user_ref = db.collection('users').document(student_id)
                    user_snap = user_ref.get()

                    if user_snap.exists:
                        user_data = user_snap.to_dict()
                        current_points = user_data.get('points', 0)
                        
                        # زيادة الرصيد
                        user_ref.update({
                            'points': current_points + points_count
                        })
                        print(f"✅ Wallet topped up for {student_id}: +{points_count} points")

                        # حفظ سجل الدفع في قاعدة البيانات للأرشيف
                        db.collection('recharge_requests').add({
                            'studentId': student_id,
                            'studentName': metadata.get("studentName", "طالب"),
                            'packageTitle': f"دفع إلكتروني ({points_count} نقطة)",
                            'pointsCount': points_count,
                            'referenceNumber': charge_id, # استخدام الـ ID الحقيقي
                            'receiptImageUrl': '',
                            'status': 'approved',
                            'createdAt': firestore.SERVER_TIMESTAMP,
                            'approvedAt': firestore.SERVER_TIMESTAMP,
                            'paymentMethod': 'Tap Gateway'
                        })
                    else:
                        print("❌ Webhook Error: Student not found.")
                else:
                    print("❌ Webhook Error: Invalid points count.")
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


@router.post("/api/session/complete/{sessionId}")
async def complete_session(
    sessionId: str,
    audio: Optional[UploadFile] = File(None),
    pdf: Optional[UploadFile] = File(None),
    uid: str = Depends(get_current_user)
):
    """إنهاء الجلسة ورفع التسجيل الصوتي والسبورة PDF باستخدام المفاتيح الآمنة"""
    session_ref = db.collection('sessions').document(sessionId)
    session_snap = session_ref.get()

    if not session_snap.exists:
        raise HTTPException(status_code=404, detail="الجلسة غير موجودة")

    session_data = session_snap.to_dict()
    if session_data.get('studentId') != uid and session_data.get('teacherId') != uid:
        raise HTTPException(status_code=403, detail="غير مصرح لك بإنهاء هذه الجلسة.")

    audio_url = None
    pdf_url = None

    # ⚡ قراءة الملفين أولاً (سريعة)، ثم رفعهما لـ Cloudinary بالتوازي بدل التتابع
    # cloudinary.uploader.upload سينكرونية (blocking)، فبنشغلها بخيط منفصل عبر asyncio.to_thread
    # عشان الرفعتين يصيرن بنفس الوقت بدل ما توحدة تنتظر التانية

    audio_bytes = await audio.read() if audio else None
    pdf_bytes = await pdf.read() if pdf else None

    def upload_audio(data: bytes):
        print("Uploading Audio...")
        result = cloudinary.uploader.upload(
            data,
            resource_type="video",
            folder="session_audio",
            timeout=60  # ⏱️ بدون هذا، الرفع ممكن يعلق للأبد بدون أي خطأ لو تعثر الاتصال بـ Cloudinary
            # ✨ تم إزالة filename لأنها تسبب خطأ وانهيار للسيرفر
        )
        print("Audio uploaded successfully:", result.get("secure_url"))
        return result.get("secure_url")

    def upload_pdf(data: bytes):
        print("Uploading PDF...")
        result = cloudinary.uploader.upload(
            data,
            resource_type="auto",
            folder="session_pdfs",
            timeout=60  # ⏱️ نفس السبب: حماية من تعليق أبدي لو تعثر الاتصال
        )
        print("PDF uploaded successfully:", result.get("secure_url"))
        return result.get("secure_url")

    upload_tasks = []
    if audio_bytes:
        upload_tasks.append(asyncio.to_thread(upload_audio, audio_bytes))
    if pdf_bytes:
        upload_tasks.append(asyncio.to_thread(upload_pdf, pdf_bytes))

    if upload_tasks:
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)
        idx = 0
        if audio_bytes:
            r = results[idx]; idx += 1
            if isinstance(r, Exception):
                print(f"❌ Audio upload failed: {r}")
            else:
                audio_url = r
        if pdf_bytes:
            r = results[idx]; idx += 1
            if isinstance(r, Exception):
                print(f"❌ PDF upload failed: {r}")
            else:
                pdf_url = r

    # تحديث حالة الجلسة
    update_data = {
        'status': 'completed',
        'completedAt': firestore.SERVER_TIMESTAMP
    }
    if audio_url:
        update_data['audioRecordingUrl'] = audio_url
    if pdf_url:
        update_data['boardPdfUrl'] = pdf_url

    session_ref.update(update_data)
    print("Session completed successfully in Firestore!")

    # ✨ إرسال إشعار للطالب بتوفر ملف السبورة
    student_id = session_data.get('studentId')
    if student_id and pdf_url:
        try:
            db.collection('users').document(student_id).collection('notifications').add({
                'title': 'تمت إضافة ملخص حصة 📄',
                'body': f'تمت إضافة ملخص حصة {session_data.get("subject", "")} إلى حسابك جاهز للتحميل.',
                'read': False,
                'createdAt': firestore.SERVER_TIMESTAMP
            })
            print("Notification sent to student.")
        except Exception as e:
            print(f"Failed to send notification: {e}")

    return {"message": "تم إنهاء الجلسة بنجاح", "audioUrl": audio_url, "pdfUrl": pdf_url}