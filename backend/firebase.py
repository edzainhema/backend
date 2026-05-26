"""
Firebase Admin initialization for server-side push (FCM).

Guarded like the Celery integration (see backend/__init__.py): a checkout that
hasn't pip-installed firebase-admin yet -- or a CI / test / local environment
without service-account credentials -- still boots Django. Push notifications
are simply unavailable until BOTH the SDK is installed AND
firebase/serviceAccount.json is present. When both are present this behaves
exactly as before.

`firebase_app` is exposed for callers/tests that want to check whether Firebase
actually initialized.
"""
import os

firebase_app = None

try:
    import firebase_admin
    from firebase_admin import credentials

    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _cred_path = os.path.join(BASE_DIR, "firebase", "serviceAccount.json")

    # The credentials file is git-ignored (it is a secret), so it is absent in
    # CI and fresh checkouts. Only initialize when it is actually there.
    if os.path.exists(_cred_path):
        cred = credentials.Certificate(_cred_path)
        firebase_app = firebase_admin.initialize_app(cred)
except ImportError:
    # firebase-admin not installed -- push is unavailable, Django still boots.
    firebase_app = None
