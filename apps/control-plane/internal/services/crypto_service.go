// internal/services/crypto_service.go
package services

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
)

// CryptoService provides AES-256-GCM authenticated encryption.
// The nonce (12 bytes) is prepended to every ciphertext blob so Decrypt
// is fully self-contained — no out-of-band nonce storage required.
type CryptoService struct {
	key []byte
}

var (
	cryptoInstance *CryptoService
	cryptoOnce    sync.Once
)

// GetCryptoService returns the process-wide singleton CryptoService.
// Reads ENCRYPTION_KEY from the environment on first call.
// Panics at startup (not at request time) if the key is absent or malformed.
func GetCryptoService() *CryptoService {
	cryptoOnce.Do(func() {
		rawKey := os.Getenv("ENCRYPTION_KEY")
		if len(rawKey) != 32 {
			panic(fmt.Sprintf(
				"[CryptoService] ENCRYPTION_KEY must be exactly 32 bytes for AES-256; got %d",
				len(rawKey),
			))
		}
		cryptoInstance = &CryptoService{key: []byte(rawKey)}
	})
	return cryptoInstance
}

// Encrypt encrypts plaintext using AES-256-GCM.
// Output format: [ 12-byte nonce | GCM ciphertext+tag ]
func (cs *CryptoService) Encrypt(plaintext []byte) ([]byte, error) {
	block, err := aes.NewCipher(cs.key)
	if err != nil {
		return nil, fmt.Errorf("crypto: failed to create AES cipher: %w", err)
	}

	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("crypto: failed to create GCM: %w", err)
	}

	nonce := make([]byte, gcm.NonceSize()) // 12 bytes
	if _, err = io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, fmt.Errorf("crypto: failed to generate nonce: %w", err)
	}

	// Seal appends ciphertext+tag after the nonce slice in one allocation.
	ciphertext := gcm.Seal(nonce, nonce, plaintext, nil)
	return ciphertext, nil
}

// Decrypt decrypts a blob produced by Encrypt.
// Expects format: [ 12-byte nonce | GCM ciphertext+tag ]
func (cs *CryptoService) Decrypt(ciphertext []byte) ([]byte, error) {
	block, err := aes.NewCipher(cs.key)
	if err != nil {
		return nil, fmt.Errorf("crypto: failed to create AES cipher: %w", err)
	}

	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("crypto: failed to create GCM: %w", err)
	}

	nonceSize := gcm.NonceSize()
	if len(ciphertext) < nonceSize {
		return nil, errors.New("crypto: ciphertext too short to contain nonce")
	}

	nonce, encryptedData := ciphertext[:nonceSize], ciphertext[nonceSize:]
	plaintext, err := gcm.Open(nil, nonce, encryptedData, nil)
	if err != nil {
		return nil, fmt.Errorf("crypto: authentication or decryption failed: %w", err)
	}

	return plaintext, nil
}
