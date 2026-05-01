"""
Universal Storage Module - Industrial-Grade Blob Storage

Provides a unified interface for file storage supporting:
- AWS S3 (Production)
- MinIO (Local Development)

Security:
- Credentials loaded securely from environment variables
- Presigned URLs for secure, time-limited access
- Server-side encryption (AES-256) for S3

Usage:
    storage = UniversalStorage()
    url = await storage.upload(file_bytes, "job-123/screenshot.png", "image/png")

Environment Variables:
    S3_BUCKET:          Bucket name (required)
    S3_REGION:          AWS region (default: us-east-1)
    S3_ENDPOINT_URL:    Custom endpoint for MinIO (optional)
    AWS_ACCESS_KEY_ID:  AWS access key
    AWS_SECRET_ACCESS_KEY: AWS secret key
"""

import os
import logging
import asyncio
from io import BytesIO
from typing import Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger("storage")


@dataclass
class StorageConfig:
    """Configuration for storage backend."""
    bucket: str
    region: str
    endpoint_url: Optional[str]
    access_key: str
    secret_key: str
    use_ssl: bool
    presigned_url_expiry: int  # seconds


class StorageError(Exception):
    """Base exception for storage operations."""
    pass


class StorageUploadError(StorageError):
    """Raised when file upload fails."""
    pass


class StorageConfigError(StorageError):
    """Raised when configuration is missing or invalid."""
    pass


class UniversalStorage:
    """
    Universal Storage Client supporting AWS S3 and MinIO.

    Thread-safe and async-compatible via run_in_executor.
    """

    # Default presigned URL expiry (1 hour)
    DEFAULT_PRESIGNED_EXPIRY = 3600

    # Maximum file size (100MB)
    MAX_FILE_SIZE = 100 * 1024 * 1024

    # Allowed MIME types for security
    ALLOWED_MIME_TYPES = {
        "image/png", "image/jpeg", "image/webp", "image/gif",
        "application/pdf", "text/csv", "application/json",
        "video/mp4", "video/webm",
    }

    def __init__(self, config: Optional[StorageConfig] = None):
        """
        Initialize storage client.

        Args:
            config: Optional StorageConfig. If not provided, reads from environment.

        Raises:
            StorageConfigError: If required configuration is missing.
        """
        self._config = config or self._load_config_from_env()
        self._client = self._create_client()

        logger.info(
            f"[Storage] Initialized: bucket={self._config.bucket}, "
            f"region={self._config.region}, "
            f"endpoint={self._config.endpoint_url or 'AWS S3'}"
        )

    def _load_config_from_env(self) -> StorageConfig:
        """
        Load configuration from environment variables.

        Prioritizes Cloudflare R2 (Production) variables, falls back to S3/MinIO (Local).
        """
        # 1. Try Cloudflare R2 (Production Standard)
        bucket = os.getenv("R2_BUCKET_NAME")
        access_key = os.getenv("R2_ACCESS_KEY_ID")
        secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
        endpoint_url = os.getenv("R2_ENDPOINT_URL")

        # 2. Fallback to Legacy S3 / MinIO (Local Dev)
        if not bucket:
            bucket = os.getenv("S3_BUCKET")
            access_key = os.getenv("AWS_ACCESS_KEY_ID")
            secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
            endpoint_url = os.getenv("S3_ENDPOINT_URL")

        if not bucket:
            raise StorageConfigError(
                "Storage configuration missing. "
                "Set R2_BUCKET_NAME (Production) or S3_BUCKET (Local/MinIO)."
            )

        # For MinIO/Local, use default credentials if not set
        if endpoint_url and not access_key:
            access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
            secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")

        return StorageConfig(
            bucket=bucket,
            region=os.getenv("S3_REGION", "auto"), # R2 uses 'auto' or 'us-east-1'
            endpoint_url=endpoint_url,
            access_key=access_key or "",
            secret_key=secret_key or "",
            use_ssl=os.getenv("S3_USE_SSL", "true").lower() == "true",
            presigned_url_expiry=int(os.getenv("S3_PRESIGNED_EXPIRY", str(self.DEFAULT_PRESIGNED_EXPIRY)))
        )

    def _create_client(self):
        """Create boto3 S3 client with appropriate configuration."""
        client_kwargs = {
            "service_name": "s3",
            "region_name": self._config.region,
        }

        # Add credentials if provided
        if self._config.access_key and self._config.secret_key:
            client_kwargs["aws_access_key_id"] = self._config.access_key
            client_kwargs["aws_secret_access_key"] = self._config.secret_key

        # Add custom endpoint for MinIO
        if self._config.endpoint_url:
            client_kwargs["endpoint_url"] = self._config.endpoint_url
            client_kwargs["use_ssl"] = self._config.use_ssl
            # MinIO requires path-style addressing
            client_kwargs["config"] = boto3.session.Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"}
            )

        return boto3.client(**client_kwargs)

    def _validate_upload(self, data: bytes, key: str, content_type: str) -> None:
        """
        Validate upload parameters.

        Raises:
            StorageUploadError: If validation fails.
        """
        # Check file size
        if len(data) > self.MAX_FILE_SIZE:
            raise StorageUploadError(
                f"File size ({len(data)} bytes) exceeds maximum ({self.MAX_FILE_SIZE} bytes)"
            )

        # Check content type
        if content_type not in self.ALLOWED_MIME_TYPES:
            raise StorageUploadError(
                f"Content type '{content_type}' not allowed. "
                f"Allowed types: {self.ALLOWED_MIME_TYPES}"
            )

        # Validate key (prevent path traversal)
        if ".." in key or key.startswith("/"):
            raise StorageUploadError(
                f"Invalid key '{key}'. Keys must not contain '..' or start with '/'."
            )

    def _sync_upload(self, data: bytes, key: str, content_type: str) -> str:
        """
        Synchronous upload implementation.

        Returns:
            Presigned URL for the uploaded file.
        """
        try:
            # Upload with server-side encryption (AES-256)
            extra_args = {
                "ContentType": content_type,
                "ServerSideEncryption": "AES256",
            }

            # For MinIO, skip encryption if not supported
            if self._config.endpoint_url:
                extra_args = {"ContentType": content_type}

            self._client.upload_fileobj(
                BytesIO(data),
                self._config.bucket,
                key,
                ExtraArgs=extra_args
            )

            logger.info(f"[Storage] Uploaded {len(data)} bytes to s3://{self._config.bucket}/{key}")

            # Generate presigned URL
            presigned_url = self._client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self._config.bucket,
                    "Key": key,
                },
                ExpiresIn=self._config.presigned_url_expiry
            )

            return presigned_url

        except NoCredentialsError as e:
            raise StorageUploadError(
                "AWS credentials not configured. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
            ) from e
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            raise StorageUploadError(
                f"S3 upload failed (code={error_code}): {e}"
            ) from e

    async def upload(
        self,
        data: bytes,
        key: str,
        content_type: str = "application/octet-stream"
    ) -> str:
        """
        Upload file to storage asynchronously.

        Args:
            data: File content as bytes
            key: Object key (path within bucket), e.g., "job-123/screenshot.png"
            content_type: MIME type of the file

        Returns:
            Presigned URL for accessing the uploaded file

        Raises:
            StorageUploadError: If upload fails
        """
        # Validate before upload
        self._validate_upload(data, key, content_type)

        # Run synchronous boto3 call in thread executor (non-blocking)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._sync_upload,
            data,
            key,
            content_type
        )

    async def upload_screenshot(
        self,
        screenshot_data: bytes,
        job_id: str,
        step_index: int
    ) -> str:
        """
        Convenience method for uploading job screenshots.

        Args:
            screenshot_data: JPEG or PNG screenshot bytes
            job_id: Job identifier
            step_index: Step number in the workflow

        Returns:
            Presigned URL for the screenshot
        """
        # Detect content type from magic bytes
        content_type = "image/jpeg"
        if screenshot_data[:8] == b'\x89PNG\r\n\x1a\n':
            content_type = "image/png"
        elif screenshot_data[:4] == b'RIFF' and screenshot_data[8:12] == b'WEBP':
            content_type = "image/webp"

        extension = content_type.split("/")[1]
        key = f"{job_id}/step_{step_index:03d}.{extension}"

        return await self.upload(screenshot_data, key, content_type)

    async def upload_pdf(
        self,
        pdf_data: bytes,
        job_id: str,
        filename: str
    ) -> str:
        """
        Convenience method for uploading job PDF outputs.

        Args:
            pdf_data: PDF file bytes
            job_id: Job identifier
            filename: Original filename (will be sanitized)

        Returns:
            Presigned URL for the PDF
        """
        # Sanitize filename
        safe_filename = "".join(c for c in filename if c.isalnum() or c in "._-")
        if not safe_filename.endswith(".pdf"):
            safe_filename += ".pdf"

        key = f"{job_id}/outputs/{safe_filename}"

        return await self.upload(pdf_data, key, "application/pdf")

    def get_bucket_url(self, key: str) -> str:
        """
        Get the full S3 URL (not presigned) for a key.

        Useful for internal references. Note: This URL may not be publicly accessible.
        """
        if self._config.endpoint_url:
            # MinIO URL format
            return f"{self._config.endpoint_url}/{self._config.bucket}/{key}"
        else:
            # AWS S3 URL format
            return f"https://{self._config.bucket}.s3.{self._config.region}.amazonaws.com/{key}"

    async def delete(self, key: str) -> bool:
        """
        Delete an object from storage.

        Args:
            key: Object key to delete

        Returns:
            True if deleted successfully
        """
        def _sync_delete():
            try:
                self._client.delete_object(
                    Bucket=self._config.bucket,
                    Key=key
                )
                logger.info(f"[Storage] Deleted s3://{self._config.bucket}/{key}")
                return True
            except ClientError as e:
                logger.error(f"[Storage] Delete failed for {key}: {e}")
                return False

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync_delete)

    def is_available(self) -> bool:
        """
        Check if storage backend is reachable.

        Returns:
            True if bucket is accessible
        """
        try:
            self._client.head_bucket(Bucket=self._config.bucket)
            return True
        except ClientError:
            return False


# =============================================================================
# GLOBAL SINGLETON
# =============================================================================

_storage_instance: Optional[UniversalStorage] = None


def get_storage() -> Optional[UniversalStorage]:
    """
    Get or create UniversalStorage singleton.

    Returns None if storage is not configured (S3_BUCKET not set).
    """
    global _storage_instance

    if _storage_instance is not None:
        return _storage_instance

    try:
        _storage_instance = UniversalStorage()
        return _storage_instance
    except StorageConfigError as e:
        logger.warning(f"[Storage] Disabled - {e}")
        return None
    except Exception as e:
        logger.warning(f"[Storage] Disabled - Initialization failed: {e}")
        return None


def is_storage_available() -> bool:
    """Check if storage is configured and available."""
    storage = get_storage()
    return storage is not None and storage.is_available()


# =============================================================================
# TEST/DEMO
# =============================================================================

if __name__ == "__main__":
    import asyncio

    async def demo():
        print("=" * 60)
        print("UNIVERSAL STORAGE - DEMO")
        print("=" * 60)

        # Test with MinIO (docker-compose)
        os.environ["S3_BUCKET"] = "e2e-local-bucket"
        os.environ["S3_ENDPOINT_URL"] = "http://localhost:9000"
        os.environ["AWS_ACCESS_KEY_ID"] = "minioadmin"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "minioadmin"
        os.environ["S3_USE_SSL"] = "false"

        storage = get_storage()

        if not storage:
            print("❌ Storage not available")
            return

        print(f"✅ Storage initialized")
        print(f"   Available: {storage.is_available()}")

        # Test upload
        test_data = b"Hello, World!"
        try:
            url = await storage.upload(test_data, "test/hello.txt", "text/plain")
            print(f"✅ Upload successful!")
            print(f"   URL: {url[:80]}...")
        except StorageUploadError as e:
            print(f"❌ Upload failed: {e}")

    asyncio.run(demo())
