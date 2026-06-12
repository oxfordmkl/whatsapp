import os
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

def _get_cipher():
    """
    Retrieves the Fernet cipher configured with WABA_ENCRYPTION_KEY.
    Raises RuntimeError if the key is missing or invalid.
    """
    key = os.environ.get("WABA_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("CRITICAL: WABA_ENCRYPTION_KEY is missing from environment.")
    
    try:
        return Fernet(key.encode('utf-8'))
    except ValueError as e:
        raise RuntimeError(f"CRITICAL: WABA_ENCRYPTION_KEY is invalid. It must be a 32-byte URL-safe base64-encoded string. Details: {e}")

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
        logger.error("Failed to decrypt WABA token: InvalidToken. The key may have rotated or the ciphertext is corrupted.")
        return None
    except Exception as e:
        logger.error(f"Failed to decrypt WABA token: {e}")
        return None

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
