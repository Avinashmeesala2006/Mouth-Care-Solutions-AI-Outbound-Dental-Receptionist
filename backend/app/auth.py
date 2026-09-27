import base64
import hashlib
import hmac
import os


def hash_password(password: str) -> str:
 salt=os.urandom(16); digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,210000); return 'pbkdf2$'+base64.urlsafe_b64encode(salt).decode()+'$'+base64.urlsafe_b64encode(digest).decode()
def verify_password(password: str, encoded: str) -> bool:
 try:
  _,salt,digest=encoded.split('$'); actual=hashlib.pbkdf2_hmac('sha256',password.encode(),base64.urlsafe_b64decode(salt),210000); return hmac.compare_digest(base64.urlsafe_b64encode(actual).decode(),digest)
 except ValueError: return False
