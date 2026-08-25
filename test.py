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

print("3. Connecting to Firestore (Timeout 5s)...")
try:
    cred = credentials.Certificate(d)
    app = firebase_admin.initialize_app(cred)
    db = firestore.client()
    db.collection('users').limit(1).get(timeout=5)
    print("✅ FIRESTORE CONNECTION SUCCESS!")
except Exception as e:
    print(f"❌ FAILED: {str(e)[:100]}")