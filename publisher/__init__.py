"""publisher — layered automatic publishing.

Layers: policy · artifact · credentials · signature · nonce · transport · adapters.
service is the only publish entry point — all gate, signature and nonce enforcement lives there.
"""
from .credentials import Credentials, CredentialsMissing, load_credentials
from .draft_upload import DraftUploadResult, upload_draft
from .policy import PolicyError, PublishPolicy, load_policy
from .service import (
    PublishBlocked,
    PublisherError,
    PublishResult,
    publish,
)
from .transport import BloggerTransport, FakeTransport, ThreadsTransport, Transport

__all__ = [
    "publish",
    "upload_draft",
    "DraftUploadResult",
    "PublishResult",
    "PublisherError",
    "PublishBlocked",
    "CredentialsMissing",
    "Credentials",
    "load_credentials",
    "PublishPolicy",
    "PolicyError",
    "load_policy",
    "Transport",
    "ThreadsTransport",
    "BloggerTransport",
    "FakeTransport",
]
