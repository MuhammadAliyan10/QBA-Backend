package services

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"errors"
	"io"
	"os"
	"sync"
)

// VaultCryptoService handles encryption for sensitive session states using VAULT_MASTER_KEY.
type VaultCryptoService struct {
	key []byte
}

var (
	vaultCryptoInstance *VaultCryptoService
	vaultCryptoOnce     sync.Once
)

func GetVaultCryptoService() *VaultCryptoService {
	vaultCryptoOnce.Do(func() {
		rawKey := os.Getenv("VAULT_MASTER_KEY")
		if len(rawKey) != 32 {
			// Fallback to ENCRYPTION_KEY if VAULT_MASTER_KEY is not set to ensure availability in dev
			rawKey = os.Getenv("ENCRYPTION_KEY")
		}
		
		if len(rawKey) != 32 {
			panic("[VaultCryptoService] VAULT_MASTER_KEY or ENCRYPTION_KEY must be 32 bytes")
		}
		vaultCryptoInstance = &VaultCryptoService{key: []byte(rawKey)}
	})
	return vaultCryptoInstance
}

func (vs *VaultCryptoService) Encrypt(plaintext []byte) ([]byte, error) {
	block, err := aes.NewCipher(vs.key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err = io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, err
	}
	return gcm.Seal(nonce, nonce, plaintext, nil), nil
}

func (vs *VaultCryptoService) Decrypt(ciphertext []byte) ([]byte, error) {
	block, err := aes.NewCipher(vs.key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonceSize := gcm.NonceSize()
	if len(ciphertext) < nonceSize {
		return nil, errors.New("ciphertext too short")
	}
	nonce, data := ciphertext[:nonceSize], ciphertext[nonceSize:]
	return gcm.Open(nil, nonce, data, nil)
}
