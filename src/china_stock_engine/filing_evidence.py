"""Audited public-filing field receipts; raw PDFs and vendor payloads stay local."""

from decimal import Decimal, InvalidOperation

from .storage import ArtifactContractError

SCALES = {"CNY": Decimal(1), "CNY_1e6": Decimal(1_000_000), "CNY_1e8": Decimal(100_000_000)}
FIELDS = {"pdf_page", "label", "column", "source_value", "source_unit", "precision_cny", "transformation"}


def validate_field_evidence(row: dict) -> None:
    """Validate the recorded conversion, not the truth of the source or human review."""
    evidence = row["field_evidence"]
    populated = {key for key, value in row["values"].items() if value is not None}
    if (row.get("source_url_kind") != "public_document"
            or row.get("document_access") != "public_document_verified"
            or not row.get("document_sha256") or not isinstance(evidence, dict)
            or set(evidence) != populated):
        raise ArtifactContractError("field evidence requires document hash and every non-null financial field")
    for field, item in evidence.items():
        if (not isinstance(item, dict) or set(item) != FIELDS
                or type(item["pdf_page"]) is not int or not 1 <= item["pdf_page"] <= 2000
                or any(not isinstance(item[key], str) or not 1 <= len(item[key]) <= 180
                       for key in ("label", "column"))
                or isinstance(item["source_value"], bool) or isinstance(item["precision_cny"], bool)
                or item["source_unit"] not in SCALES
                or item["transformation"] not in {"identity", "cash_outflow_absolute"}
                or (item["transformation"] == "cash_outflow_absolute" and field != "capex")):
            raise ArtifactContractError("invalid filing field evidence")
        try:
            value = Decimal(str(item["source_value"]))
            precision = Decimal(str(item["precision_cny"]))
            if not value.is_finite() or not precision.is_finite() or precision <= 0:
                raise InvalidOperation()
            converted = value * SCALES[item["source_unit"]]
            if item["transformation"] == "cash_outflow_absolute":
                converted = abs(converted)
            if converted != Decimal(str(row["values"][field])):
                raise InvalidOperation()
            if converted % precision != 0:
                raise InvalidOperation()
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ArtifactContractError("filing amount, unit, precision or conversion mismatch") from exc
