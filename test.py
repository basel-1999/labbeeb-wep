import os
# ✨ الإصلاح السحري: إجبار Firebase على استخدام REST API بدلاً من gRPC
os.environ["GOOGLE_CLOUD_DISABLE_GRPC"] = "True"

import json
import firebase_admin
from firebase_admin import credentials, firestore

print("1. Reading file...")
d = json.load(open('serviceAccountKey.json'))

print("2. Checking private key format...")
pk = d['private_key']
if "\\n" in pk:
    print("❌ Key is CORRUPTED (contains literal \\n)")
else:
    print("✅ Key format is clean.")

print("3. Connecting to Firestore (Using REST API)...")
try:
    cred = credentials.Certificate(d)
    app = firebase_admin.initialize_app(cred)
    db = firestore.client()
    db.collection('users').limit(1).get(timeout=10)
    print("✅ FIRESTORE CONNECTION SUCCESS!")
except Exception as e:
    print(f"❌ FAILED: {str(e)}")