"""Ask VirusTotal for an existing verdict on a file, by hash, before falling back to ClamAV.

Only ever consulted for eligible externally-fetched assets - see
``malware_scan.VIRUSTOTAL_ELIGIBLE_SOURCES`` and
``malware_scan.malware_error_for_fetched_asset``. Never for a direct user
upload or a user's own private cloud photo library: VirusTotal shares every
file it is shown industry-wide, which is fine for content that was already
public before we fetched it and never acceptable for somebody's own photo.

Fails toward "no verdict" for everything except an explicit clean or explicit
malicious/suspicious result: an unknown hash, a disabled/unconfigured
service, an exhausted quota, or any transport/HTTP error all raise
``VirusTotalNoVerdictError``, which the caller catches and silently falls
back to ``malware_error_for_upload``'s ClamAV path. Unlike
``MalwareScanUnavailableError``, this is never a reason to retry or reject an
upload on its own - VirusTotal is an optional fast path here, not the
scanner of record.
"""

from __future__ import annotations

import logging

from urbanlens.dashboard.services.apis.security.virustotal import VirusTotalGateway
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError
from urbanlens.UrbanLens.settings.app import settings as app_settings

logger = logging.getLogger(__name__)


class VirusTotalNoVerdictError(Exception):
    """VirusTotal has no usable, explicit verdict for a hash right now.

    Never a reason to reject or retry an upload by itself - callers catch
    this and fall back to ``malware_error_for_upload`` (ClamAV), which
    remains the scanner of record.
    """


def verdict_for_checksum(sha256: str) -> str | None:
    """Ask VirusTotal for an explicit clean/malicious verdict on an already-known file hash.

    Args:
        sha256: The file's SHA-256 hex digest.

    Returns:
        ``None`` when VirusTotal explicitly reports the file clean (at least
        one engine reported, and zero flagged it malicious or suspicious). A
        user-facing rejection message, matching ``malware_error_for_upload``'s
        shape, when at least one engine flagged it malicious or suspicious.

    Raises:
        VirusTotalNoVerdictError: VirusTotal is not configured, the hash is
            unknown to it, our self-imposed quota/rate limit is exhausted,
            the service is administratively disabled, or any transport/HTTP
            error occurred - every case with no explicit, trustworthy verdict
            to act on.
    """
    if not app_settings.virustotal_api_key:
        raise VirusTotalNoVerdictError("VirusTotal is not configured (no API key)")

    try:
        report = VirusTotalGateway().get_file_report(sha256)
    except (GatewayRequestError, RequestCancelledError, OSError) as exc:
        logger.info("VirusTotal lookup unavailable for %s: %s", sha256, exc)
        raise VirusTotalNoVerdictError(str(exc)) from exc

    if report is None:
        raise VirusTotalNoVerdictError(f"VirusTotal has not seen a file with hash {sha256}")

    stats = report.get("last_analysis_stats") or {}
    reported = sum(v for v in stats.values() if isinstance(v, int))
    if reported == 0:
        # No engine actually weighed in - an empty/unusable stats block must
        # never read as "nothing bad was found" (the exact bug class
        # malware_scan.py's own module docstring exists to prevent for
        # ClamAV; the same rule applies here).
        raise VirusTotalNoVerdictError(f"VirusTotal returned no usable analysis stats for {sha256}: {stats!r}")

    malicious = stats.get("malicious", 0)
    suspicious = stats.get("suspicious", 0)
    if malicious or suspicious:
        logger.warning("VirusTotal flagged a fetched asset as unsafe (%s malicious, %s suspicious): %s", malicious, suspicious, sha256)
        return "This file was flagged as malicious by VirusTotal and was not uploaded."
    return None
