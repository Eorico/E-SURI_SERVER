"""Certificate PDF renderer (Phase 7)."""
from __future__ import annotations


class CertificateRenderer:
    def render(self, student_name: str, cert_type: str, issued_on: str) -> bytes:
        raise NotImplementedError("Phase 7: certificate PDF")
