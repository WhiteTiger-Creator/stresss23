"""Verify Certwarden audit CLI and repaired signal workflow."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

OUTPUT_DIR = Path("/app/output")
DIAGNOSIS_PATH = OUTPUT_DIR / "diagnosis.json"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
MATRIX_PATH = OUTPUT_DIR / "issuer_matrix.json"
FLAGGED_PATH = OUTPUT_DIR / "escalated.jsonl"
REPAIR_AUDIT_PATH = OUTPUT_DIR / "repair_audit.json"
CLI = Path("/app/cert_audit.py")
PIPELINE = Path("/app/workflow/export_report.py")
ORIGINAL_PIPELINE = Path("/app/workflow/.export_report.original")
DOSSIER_PATH = Path("/app/incident/export_dossier.md")
INPUT_PATH = Path("/app/data/events.json")
OVERRIDES_PATH = Path("/app/data/dismissal_overrides.json")
REPORT_SPEC_PATH = Path("/app/docs/report_spec.json")
ALT_INPUT = Path("/tests/fixtures/alt_events.json")
BROKEN_PIPELINE_SHA256 = "583085d5087dfc0c7d869c84a22ccbc2bc634f334f7d8f5193bb262011089220"
SPEC_DATA = json.loads(REPORT_SPEC_PATH.read_text())
ISSUE_EVIDENCE_TERMS = SPEC_DATA["diagnosis_report"]["issues_found_item"]["evidence"][
    "required_terms_by_issue"
]
REQUIRED_ISSUE_IDS = SPEC_DATA["diagnosis_report"]["issues_found_item"]["allowed_ids"]
FORBIDDEN_TOKENS = ('event["issued_at"]', 'severity == "critical"')
ANOMALY_SEVERITIES = {"high", "critical"}
SEVERITY_ORDER = ("critical", "high", "medium", "low")
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def _executable_text(src: str) -> str:
    docstring_lines: set[int] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):  # noqa: SIM102
            if isinstance(first.value.value, str):
                end = getattr(first, "end_lineno", first.lineno)
                docstring_lines.update(range(first.lineno, end + 1))

    lines: list[str] = []
    for line_number, line in enumerate(src.splitlines(), start=1):
        if line_number in docstring_lines:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0]
        lines.append(line)
    return "\n".join(lines)


def _load_events(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def _normalize_severity(value: object) -> str:
    return str(value if value is not None else "").strip().lower()


def _normalize_issuer(value: object) -> str:
    return str(value if value is not None else "").strip().lower()


def _normalize_issued_ms(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return 0
    return 0


def _normalize_detector(value: object) -> str:
    return " ".join(str(value if value is not None else "").split())


def _normalize_override_scope(value: object) -> str:
    normalized = str(value if value is not None else "").strip().lower()
    return normalized if normalized in {"all", "high", "critical"} else ""


def _normalize_dismissed(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def _canonicalize_events(events: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for event in events:
        normalized = dict(event)
        normalized["issued_ms"] = _normalize_issued_ms(normalized.get("issued_ms", 0))
        normalized["severity"] = _normalize_severity(normalized.get("severity", ""))
        normalized["issuer"] = _normalize_issuer(normalized.get("issuer", ""))
        normalized["dismissed"] = _normalize_dismissed(normalized.get("dismissed", False))
        normalized["detector"] = _normalize_detector(normalized.get("detector", ""))
        cert_id = str(normalized["cert_id"])
        current = deduped.get(cert_id)
        if current is None:
            deduped[cert_id] = normalized
            continue
        replace = False
        if normalized["issued_ms"] > current["issued_ms"]:
            replace = True
        elif normalized["issued_ms"] == current["issued_ms"]:
            if _severity_rank(normalized["severity"]) > _severity_rank(current["severity"]):
                replace = True
            elif _severity_rank(normalized["severity"]) == _severity_rank(current["severity"]):
                if int(_normalize_dismissed(normalized.get("dismissed", False))) < int(
                    _normalize_dismissed(current.get("dismissed", False))
                ):
                    replace = True
                elif int(_normalize_dismissed(normalized.get("dismissed", False))) == int(
                    _normalize_dismissed(current.get("dismissed", False))
                ):
                    if _normalize_detector(normalized.get("detector", "")) > _normalize_detector(
                        current.get("detector", "")
                    ):
                        replace = True
                    elif _normalize_detector(normalized.get("detector", "")) == _normalize_detector(  # noqa: SIM102
                        current.get("detector", "")
                    ):
                        if _normalize_issuer(
                            normalized.get("issuer", "")
                        ) > _normalize_issuer(current.get("issuer", "")):
                            replace = True
        if replace:
            deduped[cert_id] = normalized
    return sorted(deduped.values(), key=lambda row: row["issued_ms"])


def _is_signal(event: dict) -> bool:
    if _normalize_dismissed(event.get("dismissed", False)):
        return False
    return _normalize_severity(event.get("severity", "")) in ANOMALY_SEVERITIES


def _build_issuer_matrix(events: list[dict]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for event in events:
        issuer = _normalize_issuer(event.get("issuer", ""))
        severity = _normalize_severity(event.get("severity", ""))
        matrix.setdefault(issuer, {name: 0 for name in SEVERITY_ORDER})
        if severity in matrix[issuer]:
            matrix[issuer][severity] += 1
    return {issuer: matrix[issuer] for issuer in sorted(matrix)}


def _compact_overrides(
    rows: list[dict],
) -> dict[tuple[str, str], list[tuple[int, int]]]:
    by_key: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in rows:
        issuer = _normalize_issuer(row.get("issuer", ""))
        scope = _normalize_override_scope(row.get("severity_scope", ""))
        if not scope:
            continue
        start = _normalize_issued_ms(row.get("start_ms", 0))
        end = _normalize_issued_ms(row.get("end_ms", 0))
        if end <= start:
            continue
        by_key.setdefault((issuer, scope), []).append((start, end))

    compacted: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for key, intervals in by_key.items():
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        compacted[key] = [(start, end) for start, end in merged]
    return compacted


def _is_override_suppressed(
    event: dict,
    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]],
) -> bool:
    issuer = _normalize_issuer(event.get("issuer", ""))
    severity = _normalize_severity(event.get("severity", ""))
    issued_ms = _normalize_issued_ms(event.get("issued_ms", 0))
    for scope in ("all", severity):
        for start, end in compacted_overrides.get((issuer, scope), []):
            if start <= issued_ms < end:
                return True
    return False


def _override_compaction_checksum(
    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]]
) -> str:
    return hashlib.sha256(
        "\n".join(
            f"{issuer}|{scope}|{start}|{end}"
            for issuer, scope in sorted(compacted_overrides)
            for start, end in compacted_overrides[(issuer, scope)]
        ).encode("utf-8")
    ).hexdigest()


def _probe_overlap_ms(issued_ms: int, spans: list[tuple[int, int]], lookback_ms: int = 120) -> int:
    probe_start = issued_ms - lookback_ms
    probe_end = issued_ms + 1
    total = 0
    for start, end in spans:
        overlap_start = max(probe_start, start)
        overlap_end = min(probe_end, end)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return total


def _annotate_chains(rows: list[dict]) -> None:
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    tokens = [set(str(row["detector"]).lower().split()) for row in rows]
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if abs(rows[left]["issued_ms"] - rows[right]["issued_ms"]) > 600:
                continue
            if (
                rows[left]["issuer"] == rows[right]["issuer"]
                or len(tokens[left] & tokens[right]) >= 2
            ):
                union(left, right)

    components: dict[int, list[int]] = {}
    for index in range(len(rows)):
        components.setdefault(find(index), []).append(index)
    for indexes in components.values():
        cert_ids = sorted(str(rows[index]["cert_id"]) for index in indexes)
        observed = [rows[index]["issued_ms"] for index in indexes]
        assets = {rows[index]["issuer"] for index in indexes}
        span_ms = max(observed) - min(observed)
        risk_score = (
            sum(_severity_rank(rows[index]["severity"]) for index in indexes)
            + (len(assets) * 2)
            + (span_ms // 60)
        )
        chain_id = hashlib.sha1(",".join(cert_ids).encode("utf-8")).hexdigest()[:10]
        chain_digest = hashlib.sha256(
            (
                f"{chain_id}|{len(indexes)}|{span_ms}|{risk_score}|"
                f"{','.join(cert_ids)}"
            ).encode()
        ).hexdigest()[:12]
        for index in indexes:
            rows[index]["chain_id"] = chain_id
            rows[index]["chain_size"] = len(indexes)
            rows[index]["chain_span_ms"] = span_ms
            rows[index]["chain_risk_score"] = risk_score
            rows[index]["chain_digest"] = chain_digest


def _annotate_chain_reach(rows: list[dict]) -> None:
    chains: dict[str, dict] = {}
    for index, row in enumerate(rows):
        chain = chains.setdefault(
            row["chain_id"],
            {
                "indexes": [],
                "start_ms": row["issued_ms"],
                "end_ms": row["issued_ms"],
                "assets": set(),
                "tokens": set(),
                "risk_score": row["chain_risk_score"],
            },
        )
        chain["indexes"].append(index)
        chain["start_ms"] = min(chain["start_ms"], row["issued_ms"])
        chain["end_ms"] = max(chain["end_ms"], row["issued_ms"])
        chain["assets"].add(row["issuer"])
        chain["tokens"].update(str(row["detector"]).lower().split())

    finalized: list[tuple[str, dict]] = []
    for chain_id, chain in sorted(
        chains.items(),
        key=lambda item: (item[1]["start_ms"], item[1]["end_ms"], item[0]),
    ):
        best_score = chain["risk_score"]
        best_path = (chain_id,)
        for predecessor_id, predecessor in finalized:
            gap_ms = chain["start_ms"] - predecessor["end_ms"]
            if gap_ms <= 0 or gap_ms > 3000:
                continue
            shared_assets = len(chain["assets"] & predecessor["assets"])
            shared_tokens = len(chain["tokens"] & predecessor["tokens"])
            if shared_assets == 0 and shared_tokens == 0:
                continue
            edge_weight = (
                1
                + (2 * shared_assets)
                + shared_tokens
                + max(0, 3 - (gap_ms // 1000))
            )
            candidate_score = (
                predecessor["reach_score"] + edge_weight + chain["risk_score"]
            )
            candidate_path = predecessor["reach_path"] + (chain_id,)
            if candidate_score > best_score or (
                candidate_score == best_score and candidate_path < best_path
            ):
                best_score = candidate_score
                best_path = candidate_path
        chain["reach_score"] = best_score
        chain["reach_path"] = best_path
        chain["reach_depth"] = len(best_path) - 1
        chain["reach_digest"] = hashlib.sha256(
            (
                f"{chain_id}|{best_score}|{chain['reach_depth']}|"
                f"{','.join(best_path)}"
            ).encode()
        ).hexdigest()[:12]
        finalized.append((chain_id, chain))

    for _, chain in finalized:
        for index in chain["indexes"]:
            rows[index]["chain_reach_score"] = chain["reach_score"]
            rows[index]["chain_reach_depth"] = chain["reach_depth"]
            rows[index]["chain_reach_path"] = list(chain["reach_path"])
            rows[index]["chain_reach_digest"] = chain["reach_digest"]


def _compute_summary(events: list[dict], override_rows: list[dict] | None = None) -> dict:
    canonical = _canonicalize_events(events)
    severity_counts = {severity: 0 for severity in SEVERITY_ORDER}
    issuers: set[str] = set()
    override_rows = (
        json.loads(OVERRIDES_PATH.read_text()) if override_rows is None else override_rows
    )
    compacted_overrides = _compact_overrides(override_rows)
    signals = _compute_escalated(events, override_rows=override_rows)
    for event in canonical:
        severity = _normalize_severity(event.get("severity", ""))
        if severity in severity_counts:
            severity_counts[severity] += 1
        issuers.add(_normalize_issuer(event.get("issuer", "")))
    return {
        "schema_version": "identity-triage-v2",
        "raw_cert_count": len(events),
        "unique_cert_ids": len({str(event["cert_id"]) for event in events}),
        "total_certs": len(canonical),
        "severity_counts": severity_counts,
        "issuers": sorted(issuers),
        "escalated_count": len(signals),
        "dismissed_excluded_count": sum(
            1
            for event in canonical
            if _normalize_dismissed(event.get("dismissed", False))
            and _normalize_severity(event.get("severity", "")) in ANOMALY_SEVERITIES
        ),
        "override_excluded_count": sum(
            1
            for event in canonical
            if _normalize_severity(event.get("severity", "")) in ANOMALY_SEVERITIES
            and not _normalize_dismissed(event.get("dismissed", False))
            and _is_override_suppressed(event, compacted_overrides)
        ),
        "override_compaction_checksum": _override_compaction_checksum(compacted_overrides),
        "max_wide_pressure_score": max(
            (row["wide_pressure_score"] for row in signals),
            default=0,
        ),
        "max_pressure_index": max(
            (row["pressure_index"] for row in signals),
            default=0,
        ),
        "max_override_pressure_score": max(
            (row["override_pressure_score"] for row in signals),
            default=0,
        ),
        "chain_count": len({row["chain_id"] for row in signals}),
        "max_chain_risk_score": max(
            (row["chain_risk_score"] for row in signals),
            default=0,
        ),
        "chain_digest_checksum": hashlib.sha256(
            "|".join(row["chain_digest"] for row in signals).encode("utf-8")
        ).hexdigest(),
        "max_chain_reach_score": max(
            (row["chain_reach_score"] for row in signals),
            default=0,
        ),
        "chain_reach_digest_checksum": hashlib.sha256(
            "|".join(
                row["chain_reach_digest"] for row in signals
            ).encode("utf-8")
        ).hexdigest(),
        "signal_digest_checksum": hashlib.sha256(
            "|".join(row["signal_digest"] for row in signals).encode("utf-8")
        ).hexdigest(),
        **_escalation_ledger(signals),
    }


def _escalation_ledger(signals: list[dict]) -> dict:
    """Sequential escalation-pressure ledger per #PKI-5122/5123 and #PKI-5396.

    Carry propagates between consecutive rows in export order; the carry credit
    is ceilinged while the gap decay and chain-size debit are floored. Per the
    later #PKI-5396 ruling the pressure also couples to directed reach, gaining a
    floored chain_reach_score // 6 term that affects the critical flag only.
    """
    previous_issued_ms = None
    previous_carry_out = 0
    critical_ids: list[str] = []
    max_pressure = 0
    rows: list[str] = []
    for signal in signals:
        gap_ms = (
            0
            if previous_issued_ms is None
            else max(previous_issued_ms - signal["issued_ms"], 0)
        )
        carry_in = max(previous_carry_out - (gap_ms // 150), 0)
        pressure = (
            signal["chain_risk_score"]
            + (-(-carry_in // 3))
            + (signal["chain_reach_score"] // 6)
        )
        carry_out = min(
            carry_in + signal["chain_risk_score"] - (signal["chain_size"] // 2), 63
        )
        flag = 1 if pressure >= 21 else 0
        if flag:
            critical_ids.append(str(signal["cert_id"]))
        max_pressure = max(max_pressure, pressure)
        rows.append(f"{signal['cert_id']}|{pressure}|{flag}|{carry_out}")
        previous_issued_ms = signal["issued_ms"]
        previous_carry_out = carry_out
    return {
        "critical_escalation_ids": sorted(critical_ids),
        "critical_escalation_count": len(critical_ids),
        "max_escalation_pressure": max_pressure,
        "escalation_ledger_checksum": hashlib.sha256(
            "\n".join(rows).encode("utf-8")
        ).hexdigest(),
    }


def _compute_escalated(events: list[dict], override_rows: list[dict] | None = None) -> list[dict]:
    override_rows = (
        json.loads(OVERRIDES_PATH.read_text()) if override_rows is None else override_rows
    )
    compacted_overrides = _compact_overrides(override_rows)
    rows = []
    for event in _canonicalize_events(events):
        if not _is_signal(event):
            continue
        if _is_override_suppressed(event, compacted_overrides):
            continue
        issuer = _normalize_issuer(event.get("issuer", ""))
        severity = _normalize_severity(event.get("severity", ""))
        issued_ms = _normalize_issued_ms(event.get("issued_ms", 0))
        all_overlap_ms = _probe_overlap_ms(
            issued_ms, compacted_overrides.get((issuer, "all"), [])
        )
        severity_overlap_ms = _probe_overlap_ms(
            issued_ms, compacted_overrides.get((issuer, severity), [])
        )
        wide_all_overlap_ms = _probe_overlap_ms(
            issued_ms,
            compacted_overrides.get((issuer, "all"), []),
            lookback_ms=300,
        )
        wide_severity_overlap_ms = _probe_overlap_ms(
            issued_ms,
            compacted_overrides.get((issuer, severity), []),
            lookback_ms=300,
        )
        override_pressure_score = (all_overlap_ms // 60) + (-(-severity_overlap_ms // 62))
        wide_pressure_score = (
            (-(-wide_all_overlap_ms // 64)) + (wide_severity_overlap_ms // 66)
        )
        pressure_index = override_pressure_score + wide_pressure_score
        rows.append(
            {
                "cert_id": event["cert_id"],
                "issued_ms": issued_ms,
                "severity": severity,
                "issuer": issuer,
                "detector": _normalize_detector(event["detector"]),
                "override_pressure_score": override_pressure_score,
                "wide_pressure_score": wide_pressure_score,
                "pressure_index": pressure_index,
            }
        )
    _annotate_chains(rows)
    _annotate_chain_reach(rows)
    for row in rows:
        row["signal_digest"] = hashlib.sha1(
            (
                f"{row['cert_id']}|{row['issued_ms']}|{row['severity']}|"
                f"{row['issuer']}|{row['detector']}|{row['override_pressure_score']}|"
                f"{row['pressure_index']}|"
                f"{row['chain_id']}|{row['chain_size']}|{row['chain_span_ms']}|"
                f"{row['chain_risk_score']}|{row['chain_digest']}|"
                f"{row['chain_reach_score']}|{row['chain_reach_depth']}|"
                f"{','.join(row['chain_reach_path'])}|"
                f"{row['chain_reach_digest']}"
            ).encode()
        ).hexdigest()[:12]
    rows.sort(
        key=lambda row: (
            -row["issued_ms"],
            -_severity_rank(row["severity"]),
            -row["chain_risk_score"],
            -row["chain_reach_score"],
            -row["override_pressure_score"],
            str(row["cert_id"]),
        )
    )
    return rows


def _run_pipeline(
    pipeline: Path = PIPELINE,
    input_path: Path = INPUT_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> subprocess.CompletedProcess[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(  # noqa: PLW1510
        [
            "python3",
            str(pipeline),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _escalated_rows(path: Path = FLAGGED_PATH) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


@pytest.fixture(scope="module")
def expected() -> dict:
    """Compute every expected value independently from the operational inputs.

    Nothing here is hardcoded output: summaries, matrices, escalated rows and
    checksums are all derived from /app/data at test time, for both the primary
    and the alternate input.
    """
    events = _load_events(INPUT_PATH)
    summary = _compute_summary(events)
    escalated = _compute_escalated(events)
    alternate_events = _load_events(ALT_INPUT)
    alternate_summary = _compute_summary(alternate_events)
    alternate_escalated = _compute_escalated(alternate_events)
    return {
        **summary,
        "cert_count": len(events),
        "unique_ids": len({str(event["cert_id"]) for event in events}),
        "expected_issuer_matrix": _build_issuer_matrix(_canonicalize_events(events)),
        "expected_escalated_ids_desc": [row["cert_id"] for row in escalated],
        "expected_escalated_ms_desc": [row["issued_ms"] for row in escalated],
        "broken_pipeline_sha256": BROKEN_PIPELINE_SHA256,
        "alternate_input": str(ALT_INPUT),
        "alternate_expected": {
            **alternate_summary,
            "escalated_ids_desc": [row["cert_id"] for row in alternate_escalated],
        },
    }


@pytest.fixture(scope="module")
def dossier_text() -> str:
    return _normalize_ws(DOSSIER_PATH.read_text())


@pytest.fixture(scope="module")
def diagnosis() -> dict:
    assert DIAGNOSIS_PATH.exists(), (
        f"Missing {DIAGNOSIS_PATH}. Run: python3 {CLI} repair --output-dir /app/output"
    )
    return json.loads(DIAGNOSIS_PATH.read_text())


@pytest.fixture(scope="module")
def summary(diagnosis: dict) -> dict:
    assert SUMMARY_PATH.exists(), "missing summary.json"
    data = json.loads(SUMMARY_PATH.read_text())
    assert data == diagnosis["verified_summary"]
    return data


@pytest.fixture(scope="module")
def escalated_rows() -> list[dict]:
    assert FLAGGED_PATH.exists(), "missing escalated.jsonl"
    return _escalated_rows()


def test_override_checksum_contract_and_touching_merge():
    """Verify touching-window compaction and checksum serialization."""
    contract = SPEC_DATA["outputs"]["summary_json"]["override_checksum_serialization"]
    assert hashlib.sha256(
        contract["test_vector_payload"].encode("utf-8")
    ).hexdigest() == contract["test_vector_sha256"]
    compacted = _compact_overrides(
        [
            {
                "issuer": "edge",
                "severity_scope": "high",
                "start_ms": 100,
                "end_ms": 160,
            },
            {
                "issuer": "edge",
                "severity_scope": "high",
                "start_ms": 160,
                "end_ms": 220,
            },
        ]
    )
    assert compacted[("edge", "high")] == [(100, 220)]


def test_cli_exists():
    assert CLI.exists(), f"CLI not found at {CLI}"


def test_dossier_has_context():
    minimum = SPEC_DATA["context"]["minimum_line_count"]
    assert len(DOSSIER_PATH.read_text().splitlines()) >= minimum


def test_repair_produces_required_outputs():
    for path in (SUMMARY_PATH, MATRIX_PATH, FLAGGED_PATH, REPAIR_AUDIT_PATH):
        assert path.exists(), f"missing required output: {path}"


def test_diagnosis_schema_repaired(diagnosis: dict):
    for key in ("pipeline_status", "issues_found", "input_stats", "verified_summary", "output_paths"):
        assert key in diagnosis
    assert diagnosis["pipeline_status"] == "repaired"


def test_output_paths_exact(diagnosis: dict):
    paths = diagnosis["output_paths"]
    assert paths["summary_json"] == str(SUMMARY_PATH)
    assert paths["escalated_jsonl"] == str(FLAGGED_PATH)
    assert paths["issuer_matrix_json"] == str(MATRIX_PATH)


def test_issues_found_exactly_six_allowed_ids(diagnosis: dict):
    assert len(diagnosis["issues_found"]) == 6
    assert {item["id"] for item in diagnosis["issues_found"]} == set(REQUIRED_ISSUE_IDS)


def test_issue_item_required_fields(diagnosis: dict):
    for issue in diagnosis["issues_found"]:
        for key in ("id", "severity", "description", "resolution", "evidence"):
            assert key in issue


def test_issue_evidence(diagnosis: dict):
    original_pipeline = ORIGINAL_PIPELINE.read_text()
    issues = {item["id"]: item for item in diagnosis["issues_found"]}
    for issue_id, terms in ISSUE_EVIDENCE_TERMS.items():
        evidence = issues[issue_id]["evidence"]
        for key in ("dossier_quote", "pipeline_evidence", "repair_action"):
            assert key in evidence
            assert len(evidence[key]) >= 10
        assert len(evidence["dossier_quote"]) >= 30
        for term in terms["dossier_quote"]:
            assert term in evidence["dossier_quote"]
        for term in terms["pipeline_evidence"]:
            assert term in evidence["pipeline_evidence"]
        assert evidence["pipeline_evidence"] in original_pipeline
        for term in terms["repair_action"]:
            assert term in evidence["repair_action"]


def test_dossier_quotes_are_verbatim(diagnosis: dict, dossier_text: str):
    for issue in diagnosis["issues_found"]:
        quote = _normalize_ws(issue["evidence"]["dossier_quote"])
        assert quote in dossier_text


def test_input_stats(diagnosis: dict, expected: dict):
    stats = diagnosis["input_stats"]
    assert stats["cert_count"] == expected["cert_count"]
    assert stats["unique_cert_ids"] == expected["unique_ids"]
    assert stats["issuers"] == expected["issuers"]


def test_verified_summary_matches_independent_computation(diagnosis: dict, expected: dict):
    verified = diagnosis["verified_summary"]
    for key in (
        "schema_version",
        "raw_cert_count",
        "unique_cert_ids",
        "total_certs",
        "severity_counts",
        "issuers",
        "escalated_count",
        "dismissed_excluded_count",
        "override_excluded_count",
        "override_compaction_checksum",
        "max_override_pressure_score",
        "chain_count",
        "max_chain_risk_score",
        "chain_digest_checksum",
        "max_chain_reach_score",
        "chain_reach_digest_checksum",
        "signal_digest_checksum",
        "critical_escalation_ids",
        "critical_escalation_count",
        "max_escalation_pressure",
        "escalation_ledger_checksum",
    ):
        assert verified[key] == expected[key]
    assert list(verified["severity_counts"].keys()) == list(SEVERITY_ORDER)
    assert len(verified["chain_digest_checksum"]) == 64
    assert len(verified["chain_reach_digest_checksum"]) == 64
    assert len(verified["signal_digest_checksum"]) == 64


def test_summary_computed_from_events(summary: dict):
    assert summary == _compute_summary(_load_events(INPUT_PATH))


def test_issuer_matrix_matches_independent_computation(expected: dict):
    matrix = json.loads(MATRIX_PATH.read_text())
    assert matrix == expected["expected_issuer_matrix"]
    assert matrix == _build_issuer_matrix(_canonicalize_events(_load_events(INPUT_PATH)))


def test_escalated_computed_from_events(escalated_rows: list[dict]):
    assert escalated_rows == _compute_escalated(_load_events(INPUT_PATH))


def test_escalated_sorted_descending(escalated_rows: list[dict], expected: dict):
    assert [row["cert_id"] for row in escalated_rows] == expected["expected_escalated_ids_desc"]
    assert [row["issued_ms"] for row in escalated_rows] == expected["expected_escalated_ms_desc"]


def test_escalated_severities(escalated_rows: list[dict]):
    for row in escalated_rows:
        assert row["severity"] in ANOMALY_SEVERITIES
        assert isinstance(row["override_pressure_score"], int)
        assert len(row["chain_id"]) == 10
        assert isinstance(row["chain_size"], int)
        assert isinstance(row["chain_span_ms"], int)
        assert isinstance(row["chain_risk_score"], int)
        assert len(row["chain_digest"]) == 12
        assert isinstance(row["chain_reach_score"], int)
        assert isinstance(row["chain_reach_depth"], int)
        assert isinstance(row["chain_reach_path"], list)
        assert len(row["chain_reach_digest"]) == 12
        assert len(row["signal_digest"]) == 12


def test_escalated_jsonl_compact_format():
    for line in FLAGGED_PATH.read_text().splitlines():
        if not line.strip():
            continue
        assert ": " not in line
        parsed = json.loads(line)
        assert json.dumps(parsed, separators=(",", ":")) == line


def test_original_snapshot_preserved(expected: dict):
    assert ORIGINAL_PIPELINE.exists()
    digest = hashlib.sha256(ORIGINAL_PIPELINE.read_bytes()).hexdigest()
    assert digest == expected["broken_pipeline_sha256"]
    original = ORIGINAL_PIPELINE.read_text()
    for token in FORBIDDEN_TOKENS:
        assert token in original
    assert ".lower(" not in original


def test_pipeline_output_tracks_its_input(tmp_path_factory):
    """The repaired pipeline computes from its --input rather than emitting fixed
    values, so its output changes when the input changes. A solution that hard-coded
    results or read verifier fixtures instead of the given stream would fail this."""
    base_events = _load_events(INPUT_PATH)
    assert len(base_events) > 1

    base_dir = tmp_path_factory.mktemp("track_base")
    assert _run_pipeline(output_dir=base_dir).returncode == 0
    base_summary = json.loads((base_dir / "summary.json").read_text())

    perturbed = base_events[:-1]
    perturbed_input = tmp_path_factory.mktemp("track_in") / "events.json"
    perturbed_input.write_text(json.dumps(perturbed), encoding="utf-8")
    perturbed_dir = tmp_path_factory.mktemp("track_out")
    assert _run_pipeline(input_path=perturbed_input, output_dir=perturbed_dir).returncode == 0
    perturbed_summary = json.loads((perturbed_dir / "summary.json").read_text())

    assert perturbed_summary["raw_cert_count"] == len(perturbed)
    assert perturbed_summary["raw_cert_count"] != base_summary["raw_cert_count"]
    assert perturbed_summary != base_summary


def test_repair_runtime_does_not_read_tests_tree():
    with tempfile.TemporaryDirectory() as tmp:
        guard = Path(tmp) / "sitecustomize.py"
        guard.write_text(
            "\n".join(  # noqa: FLY002
                [
                    "import builtins",
                    "from pathlib import Path",
                    "_open = builtins.open",
                    "_text = Path.read_text",
                    "_bytes = Path.read_bytes",
                    "def _blocked(value):",
                    "    try: return '/tests' in str(Path(value).resolve())",
                    "    except Exception: return False",
                    "def guarded_open(file, *args, **kwargs):",
                    "    if _blocked(file): raise PermissionError(file)",
                    "    return _open(file, *args, **kwargs)",
                    "def guarded_text(self, *args, **kwargs):",
                    "    if _blocked(self): raise PermissionError(self)",
                    "    return _text(self, *args, **kwargs)",
                    "def guarded_bytes(self, *args, **kwargs):",
                    "    if _blocked(self): raise PermissionError(self)",
                    "    return _bytes(self, *args, **kwargs)",
                    "builtins.open = guarded_open",
                    "Path.read_text = guarded_text",
                    "Path.read_bytes = guarded_bytes",
                ]
            )
            + "\n"
        )
        out = Path(tmp) / "out"
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp
        result = subprocess.run(  # noqa: PLW1510
            [
                "python3",
                str(CLI),
                "repair",
                "--output-dir",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stderr


def test_broken_snapshot_produces_wrong_export(expected: dict):
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "export_report.py"
        out = Path(tmp) / "out"
        shutil.copy(ORIGINAL_PIPELINE, broken)
        result = _run_pipeline(pipeline=broken, output_dir=out)
        assert result.returncode == 0, result.stderr
        summary = json.loads((out / "summary.json").read_text())
        escalated = _escalated_rows(out / "escalated.jsonl")
        assert summary != _compute_summary(_load_events(INPUT_PATH))
        assert escalated != _compute_escalated(_load_events(INPUT_PATH))
        assert all(row["issued_ms"] == 0 for row in escalated)


def test_pipeline_patched():
    ast.parse(PIPELINE.read_text())
    code = _executable_text(PIPELINE.read_text())
    for token in FORBIDDEN_TOKENS:
        assert token not in code


def test_repair_audit(diagnosis: dict, expected: dict, summary: dict):
    audit = json.loads(REPAIR_AUDIT_PATH.read_text())
    code = _executable_text(PIPELINE.read_text())
    assert audit["patched_workflow"] == str(PIPELINE)
    assert audit["processing_steps"] == SPEC_DATA["repair_audit"]["processing_steps"]
    assert audit["removed_tokens"] == {token: token not in code for token in FORBIDDEN_TOKENS}
    assert all(audit["removed_tokens"].values())
    assert audit["pre_repair"]["pipeline_source_sha256"] == expected["broken_pipeline_sha256"]
    assert audit["pre_repair"]["pipeline_tokens_present"] == {token: True for token in FORBIDDEN_TOKENS}
    assert audit["post_repair"]["escalated_count"] == summary["escalated_count"]
    assert audit["post_repair"]["rerun_escalated_count"] == summary["escalated_count"]


def test_pipeline_reruns_idempotently(summary: dict, escalated_rows: list[dict], tmp_path_factory):
    rerun_dir = tmp_path_factory.mktemp("rerun")
    result = _run_pipeline(output_dir=rerun_dir)
    assert result.returncode == 0, result.stderr
    rerun_summary = json.loads((rerun_dir / "summary.json").read_text())
    rerun_escalated = _escalated_rows(rerun_dir / "escalated.jsonl")
    assert rerun_summary == summary
    assert rerun_escalated == escalated_rows


def test_patched_pipeline_supports_alternate_input(expected: dict, tmp_path_factory):
    alt_dir = tmp_path_factory.mktemp("alt")
    alt_input = Path(expected["alternate_input"])
    result = _run_pipeline(input_path=alt_input, output_dir=alt_dir)
    assert result.returncode == 0, result.stderr
    summary = json.loads((alt_dir / "summary.json").read_text())
    escalated = _escalated_rows(alt_dir / "escalated.jsonl")
    events = _load_events(alt_input)
    assert summary == _compute_summary(events)
    assert escalated == _compute_escalated(events)
    alt = expected["alternate_expected"]
    assert summary["raw_cert_count"] == alt["raw_cert_count"]
    assert summary["escalated_count"] == alt["escalated_count"]
    assert summary["dismissed_excluded_count"] == alt["dismissed_excluded_count"]
    assert summary["override_excluded_count"] == alt["override_excluded_count"]
    assert summary["override_compaction_checksum"] == alt["override_compaction_checksum"]
    assert summary["chain_count"] == alt["chain_count"]
    assert summary["max_chain_risk_score"] == alt["max_chain_risk_score"]
    assert summary["chain_digest_checksum"] == alt["chain_digest_checksum"]
    assert summary["max_chain_reach_score"] == alt[
        "max_chain_reach_score"
    ]
    assert summary["chain_reach_digest_checksum"] == alt[
        "chain_reach_digest_checksum"
    ]
    assert summary["signal_digest_checksum"] == alt[
        "signal_digest_checksum"
    ]
    assert [row["cert_id"] for row in escalated] == alt["escalated_ids_desc"]


def test_cli_diagnose_subcommand(expected: dict, dossier_text: str):
    report = OUTPUT_DIR / "diagnosis_redundant.json"
    if report.exists():
        report.unlink()
    result = subprocess.run(  # noqa: PLW1510
        [
            "python3",
            str(CLI),
            "diagnose",
            "--dossier",
            str(DOSSIER_PATH),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert report.exists(), f"diagnose failed (rc={result.returncode}): {result.stderr}"
    data = json.loads(report.read_text())
    assert data["pipeline_status"] == "diagnosed"
    assert "input_stats" in data
    assert data["input_stats"]["cert_count"] == expected["cert_count"]
    assert data["input_stats"]["unique_cert_ids"] == expected["unique_ids"]
    assert data["input_stats"]["issuers"] == expected["issuers"]
    for key in ("verified_summary", "output_paths"):
        assert key not in data
    assert {item["id"] for item in data["issues_found"]} == set(REQUIRED_ISSUE_IDS)
    for issue in data["issues_found"]:
        for key in ("id", "severity", "description", "resolution", "evidence"):
            assert key in issue
        for key in ("dossier_quote", "pipeline_evidence", "repair_action"):
            assert key in issue["evidence"]
            assert len(issue["evidence"][key]) >= 10
        quote = _normalize_ws(issue["evidence"]["dossier_quote"])
        assert quote in dossier_text


def test_diagnose_rejects_stray_input_flag(tmp_path_factory):
    """diagnose is stateless: it accepts only --dossier/--report and rejects a stray --input."""
    report = tmp_path_factory.mktemp("diag_reject") / "diagnosis.json"
    result = subprocess.run(  # noqa: PLW1510
        [
            "python3", str(CLI), "diagnose",
            "--dossier", str(DOSSIER_PATH),
            "--report", str(report),
            "--input", str(DOSSIER_PATH),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, "diagnose must reject a stray --input flag"
    assert not report.exists(), "diagnose must not write a report when given an unknown flag"


def test_repair_repatches_reset_workflow_with_custom_output_dir(
    tmp_path_factory, expected: dict
):
    custom_dir = tmp_path_factory.mktemp("custom_output")
    current = PIPELINE.read_text()
    try:
        shutil.copy(ORIGINAL_PIPELINE, PIPELINE)
        result = subprocess.run(  # noqa: PLW1510
            ["python3", str(CLI), "repair", "--output-dir", str(custom_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        repaired_source = PIPELINE.read_text()
        assert 'event["issued_at"]' not in repaired_source
        summary = json.loads((custom_dir / "summary.json").read_text())
        escalated = _escalated_rows(custom_dir / "escalated.jsonl")
        diagnosis = json.loads((custom_dir / "diagnosis.json").read_text())
        assert summary == _compute_summary(_load_events(INPUT_PATH))
        assert escalated == _compute_escalated(_load_events(INPUT_PATH))
        assert diagnosis["output_paths"]["summary_json"] == str(custom_dir / "summary.json")
        assert diagnosis["output_paths"]["escalated_jsonl"] == str(custom_dir / "escalated.jsonl")
        assert diagnosis["output_paths"]["issuer_matrix_json"] == str(custom_dir / "issuer_matrix.json")
        assert summary["escalated_count"] == expected["escalated_count"]
    finally:
        PIPELINE.write_text(current)


def test_dedupe_tie_break_severity_and_detector():
    events = [
        {
            "cert_id": "x1",
            "issued_ms": 100,
            "severity": "medium",
            "issuer": "edge",
            "detector": "aaa",
            "dismissed": False,
        },
        {
            "cert_id": "x1",
            "issued_ms": 100,
            "severity": "HIGH",
            "issuer": "edge",
            "detector": "bbb",
            "dismissed": False,
        },
        {
            "cert_id": "x1",
            "issued_ms": 100,
            "severity": "high",
            "issuer": "edge",
            "detector": "zzz",
            "dismissed": False,
        },
    ]
    canonical = _canonicalize_events(events)
    assert len(canonical) == 1
    assert canonical[0]["severity"] == "high"
    assert canonical[0]["detector"] == "zzz"


def test_dismissed_string_normalization_excludes_signal():
    events = [
        {
            "cert_id": "m1",
            "issued_ms": 100,
            "severity": "critical",
            "issuer": "beta",
            "detector": "x",
            "dismissed": "true",
        },
        {
            "cert_id": "m2",
            "issued_ms": 110,
            "severity": "high",
            "issuer": "beta",
            "detector": "y",
            "dismissed": "1",
        },
        {
            "cert_id": "m3",
            "issued_ms": 120,
            "severity": "critical",
            "issuer": "beta",
            "detector": "z",
            "dismissed": False,
        },
    ]
    escalated = _compute_escalated(events)
    assert [row["cert_id"] for row in escalated] == ["m3"]


def test_escalated_sort_tie_breaks_by_severity_then_cert_id():
    events = [
        {
            "cert_id": "c2",
            "issued_ms": 500,
            "severity": "critical",
            "issuer": "m",
            "detector": "c2",
            "dismissed": False,
        },
        {
            "cert_id": "h1",
            "issued_ms": 500,
            "severity": "high",
            "issuer": "m",
            "detector": "h1",
            "dismissed": False,
        },
        {
            "cert_id": "c1",
            "issued_ms": 500,
            "severity": "critical",
            "issuer": "m",
            "detector": "c1",
            "dismissed": False,
        },
    ]
    escalated = _compute_escalated(events)
    assert [row["cert_id"] for row in escalated] == ["c1", "c2", "h1"]


def test_pipeline_coerces_issued_ms_and_normalizes_outputs(tmp_path_factory):
    events = [
        {
            "cert_id": "p1",
            "issued_ms": " 200 ",
            "severity": " CRITICAL ",
            "issuer": " Core ",
            "detector": " first   detector ",
            "dismissed": "no",
        },
        {
            "cert_id": "p2",
            "issued_ms": "not-a-number",
            "severity": "high",
            "issuer": "core",
            "detector": "second",
            "dismissed": False,
        },
        {
            "cert_id": "p3",
            "issued_ms": 150,
            "severity": "high",
            "issuer": "core",
            "detector": "dismissed row",
            "dismissed": "yes",
        },
    ]
    input_path = tmp_path_factory.mktemp("coerce") / "events.json"
    input_path.write_text(json.dumps(events))
    out_dir = tmp_path_factory.mktemp("coerce_out")
    result = _run_pipeline(input_path=input_path, output_dir=out_dir)
    assert result.returncode == 0, result.stderr

    summary = json.loads((out_dir / "summary.json").read_text())
    escalated = _escalated_rows(out_dir / "escalated.jsonl")
    matrix = json.loads((out_dir / "issuer_matrix.json").read_text())

    assert summary["issuers"] == ["core"]
    assert summary["escalated_count"] == 2
    assert summary["dismissed_excluded_count"] == 1
    assert [row["cert_id"] for row in escalated] == ["p1", "p2"]
    assert [row["issued_ms"] for row in escalated] == [200, 0]
    assert escalated[0]["detector"] == "first detector"
    assert matrix == {"core": {"critical": 1, "high": 2, "medium": 0, "low": 0}}


def test_pipeline_dedupe_tie_break_prefers_non_dismissed_then_detector(tmp_path_factory):
    events = [
        {
            "cert_id": "d1",
            "issued_ms": 100,
            "severity": "high",
            "issuer": "m",
            "detector": "zzz",
            "dismissed": "yes",
        },
        {
            "cert_id": "d1",
            "issued_ms": 100,
            "severity": "high",
            "issuer": "m",
            "detector": "aaa",
            "dismissed": False,
        },
        {
            "cert_id": "d1",
            "issued_ms": 100,
            "severity": "high",
            "issuer": "m",
            "detector": "bbb",
            "dismissed": "0",
        },
    ]
    input_path = tmp_path_factory.mktemp("dedupe") / "events.json"
    input_path.write_text(json.dumps(events))
    out_dir = tmp_path_factory.mktemp("dedupe_out")
    result = _run_pipeline(input_path=input_path, output_dir=out_dir)
    assert result.returncode == 0, result.stderr

    escalated = _escalated_rows(out_dir / "escalated.jsonl")
    summary = json.loads((out_dir / "summary.json").read_text())

    assert summary["total_certs"] == 1
    assert summary["dismissed_excluded_count"] == 0
    assert [row["cert_id"] for row in escalated] == ["d1"]
    assert escalated[0]["detector"] == "bbb"


def test_override_source_path_affects_output(tmp_path_factory):
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        base_dir = tmp_path_factory.mktemp("base_override")
        base_result = _run_pipeline(output_dir=base_dir)
        assert base_result.returncode == 0, base_result.stderr
        base_summary = json.loads((base_dir / "summary.json").read_text())
        base_escalated = _escalated_rows(base_dir / "escalated.jsonl")

        OVERRIDES_PATH.write_text("[]\n")
        no_override_dir = tmp_path_factory.mktemp("no_override")
        no_override_result = _run_pipeline(output_dir=no_override_dir)
        assert no_override_result.returncode == 0, no_override_result.stderr
        no_override_summary = json.loads((no_override_dir / "summary.json").read_text())
        no_override_escalated = _escalated_rows(no_override_dir / "escalated.jsonl")

        assert base_summary["override_excluded_count"] > 0
        assert no_override_summary["override_excluded_count"] == 0
        assert (
            base_summary["override_compaction_checksum"]
            != no_override_summary["override_compaction_checksum"]
        )
        assert len(no_override_escalated) > len(base_escalated)
    finally:
        OVERRIDES_PATH.write_text(original_overrides)


def test_override_compaction_and_scope_exercised(tmp_path_factory):
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        override_rows = [
            {"issuer": "edge", "severity_scope": "high", "start_ms": 100, "end_ms": 160},
            {"issuer": "edge", "severity_scope": "high", "start_ms": 160, "end_ms": 200},
            {"issuer": "edge", "severity_scope": "all", "start_ms": 220, "end_ms": 260},
            {"issuer": "edge", "severity_scope": "debug", "start_ms": 0, "end_ms": 1},
        ]
        OVERRIDES_PATH.write_text(json.dumps(override_rows, indent=2) + "\n")
        events = [
            {
                "cert_id": "o1",
                "issued_ms": 120,
                "severity": "high",
                "issuer": "edge",
                "detector": "silenced high",
                "dismissed": False,
            },
            {
                "cert_id": "o2",
                "issued_ms": 120,
                "severity": "critical",
                "issuer": "edge",
                "detector": "kept critical",
                "dismissed": False,
            },
            {
                "cert_id": "o3",
                "issued_ms": 230,
                "severity": "critical",
                "issuer": "edge",
                "detector": "silenced all",
                "dismissed": False,
            },
            {
                "cert_id": "o4",
                "issued_ms": 280,
                "severity": "high",
                "issuer": "edge",
                "detector": "kept high",
                "dismissed": False,
            },
        ]
        input_path = tmp_path_factory.mktemp("override_scope") / "events.json"
        input_path.write_text(json.dumps(events))
        out_dir = tmp_path_factory.mktemp("override_scope_out")
        result = _run_pipeline(input_path=input_path, output_dir=out_dir)
        assert result.returncode == 0, result.stderr

        summary = json.loads((out_dir / "summary.json").read_text())
        escalated = _escalated_rows(out_dir / "escalated.jsonl")
        assert summary["override_excluded_count"] == 2
        assert [row["cert_id"] for row in escalated] == ["o4", "o2"]
    finally:
        OVERRIDES_PATH.write_text(original_overrides)


def test_chain_correlation_is_transitive_across_console_certs(tmp_path_factory):
    """Require full connected components rather than direct-neighbor groups."""
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        OVERRIDES_PATH.write_text("[]\n")
        events = [
            {
                "cert_id": "c1",
                "issued_ms": 100,
                "severity": "critical",
                "issuer": "edge",
                "detector": "alpha beta one",
                "dismissed": False,
            },
            {
                "cert_id": "c2",
                "issued_ms": 250,
                "severity": "high",
                "issuer": "core",
                "detector": "alpha beta two",
                "dismissed": False,
            },
            {
                "cert_id": "c3",
                "issued_ms": 400,
                "severity": "high",
                "issuer": "core",
                "detector": "gamma delta",
                "dismissed": False,
            },
        ]
        input_path = tmp_path_factory.mktemp("chain") / "events.json"
        input_path.write_text(json.dumps(events))
        out_dir = tmp_path_factory.mktemp("chain_out")
        result = _run_pipeline(input_path=input_path, output_dir=out_dir)
        assert result.returncode == 0, result.stderr
        rows = _escalated_rows(out_dir / "escalated.jsonl")
        assert {row["chain_id"] for row in rows} == {rows[0]["chain_id"]}
        assert {row["chain_size"] for row in rows} == {3}
        assert {row["chain_span_ms"] for row in rows} == {300}
        assert {row["chain_risk_score"] for row in rows} == {19}
        summary = json.loads((out_dir / "summary.json").read_text())
        assert summary["chain_count"] == 1
        assert summary["max_chain_risk_score"] == 19
    finally:
        OVERRIDES_PATH.write_text(original_overrides)


def test_chain_reach_propagates_over_strongest_directed_path(tmp_path_factory):
    """Verify strongest-path dynamic programming across chain nodes."""
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        OVERRIDES_PATH.write_text("[]\n")
        events = [
            {
                "cert_id": "i1",
                "issued_ms": 100,
                "severity": "critical",
                "issuer": "edge",
                "detector": "alpha one",
                "dismissed": False,
            },
            {
                "cert_id": "i2",
                "issued_ms": 1000,
                "severity": "critical",
                "issuer": "edge",
                "detector": "beta two",
                "dismissed": False,
            },
            {
                "cert_id": "i3",
                "issued_ms": 2000,
                "severity": "critical",
                "issuer": "core",
                "detector": "beta gamma",
                "dismissed": False,
            },
        ]
        input_path = tmp_path_factory.mktemp("reach") / "events.json"
        input_path.write_text(json.dumps(events))
        out_dir = tmp_path_factory.mktemp("reach_out")
        result = _run_pipeline(input_path=input_path, output_dir=out_dir)
        assert result.returncode == 0, result.stderr
        rows = {
            row["cert_id"]: row
            for row in _escalated_rows(out_dir / "escalated.jsonl")
        }
        assert rows["i1"]["chain_reach_score"] == 6
        assert rows["i2"]["chain_reach_score"] == 18
        assert rows["i3"]["chain_reach_score"] == 28
        assert rows["i3"]["chain_reach_depth"] == 2
        assert rows["i3"]["chain_reach_path"] == [
            rows["i1"]["chain_id"],
            rows["i2"]["chain_id"],
            rows["i3"]["chain_id"],
        ]
        summary = json.loads((out_dir / "summary.json").read_text())
        assert summary["max_chain_reach_score"] == 28
    finally:
        OVERRIDES_PATH.write_text(original_overrides)


def test_escalation_ledger_credit_is_ceilinged(summary: dict):
    """The escalation carry credit rounds UP; a floored credit yields a different ledger."""
    signals = _compute_escalated(_load_events(INPUT_PATH))
    assert summary["escalation_ledger_checksum"] == _escalation_ledger(signals)[
        "escalation_ledger_checksum"
    ]
    # Recompute with a floored credit -- the shipped data is tuned so they differ.
    prev_ms, prev_out, rows = None, 0, []
    for signal in signals:
        gap = 0 if prev_ms is None else max(prev_ms - signal["issued_ms"], 0)
        carry_in = max(prev_out - (gap // 150), 0)
        pressure = signal["chain_risk_score"] + (carry_in // 3) + (signal["chain_reach_score"] // 6)
        carry_out = min(carry_in + signal["chain_risk_score"] - (signal["chain_size"] // 2), 63)
        rows.append(f"{signal['cert_id']}|{pressure}|{1 if pressure >= 21 else 0}|{carry_out}")
        prev_ms, prev_out = signal["issued_ms"], carry_out
    floored = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    assert summary["escalation_ledger_checksum"] != floored


def test_escalation_pressure_couples_chain_reach(summary: dict):
    """#PKI-5396: escalation_pressure gains a floored chain_reach_score // 6 term.

    A ledger that reads chain risk and carry alone (the stale #PKI-5122 formula,
    omitting the reach coupling) yields a different checksum on the shipped data,
    so the reach term is load-bearing rather than cosmetic.
    """
    signals = _compute_escalated(_load_events(INPUT_PATH))
    assert summary["escalation_ledger_checksum"] == _escalation_ledger(signals)[
        "escalation_ledger_checksum"
    ]
    prev_ms, prev_out, rows = None, 0, []
    for signal in signals:
        gap = 0 if prev_ms is None else max(prev_ms - signal["issued_ms"], 0)
        carry_in = max(prev_out - (gap // 150), 0)
        # Omit the PKI-5396 reach term to prove it changes the ledger.
        pressure = signal["chain_risk_score"] + (-(-carry_in // 3))
        carry_out = min(carry_in + signal["chain_risk_score"] - (signal["chain_size"] // 2), 63)
        rows.append(f"{signal['cert_id']}|{pressure}|{1 if pressure >= 21 else 0}|{carry_out}")
        prev_ms, prev_out = signal["issued_ms"], carry_out
    no_reach = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    assert summary["escalation_ledger_checksum"] != no_reach


# --- Host containment: persistence and escalation left by the sync automation ---

ROOT_AUTH_KEYS = Path("/root/.ssh/authorized_keys")
SUDOERS_DROPIN = Path("/etc/sudoers.d/casync")
SIGNING_KEY = Path("/app/secrets/cert_signing.key")
SENTINEL_KEY_MARKER = "ops-oncall@bastion"


def test_sentinel_ssh_key_survives_containment():
    """Containment revokes the rogue key WITHOUT destroying legitimate access.

    root's authorized_keys also holds the ops on-call bastion key. Truncating or rewriting
    the file removes the rogue entry but locks responders out of the control plane, so the sentinel
    must still be present and the file still mode 0600.
    """
    assert ROOT_AUTH_KEYS.exists(), "authorized_keys was deleted; on-call access lost"
    text = ROOT_AUTH_KEYS.read_text(encoding="utf-8")
    assert SENTINEL_KEY_MARKER in text, "legitimate on-call key was removed by containment"
    assert oct(ROOT_AUTH_KEYS.stat().st_mode)[-3:] == "600"


def test_rogue_sync_key_revoked():
    """The sync automation's SSH persistence key is removed from root's authorized_keys."""
    if ROOT_AUTH_KEYS.exists():
        assert "casync-automation" not in ROOT_AUTH_KEYS.read_text()


def test_sudoers_escalation_removed():
    """The passwordless sudoers drop-in for svc-casync is deleted entirely, not narrowed."""
    assert not SUDOERS_DROPIN.exists()


def test_cert_signing_key_locked_down():
    """The stream cert-signing key is retained but restricted to root-only 0600."""
    import pwd

    assert SIGNING_KEY.exists(), "the control plane still needs the signing key in place"
    info = SIGNING_KEY.stat()
    assert (info.st_mode & 0o777) == 0o600
    assert info.st_uid == pwd.getpwnam("root").pw_uid
    assert info.st_gid == pwd.getpwnam("root").pw_gid
