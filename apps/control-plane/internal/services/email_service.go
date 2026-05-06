package services

import (
	"encoding/base64"
	"fmt"
	"log"
	"net/smtp"
	"os"
)

// EmailService handles sending emails with attachments.
type EmailService struct {
	host     string
	port     string
	username string
	password string
	from     string
}

// NewEmailService creates a new email service using environment variables.
func NewEmailService() *EmailService {
	return &EmailService{
		host:     os.Getenv("SMTP_HOST"),
		port:     os.Getenv("SMTP_PORT"),
		username: os.Getenv("SMTP_USER"),
		password: os.Getenv("SMTP_PASSWORD"),
		from:     os.Getenv("SMTP_FROM"),
	}
}

// SendWithAttachment sends an email with an optional file attachment.
func (s *EmailService) SendWithAttachment(to, subject, body, attachment, filename string) error {
	// If SMTP is not configured, log and skip (fail-safe for dev)
	if s.host == "" || s.username == "" {
		log.Printf("[Email] SKIP: SMTP not configured (need SMTP_HOST, SMTP_USER, etc.)")
		return nil
	}

	// Build the email message with MIME support for attachments
	delimiter := "v1_quanta_boundary"
	header := fmt.Sprintf("From: %s\r\n", s.from)
	header += fmt.Sprintf("To: %s\r\n", to)
	header += fmt.Sprintf("Subject: %s\r\n", subject)
	header += "MIME-Version: 1.0\r\n"
	header += fmt.Sprintf("Content-Type: multipart/mixed; boundary=%s\r\n", delimiter)
	header += "\r\n"

	// Body part
	msg := fmt.Sprintf("--%s\r\n", delimiter)
	msg += "Content-Type: text/html; charset=utf-8\r\n"
	msg += "\r\n"
	msg += body + "\r\n"

	// Attachment part (if provided)
	if attachment != "" {
		msg += fmt.Sprintf("--%s\r\n", delimiter)
		msg += fmt.Sprintf("Content-Type: text/csv; name=\"%s\"\r\n", filename)
		msg += "Content-Transfer-Encoding: base64\r\n"
		msg += fmt.Sprintf("Content-Disposition: attachment; filename=\"%s\"\r\n", filename)
		msg += "\r\n"

		encoded := base64.StdEncoding.EncodeToString([]byte(attachment))

		// Break base64 into lines for SMTP protocol limits
		for i := 0; i < len(encoded); i += 76 {
			end := i + 76
			if end > len(encoded) {
				end = len(encoded)
			}
			msg += encoded[i:end] + "\r\n"
		}
	}

	msg += fmt.Sprintf("--%s--", delimiter)

	// Combine header and message
	fullMsg := []byte(header + msg)

	// Authentication
	auth := smtp.PlainAuth("", s.username, s.password, s.host)

	// SEND
	err := smtp.SendMail(s.host+":"+s.port, auth, s.from, []string{to}, fullMsg)
	if err != nil {
		log.Printf("[Email] Failed to send email to %s: %v", to, err)
		return err
	}

	log.Printf("[Email] Successfully sent email to %s", to)
	return nil
}
