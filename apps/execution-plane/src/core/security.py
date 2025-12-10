"""
CryptoManager - Envelope Encryption with AWS KMS

This module provides zero-trust credential encryption using AWS KMS.
No credentials are stored in plaintext - all sensitive data is encrypted
using envelope encryption with KMS-generated data keys.

Architecture:
1. Request Data Encryption Key (DEK) from KMS
2. KMS returns plaintext DEK + encrypted DEK
3. Encrypt credential with plaintext DEK (AES-256-GCM)
4. Store encrypted blob + encrypted DEK + KMS key ID
5. Discard plaintext DEK from memory

For local development, use moto or localstack to mock KMS.
"""

import os
import base64
import json
import logging
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend

# AWS SDK
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@dataclass
class EncryptedBlob:
    """
    Represents an encrypted credential with KMS metadata.
    """
    encrypted_data: str  # Base64-encoded ciphertext
    encrypted_dek: str   # Base64-encoded encrypted data key from KMS
    kms_key_id: str      # KMS key ID used
    algorithm: str = "AES-256-GCM"
    
    def to_dict(self) -> Dict:
        """Serialize for database storage."""
        return {
            "encrypted_data": self.encrypted_data,
            "encrypted_dek": self.encrypted_dek,
            "kms_key_id": self.kms_key_id,
            "algorithm": self.algorithm
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "EncryptedBlob":
        """Deserialize from database."""
        return cls(
            encrypted_data=data["encrypted_data"],
            encrypted_dek=data["encrypted_dek"],
            kms_key_id=data["kms_key_id"],
            algorithm=data.get("algorithm", "AES-256-GCM")
        )


class CryptoManager:
    """
    Manages envelope encryption for sensitive credentials.
    
    Usage:
        crypto = CryptoManager()
        blob = await crypto.encrypt_credential("my-secret-password")
        # Store blob in PostgreSQL
        
        # Later...
        plaintext = await crypto.decrypt_credential(blob)
    """
    
    def __init__(self, kms_key_id: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize CryptoManager.
        
        Args:
            kms_key_id: AWS KMS key ID (or alias). If None, read from env.
            region: AWS region. If None, read from env.
        """
        self.kms_key_id = kms_key_id or os.getenv("AWS_KMS_KEY_ID")
        self.region = region or os.getenv("AWS_KMS_REGION", "us-east-1")
        
        if not self.kms_key_id:
            logger.warning("AWS_KMS_KEY_ID not set. Using local mock mode.")
            self.kms_key_id = "alias/local-test-key"
        
        # Initialize KMS client
        # For local testing, set AWS_ENDPOINT_URL=http://localhost:4566 (localstack)
        endpoint_url = os.getenv("AWS_ENDPOINT_URL")
        self.kms_client = boto3.client(
            'kms',
            region_name=self.region,
            endpoint_url=endpoint_url
        )
        
        logger.info(f"CryptoManager initialized with KMS key: {self.kms_key_id}")
    
    def generate_data_key(self) -> Tuple[bytes, bytes]:
        """
        Request a Data Encryption Key from KMS.
        
        Returns:
            (plaintext_dek, encrypted_dek): Tuple of plaintext and encrypted DEK
        
        Raises:
            ClientError: If KMS request fails
        """
        try:
            response = self.kms_client.generate_data_key(
                KeyId=self.kms_key_id,
                KeySpec='AES_256'
            )
            
            plaintext_dek = response['Plaintext']
            encrypted_dek = response['CiphertextBlob']
            
            logger.debug(f"Generated data key (encrypted DEK size: {len(encrypted_dek)} bytes)")
            return plaintext_dek, encrypted_dek
            
        except ClientError as e:
            logger.error(f"Failed to generate data key: {e}")
            raise
    
    def decrypt_data_key(self, encrypted_dek: bytes) -> bytes:
        """
        Decrypt a Data Encryption Key using KMS.
        
        Args:
            encrypted_dek: Encrypted data key from KMS
        
        Returns:
            Plaintext data key
        
        Raises:
            ClientError: If KMS decryption fails
        """
        try:
            response = self.kms_client.decrypt(
                CiphertextBlob=encrypted_dek
            )
            
            plaintext_dek = response['Plaintext']
            logger.debug("Successfully decrypted data key")
            return plaintext_dek
            
        except ClientError as e:
            logger.error(f"Failed to decrypt data key: {e}")
            raise
    
    async def encrypt_credential(self, plaintext: str) -> EncryptedBlob:
        """
        Encrypt a credential using envelope encryption.
        
        Args:
            plaintext: The credential to encrypt (e.g., password, API key)
        
        Returns:
            EncryptedBlob with encrypted data and metadata
        
        Raises:
            Exception: If encryption fails
        """
        try:
            # Step 1: Get data encryption key from KMS
            plaintext_dek, encrypted_dek = self.generate_data_key()
            
            # Step 2: Encrypt the credential with the plaintext DEK
            aesgcm = AESGCM(plaintext_dek)
            nonce = os.urandom(12)  # 96-bit nonce for GCM
            
            ciphertext = aesgcm.encrypt(
                nonce,
                plaintext.encode('utf-8'),
                None  # No additional authenticated data
            )
            
            # Prepend nonce to ciphertext (we need it for decryption)
            encrypted_data = nonce + ciphertext
            
            # Step 3: Encode to base64 for storage
            encrypted_data_b64 = base64.b64encode(encrypted_data).decode('utf-8')
            encrypted_dek_b64 = base64.b64encode(encrypted_dek).decode('utf-8')
            
            # Step 4: Clear plaintext DEK from memory (security best practice)
            del plaintext_dek
            
            logger.info("Successfully encrypted credential")
            
            return EncryptedBlob(
                encrypted_data=encrypted_data_b64,
                encrypted_dek=encrypted_dek_b64,
                kms_key_id=self.kms_key_id
            )
            
        except Exception as e:
            logger.error(f"Encryption failed: {e}", exc_info=True)
            raise
    
    async def decrypt_credential(self, blob: EncryptedBlob) -> str:
        """
        Decrypt a credential using envelope encryption.
        
        Args:
            blob: EncryptedBlob containing encrypted data and metadata
        
        Returns:
            Plaintext credential
        
        Raises:
            Exception: If decryption fails
        """
        try:
            # Step 1: Decode from base64
            encrypted_data = base64.b64decode(blob.encrypted_data)
            encrypted_dek = base64.b64decode(blob.encrypted_dek)
            
            # Step 2: Decrypt the DEK using KMS
            plaintext_dek = self.decrypt_data_key(encrypted_dek)
            
            # Step 3: Extract nonce and ciphertext
            nonce = encrypted_data[:12]
            ciphertext = encrypted_data[12:]
            
            # Step 4: Decrypt the credential
            aesgcm = AESGCM(plaintext_dek)
            plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, None)
            
            # Step 5: Clear plaintext DEK from memory
            del plaintext_dek
            
            plaintext = plaintext_bytes.decode('utf-8')
            logger.info("Successfully decrypted credential")
            
            return plaintext
            
        except Exception as e:
            logger.error(f"Decryption failed: {e}", exc_info=True)
            raise
    
    @staticmethod
    def is_kms_available() -> bool:
        """
        Check if KMS is available (or mocked).
        
        Returns:
            True if KMS can be reached
        """
        try:
            kms = boto3.client('kms',
                              region_name=os.getenv("AWS_KMS_REGION", "us-east-1"),
                              endpoint_url=os.getenv("AWS_ENDPOINT_URL"))
            kms.list_keys(Limit=1)
            return True
        except Exception as e:
            logger.warning(f"KMS not available: {e}")
            return False


# Example usage
if __name__ == "__main__":
    import asyncio
    
    async def demo():
        """Demo encryption/decryption flow."""
        logging.basicConfig(level=logging.INFO)
        
        crypto = CryptoManager()
        
        # Encrypt a password
        secret = "super-secret-password-123"
        print(f"Encrypting: {secret}")
        
        blob = await crypto.encrypt_credential(secret)
        print(f"Encrypted blob: {blob.to_dict()}")
        
        # Decrypt it back
        decrypted = await crypto.decrypt_credential(blob)
        print(f"Decrypted: {decrypted}")
        
        assert decrypted == secret, "Encryption/decryption failed!"
        print("Encryption/decryption test passed!")
    
    asyncio.run(demo())
