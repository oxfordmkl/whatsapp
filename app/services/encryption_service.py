import os
import logging
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

# ── Phase RC2.5.19-C: key rotation foundation ────────────────────────────────
#
# Before this phase there was exactly one key, WABA_ENCRYPTION_KEY, and no way
# to change it: every stored token was encrypted under it, so replacing it
# made every tenant's credential undecryptable at once -- and decrypt_token()
# turns that into a silent None, i.e. a WhatsApp outage for every tenant.
#
# MultiFernet fixes the mechanism without any schema: it ENCRYPTS with the
# first key and DECRYPTS with whichever configured key works. A Fernet token
# does not name its key, so no key-version column is needed.
#
#   WABA_ENCRYPTION_KEY            current key. Encrypts. Required (unchanged).
#   WABA_ENCRYPTION_KEYS_PREVIOUS  OPTIONAL, comma-separated, decrypt-only.
#
# With only WABA_ENCRYPTION_KEY set -- production today -- behaviour is
# byte-for-byte what it was: one key, same ciphertext format, same errors.
#
# ROTATION PROCEDURE (never automatic; each step separately authorised):
#   1. Generate a new Fernet key.
#   2. Set WABA_ENCRYPTION_KEYS_PREVIOUS = <old key>, then
#      WABA_ENCRYPTION_KEY = <new key>. Existing tokens still decrypt (via the
#      previous key); new saves encrypt under the new key.
#   3. Run reencrypt_all_tenant_tokens(dry_run=True), review the counts, then
#      dry_run=False. Every stored token is re-encrypted under the new key.
#   4. Only once step 3 reports zero failures, remove the old key from
#      WABA_ENCRYPTION_KEYS_PREVIOUS.
# Removing the old key before step 3 completes is the one way to lose
# credentials; step 4's precondition exists to prevent exactly that.

_PREVIOUS_KEYS_VAR = "WABA_ENCRYPTION_KEYS_PREVIOUS"


def _fernet_for(key: str, label: str) -> Fernet:
    try:
        return Fernet(key.encode('utf-8'))
    except ValueError as e:
        # The label names WHICH key is bad; the key itself is never included.
        raise RuntimeError(f"CRITICAL: {label} is invalid. It must be a 32-byte URL-safe base64-encoded string. Details: {e}")


def _get_cipher():
    """
    Returns a MultiFernet: encrypts with WABA_ENCRYPTION_KEY, decrypts with it
    or with any key listed in WABA_ENCRYPTION_KEYS_PREVIOUS.
    Raises RuntimeError if the current key is missing, or any key is invalid.
    Keys are read at call time, so a changed environment takes effect without
    a restart of this module.
    """
    key = os.environ.get("WABA_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("CRITICAL: WABA_ENCRYPTION_KEY is missing from environment.")

    ciphers = [_fernet_for(key, "WABA_ENCRYPTION_KEY")]
    previous = [k.strip() for k in os.environ.get(_PREVIOUS_KEYS_VAR, "").split(",") if k.strip()]
    for i, old in enumerate(previous, 1):
        ciphers.append(_fernet_for(old, f"{_PREVIOUS_KEYS_VAR} entry #{i}"))
    return MultiFernet(ciphers)

def encrypt_token(plaintext: str) -> str:
    """
    Encrypts a plaintext WABA access token.
    Returns the encrypted token as a string.
    Returns None if the input is empty or None.
    """
    if not plaintext:
        return None
        
    cipher = _get_cipher()
    # Fernet requires bytes
    plaintext_bytes = plaintext.encode('utf-8')
    ciphertext_bytes = cipher.encrypt(plaintext_bytes)
    return ciphertext_bytes.decode('utf-8')

def decrypt_token(ciphertext: str) -> str:
    """
    Decrypts a WABA access token.
    Returns the plaintext token as a string.
    Returns None if the ciphertext is empty, None, or invalid.
    """
    if not ciphertext:
        return None
        
    cipher = _get_cipher()
    try:
        ciphertext_bytes = ciphertext.encode('utf-8')
        plaintext_bytes = cipher.decrypt(ciphertext_bytes)
        return plaintext_bytes.decode('utf-8')
    except InvalidToken:
        logger.error("Failed to decrypt WABA token: InvalidToken. No configured key "
                     "(current or previous) matches -- the key may have rotated "
                     "without WABA_ENCRYPTION_KEYS_PREVIOUS, or the ciphertext is corrupted.")
        return None
    except Exception as e:
        # Class only: an exception message could echo input.
        logger.error("Failed to decrypt WABA token: %s", type(e).__name__)
        return None


def rotate_token(ciphertext: str) -> str:
    """
    Re-encrypts `ciphertext` under the CURRENT key. Returns the new ciphertext,
    or None if no configured key can decrypt it (it is then left untouched by
    the caller). Never returns or logs plaintext.
    """
    if not ciphertext:
        return None
    cipher = _get_cipher()
    try:
        return cipher.rotate(ciphertext.encode('utf-8')).decode('utf-8')
    except InvalidToken:
        logger.error("WABA token rotation failed: InvalidToken (no configured key matches).")
        return None


def reencrypt_all_tenant_tokens(dry_run: bool = True) -> dict:
    """
    Step 3 of the rotation procedure above. NEVER called automatically.

    dry_run=True (default) reads and counts only; nothing is written.
    dry_run=False re-encrypts every tenant token under the current key in one
    transaction, committing only if EVERY token rotated -- a single failure
    rolls the whole batch back, so a partially rotated table cannot exist.

    Returns counts only: {"total", "rotated", "failed", "written"}. No tenant
    id, token or ciphertext is returned or logged.
    """
    from app.extensions import db
    from app.models import Tenant

    rows = Tenant.query.filter(Tenant.waba_access_token_encrypted.isnot(None)).all()
    result = {"total": len(rows), "rotated": 0, "failed": 0, "written": False}
    pending = []
    for t in rows:
        new = rotate_token(t.waba_access_token_encrypted)
        if new is None:
            result["failed"] += 1
        else:
            result["rotated"] += 1
            pending.append((t, new))

    if dry_run or result["failed"]:
        db.session.rollback()
        logger.info("WABA re-encryption %s: %s", "dry run" if dry_run else "ABORTED", result)
        return result

    try:
        for t, new in pending:
            t.waba_access_token_encrypted = new
        db.session.commit()
        result["written"] = True
    except Exception as e:                                       # noqa: BLE001
        db.session.rollback()
        logger.error("WABA re-encryption rolled back: %s", type(e).__name__)
    logger.info("WABA re-encryption: %s", result)
    return result

if __name__ == "__main__":
    # Local self-test utility
    import sys
    
    print("--- WABA Encryption Service Self-Test ---")
    
    # Generate a temporary key for testing if one isn't present
    if not os.environ.get("WABA_ENCRYPTION_KEY"):
        test_key = Fernet.generate_key().decode('utf-8')
        print(f"No WABA_ENCRYPTION_KEY found. Using temporary key: {test_key}")
        os.environ["WABA_ENCRYPTION_KEY"] = test_key
        
    test_token = "EAAB1234567890abcdefGHIJKL"
    print(f"\nOriginal Plaintext: {test_token}")
    
    try:
        encrypted = encrypt_token(test_token)
        print(f"Encrypted Ciphertext: {encrypted}")
        
        decrypted = decrypt_token(encrypted)
        print(f"Decrypted Plaintext:  {decrypted}")
        
        assert test_token == decrypted, "Decryption mismatch!"
        print("\n[PASS] SELF-TEST PASSED: Encryption and Decryption successful.")
        
    except Exception as e:
        print(f"\n[FAIL] SELF-TEST FAILED: {e}")
        sys.exit(1)
