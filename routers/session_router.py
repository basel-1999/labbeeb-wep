from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import firebase_admin
from firebase_admin import credentials, firestore, auth
import cloudinary
import cloudinary.uploader
import os
from dotenv import load_dotenv
from typing import Optional
import json 

load_dotenv()

router = APIRouter()
security = HTTPBearer()

# إعداد Cloudinary
cloudinary.config(
    cloud_name="f4t8ayoq",
    api_key="325765956116463",
    api_secret="ZGiALPgBRexubi0I6VxKCyjM-Tg"
)

# تهيئة Firebase Admin (قراءة الملف المباشر فقط)
if not firebase_admin._apps:
    cred_path = "serviceAccountKey.json"
    if os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase initialized from local file.")
    else:
        print("⚠️ Warning: Firebase service account key not found!")

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
    user_ref = db.collection('users').document(uid)
    user_snap = user_ref.get()

    if not user_snap.exists:
        raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")

    user_data = user_snap.to_dict()
    raw_credits = user_data.get('sessionCredits', 0)
    current_credits = raw_credits if isinstance(raw_credits, int) else 0

    if current_credits <= 0:
        raise HTTPException(status_code=400, detail="رصيدك الحالي لا يكفي لإنشاء حصة جديدة. يرجى شحن المحفظة.")

    user_ref.update({'sessionCredits': current_credits - 1})

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
        'grade': grade,  # ✨ حفظ المرحلة الدراسية
        'status': 'pending',
        'createdAt': firestore.SERVER_TIMESTAMP,
        'audioRecordingUrl': None,
        'boardPdfUrl': None,
    })

    return {"sessionId": session_ref.id, "message": "تم إنشاء الطلب بنجاح"}

@router.post("/api/session/cancel/{sessionId}")
async def cancel_session_request(sessionId: str, uid: str = Depends(get_current_user)):
    session_ref = db.collection('sessions').document(sessionId)

    @firestore.transactional
    def cancel_transaction(transaction):
        session_snap = transaction.get(session_ref)
        if not session_snap.exists:
            return
        
        session_data = session_snap.to_dict()
        if session_data.get('status') == 'pending' and session_data.get('studentId') == uid:
            user_ref = db.collection('users').document(uid)
            user_snap = transaction.get(user_ref)
            
            if user_snap.exists:
                user_data = user_snap.to_dict()
                current_credits = user_data.get('sessionCredits', 0)
                transaction.update(user_ref, {'sessionCredits': current_credits + 1})
            
            transaction.update(session_ref, {
                'status': 'cancelled',
                'cancelledAt': firestore.SERVER_TIMESTAMP
            })

    transaction = db.transaction()
    cancel_transaction(transaction)
    return {"message": "تم إلغاء الطلب وإرجاع الرصيد"}

@router.post("/api/session/extend/{sessionId}")
async def extend_session_duration(sessionId: str, studentId: str = Form(...), uid: str = Depends(get_current_user)):
    if uid != studentId:
        raise HTTPException(status_code=403, detail="إجراء غير مصرح به: لا يمكنك تمديد جلسة لمستخدم آخر.")

    user_ref = db.collection('users').document(studentId)
    user_snap = user_ref.get()

    if not user_snap.exists:
        raise HTTPException(status_code=404, detail="حساب الطالب غير موجود.")

    user_data = user_snap.to_dict()
    raw_credits = user_data.get('sessionCredits', 0)
    current_credits = raw_credits if isinstance(raw_credits, int) else 0

    if current_credits <= 0:
        raise HTTPException(status_code=400, detail="رصيدك الحالي لا يكفي لتمديد الحصة. يرجى شحن المحفظة أولاً.")

    user_ref.update({'sessionCredits': current_credits - 1})

    db.collection('sessions').document(sessionId).update({
        'extensionsCount': firestore.Increment(1),
        'lastExtendedAt': firestore.SERVER_TIMESTAMP
    })

    return {"message": "تم تمديد الجلسة بنجاح"}

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
    
    if session_data and session_data.get('status') == 'pending':
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

    # رفع التسجيل الصوتي
    if audio:
        audio_bytes = await audio.read()
        if audio_bytes:
            print("Uploading Audio...")
            audio_upload = cloudinary.uploader.upload(
                audio_bytes,
                resource_type="video", 
                folder="session_audio",
                filename=audio.filename
            )
            audio_url = audio_upload.get("secure_url")
            print("Audio uploaded successfully:", audio_url)

    # رفع ملف PDF
    if pdf:
        pdf_bytes = await pdf.read()
        if pdf_bytes:
            print("Uploading PDF...")
            pdf_upload = cloudinary.uploader.upload(
                pdf_bytes,
                resource_type="raw", 
                folder="session_pdfs",
                filename=pdf.filename
            )
            pdf_url = pdf_upload.get("secure_url")
            print("PDF uploaded successfully:", pdf_url)

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