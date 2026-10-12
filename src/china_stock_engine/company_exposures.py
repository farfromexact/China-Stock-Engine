"""Small, source-attributed business exposure registry, never a thesis ledger."""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlparse

from .research_context import timestamp
from .storage import ArtifactContractError, json_sha256, load_json_object

FIELDS = {"exposure_id", "thscode", "business_segment", "commodity", "channel", "direction",
          "constraints", "transmission_horizon", "source_url", "source_published_at",
          "first_observed_at", "known_at", "confidence", "evidence_summary"}
SOURCE_RECEIPT_FIELDS = {"document_sha256", "source_pdf_pages"}


def load_exposures(path: Path | None, research_asof: str) -> tuple[list[dict], str | None]:
    if path is None:
        return [], None
    payload = load_json_object(path, missing_ok=False)
    if set(payload) != {"schema_version", "exposures"} or payload["schema_version"] != 1:
        raise ArtifactContractError("unsupported company commodity exposure registry")
    rows = payload["exposures"]
    if not isinstance(rows, list) or len(rows) > 100:
        raise ArtifactContractError("company commodity registry must contain at most 100 exposures")
    identities = set()
    visible = []
    for row in rows:
        if not isinstance(row, dict) or set(row) not in (FIELDS, FIELDS | SOURCE_RECEIPT_FIELDS):
            raise ArtifactContractError("invalid company commodity exposure fields")
        if "document_sha256" in row:
            if (not isinstance(row["document_sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", row["document_sha256"])
                    or not isinstance(row["source_pdf_pages"], list) or not row["source_pdf_pages"]
                    or len(row["source_pdf_pages"]) > 20
                    or any(type(page) is not int or not 1 <= page <= 2000 for page in row["source_pdf_pages"])):
                raise ArtifactContractError("invalid exposure document receipt")
        for field in FIELDS - {"constraints"}:
            if not isinstance(row[field], str) or not row[field].strip() or len(row[field]) > 1000:
                raise ArtifactContractError(f"invalid exposure field: {field}")
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", row["thscode"]):
            raise ArtifactContractError("invalid exposure security")
        if row["exposure_id"] in identities:
            raise ArtifactContractError("duplicate exposure_id")
        identities.add(row["exposure_id"])
        if row["channel"] not in {"product_price", "input_cost", "processing_spread", "volume"}:
            raise ArtifactContractError("invalid exposure channel")
        if row["direction"] not in {"positive", "negative", "conditional", "unknown"}:
            raise ArtifactContractError("invalid exposure direction")
        if row["confidence"] not in {"verified", "partially_verified", "research_only"}:
            raise ArtifactContractError("invalid exposure confidence")
        if not isinstance(row["constraints"], list) or not row["constraints"] or any(
            not isinstance(item, str) or not item.strip() or len(item) > 500 for item in row["constraints"]
        ):
            raise ArtifactContractError("exposure transmission constraints are required")
        url = urlparse(row["source_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query:
            raise ArtifactContractError("exposure requires a credential-free HTTPS source URL")
        published, observed, known = map(timestamp, (row["source_published_at"], row["first_observed_at"], row["known_at"]))
        if not published <= observed <= known:
            raise ArtifactContractError("exposure timestamps must preserve first observation")
        if known <= timestamp(research_asof):
            visible.append(row)
    return sorted(visible, key=lambda row: row["exposure_id"]), json_sha256(payload)
