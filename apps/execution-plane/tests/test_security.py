"""
Unit tests for CryptoManager (envelope encryption with KMS).
"""

import pytest
import os
from unittest.mock import Mock, patch, MagicMock
from src.core.security import CryptoManager, EncryptedBlob


class TestCryptoManager:
    """Test suite for CryptoManager envelope encryption."""

    @pytest.fixture
    def mock_kms_client(self):
        """Create a mocked KMS client."""
        with patch('src.core.security.boto3.client') as mock_client:
            # Mock KMS responses
            kms = MagicMock()
            
            # Mock generate_data_key
            kms.generate_data_key.return_value = {
                'Plaintext': b'0' * 32,  # 32-byte key
                'CiphertextBlob': b'encrypted_key_data'
            }
            
            # Mock decrypt
            kms.decrypt.return_value = {
                'Plaintext': b'0' * 32
            }
            
            mock_client.return_value = kms
            yield kms

    @pytest.fixture
    def crypto_manager(self, mock_kms_client):
        """Create CryptoManager with mocked KMS."""
        os.environ['AWS_KMS_KEY_ID'] = 'alias/test-key'
        os.environ['AWS_KMS_REGION'] = 'us-east-1'
        return CryptoManager()

    @pytest.mark.asyncio
    async def test_encrypt_decrypt_roundtrip(self, crypto_manager):
        """Test successful encryption and decryption."""
        secret = "my-super-secret-api-key-123"
        
        # Encrypt
        blob = await crypto_manager.encrypt_credential(secret)
        
        assert blob.encrypted_data is not None
        assert blob.encrypted_dek is not None
        assert blob.kms_key_id == 'alias/test-key'
        assert blob.algorithm == "AES-256-GCM"
        
        # Decrypt
        decrypted = await crypto_manager.decrypt_credential(blob)
        
        assert decrypted == secret

    @pytest.mark.asyncio
    async def test_encrypted_data_is_different(self, crypto_manager):
        """Test that encrypted data is not plaintext."""
        secret = "password123"
        
        blob = await crypto_manager.encrypt_credential(secret)
        
        # Encrypted data should not contain plaintext
        assert secret not in blob.encrypted_data
        assert secret.encode() not in blob.encrypted_data.encode()

    @pytest.mark.asyncio
    async def test_multiple_encryptions_different_ciphertext(self, crypto_manager):
        """Test that encrypting the same plaintext twice produces different ciphertext (due to nonce)."""
        secret = "same-password"
        
        blob1 = await crypto_manager.encrypt_credential(secret)
        blob2 = await crypto_manager.encrypt_credential(secret)
        
        # Ciphertexts should be different (different nonces)
        assert blob1.encrypted_data != blob2.encrypted_data
        
        # But both should decrypt to the same plaintext
        decrypted1 = await crypto_manager.decrypt_credential(blob1)
        decrypted2 = await crypto_manager.decrypt_credential(blob2)
        
        assert decrypted1 == decrypted2 == secret

    def test_encrypted_blob_serialization(self):
        """Test EncryptedBlob to_dict and from_dict."""
        blob = EncryptedBlob(
            encrypted_data="ciphertext123",
            encrypted_dek="dek456",
            kms_key_id="alias/test"
        )
        
        # Serialize
        data = blob.to_dict()
        assert data['encrypted_data'] == "ciphertext123"
        assert data['encrypted_dek'] == "dek456"
        assert data['kms_key_id'] == "alias/test"
        
        # Deserialize
        restored = EncryptedBlob.from_dict(data)
        assert restored.encrypted_data == blob.encrypted_data
        assert restored.encrypted_dek == blob.encrypted_dek
        assert restored.kms_key_id == blob.kms_key_id

    @pytest.mark.asyncio
    async def test_decrypt_with_wrong_key_fails(self, crypto_manager):
        """Test that decryption fails with wrong key."""
        secret = "password"
        blob = await crypto_manager.encrypt_credential(secret)
        
        # Tamper with encrypted DEK
        blob.encrypted_dek = "invalid_base64_data"
        
        with pytest.raises(Exception):
            await crypto_manager.decrypt_credential(blob)

    def test_generate_data_key(self, crypto_manager, mock_kms_client):
        """Test KMS data key generation."""
        plaintext_dek, encrypted_dek = crypto_manager.generate_data_key()
        
        assert len(plaintext_dek) == 32  # 256-bit AES key
        assert encrypted_dek == b'encrypted_key_data'
        
        # Verify KMS was called correctly
        mock_kms_client.generate_data_key.assert_called_once()

    def test_decrypt_data_key(self, crypto_manager, mock_kms_client):
        """Test KMS data key decryption."""
        encrypted_dek = b'encrypted_key_data'
        
        plaintext_dek = crypto_manager.decrypt_data_key(encrypted_dek)
        
        assert len(plaintext_dek) == 32
        mock_kms_client.decrypt.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_string_encryption(self, crypto_manager):
        """Test encrypting empty string."""
        blob = await crypto_manager.encrypt_credential("")
        decrypted = await crypto_manager.decrypt_credential(blob)
        
        assert decrypted == ""

    @pytest.mark.asyncio
    async def test_unicode_encryption(self, crypto_manager):
        """Test encrypting Unicode characters."""
        secret = "🔐 Secret with émojis and ümlauts"
        
        blob = await crypto_manager.encrypt_credential(secret)
        decrypted = await crypto_manager.decrypt_credential(blob)
        
        assert decrypted == secret
