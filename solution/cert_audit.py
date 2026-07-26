"""Diagnostic and repair CLI for Certwarden signal workflow."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

EVENTS_PATH = Path("/app/data/events.json")
PIPELINE_PATH = Path("/app/workflow/export_report.py")
ORIGINAL_PIPELINE = Path("/app/workflow/.export_report.original")
SPEC_PATH = Path("/app/docs/report_spec.json")
FORBIDDEN_TOKENS = ('event["issued_at"]', 'severity == "critical"')

ISSUE_META = {
    "wrong_source_field": {
        "severity": "critical",
        "description": "Signal rows use issued_at instead of issued_ms.",
        "resolution": "Use issued_ms when emitting signal rows.",
    },
    "risk_threshold_filter": {
        "severity": "critical",
        "description": "Workflow escalates only exact critical rows.",
        "resolution": "Include high and critical severities in escalated export.",
    },
    "recency_order": {
        "severity": "high",
        "description": "Signals are sorted oldest-first.",
        "resolution": "Sort signals by issued_ms descending (reverse=True).",
    },
    "risk_class_normalization": {
        "severity": "high",
        "description": "Severity aliases are not normalized to lowercase.",
        "resolution": "Normalize severity with .lower() before filtering.",
    },
    "dedupe_event": {
        "severity": "high",
        "description": "Duplicate cert_id rows are exported multiple times.",
        "resolution": "dedupe cert_id rows keeping the highest issued_ms before export.",
    },
    "benign_filter": {
        "severity": "high",
        "description": "Muted rows appear in escalated export.",
        "resolution": "Exclude dismissed rows from escalated export.",
    },
}


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text())


def load_events(path: Path = EVENTS_PATH) -> list[dict]:
    return json.loads(path.read_text())


def input_stats(events: list[dict]) -> dict:
    issuers = sorted({str(event.get("issuer", "")).strip().lower() for event in events})
    return {
        "cert_count": len(events),
        "unique_cert_ids": len({str(event["cert_id"]) for event in events}),
        "issuers": issuers,
    }


def pre_repair_audit() -> dict:
    source_bytes = ORIGINAL_PIPELINE.read_bytes()
    source = source_bytes.decode("utf-8")
    return {
        "pipeline_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "pipeline_tokens_present": {token: token in source for token in FORBIDDEN_TOKENS},
    }


def _line_contains_all(line: str, terms: list[str]) -> bool:
    return all(term in line for term in terms)


def find_dossier_quote(dossier_text: str, terms: list[str]) -> str:
    normalized = _normalize_ws(dossier_text)
    candidates: list[str] = []
    for line in dossier_text.splitlines():
        stripped = line.strip()
        if len(stripped) < 30 or not _line_contains_all(stripped, terms):
            continue
        if _normalize_ws(stripped) in normalized:
            candidates.append(stripped)
    if not candidates:
        raise ValueError(f"no dossier quote found for terms {terms}")
    return max(candidates, key=len)


def find_pipeline_evidence(original_pipeline: str, terms: list[str]) -> str:
    for line in original_pipeline.splitlines():
        stripped = line.strip()
        if stripped and _line_contains_all(stripped, terms):
            return stripped
    if all(term in original_pipeline for term in terms):
        for line in original_pipeline.splitlines():
            if any(term in line for term in terms):
                return line.strip()
    raise ValueError(f"no pipeline evidence found for terms {terms}")


def build_repair_action(issue_id: str, terms: list[str]) -> str:
    templates = {
        "wrong_source_field": "Use issued_ms when emitting signal rows.",
        "risk_threshold_filter": "Include high and critical rows in signal export.",
        "recency_order": "Sort with reverse=True on issued_ms for recency-first ordering.",
        "risk_class_normalization": "Normalize severity values using .lower() in canonicalization.",
        "dedupe_event": "dedupe cert_id rows keeping the highest issued_ms before export.",
        "benign_filter": "Exclude dismissed=true rows from escalated signal export.",
    }
    action = templates[issue_id]
    for term in terms:
        if term not in action:
            action = f"{action} ({term})"
    return action


def build_issues_from_sources(dossier_text: str, original_pipeline: str, spec: dict) -> list[dict]:
    evidence_spec = spec["diagnosis_report"]["issues_found_item"]["evidence"][
        "required_terms_by_issue"
    ]
    allowed_ids = spec["diagnosis_report"]["issues_found_item"]["allowed_ids"]
    issues = []
    for issue_id in allowed_ids:
        terms = evidence_spec[issue_id]
        meta = ISSUE_META[issue_id]
        issues.append(
            {
                "id": issue_id,
                "severity": meta["severity"],
                "description": meta["description"],
                "resolution": meta["resolution"],
                "evidence": {
                    "dossier_quote": find_dossier_quote(dossier_text, terms["dossier_quote"]),
                    "pipeline_evidence": find_pipeline_evidence(
                        original_pipeline, terms["pipeline_evidence"]
                    ),
                    "repair_action": build_repair_action(issue_id, terms["repair_action"]),
                },
            }
        )
    return issues


WORKFLOW_DOCSTRING_BROKEN = (
    '"""Broken Certwarden signal workflow used for repair task."""'
)
WORKFLOW_DOCSTRING_REPAIRED = (
    '"""Export corrected Certwarden summary and signal rows."""'
)

# Corrected processing core spliced into the frozen workflow head during repair.
REPAIRED_CORE = 'ANOMALY_SEVERITIES = {"high", "critical"}\nSEVERITY_ORDER = ("critical", "high", "medium", "low")\nSEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}\nOVERRIDES_PATH = Path("/app/data/dismissal_overrides.json")\nSUPPORTED_OVERRIDE_SCOPES = {"all", "high", "critical"}\n\n\ndef load_events(path: Path) -> list[dict]:\n    return json.loads(path.read_text())\n\n\ndef load_overrides(path: Path = OVERRIDES_PATH) -> list[dict]:\n    return json.loads(path.read_text())\n\n\ndef _normalize_severity(value: object) -> str:\n    return str(value if value is not None else "").strip().lower()\n\n\ndef _normalize_issuer(value: object) -> str:\n    return str(value if value is not None else "").strip().lower()\n\n\ndef _normalize_issued_ms(value: object) -> int:\n    if isinstance(value, bool):\n        return int(value)\n    if isinstance(value, int):\n        return value\n    if isinstance(value, float):\n        return int(value)\n    if isinstance(value, str):\n        text = value.strip()\n        try:\n            return int(text)\n        except ValueError:\n            return 0\n    return 0\n\n\ndef _normalize_detector(value: object) -> str:\n    return " ".join(str(value if value is not None else "").split())\n\n\ndef _normalize_override_scope(value: object) -> str:\n    normalized = str(value if value is not None else "").strip().lower()\n    return normalized if normalized in SUPPORTED_OVERRIDE_SCOPES else ""\n\n\ndef _normalize_dismissed(value: object) -> bool:\n    if isinstance(value, bool):\n        return value\n    if isinstance(value, str):\n        return value.strip().lower() in {"true", "1", "yes"}\n    return bool(value)\n\n\ndef _severity_rank(severity: str) -> int:\n    return SEVERITY_RANK.get(severity, 0)\n\n\ndef canonicalize_events(events: list[dict]) -> list[dict]:\n    deduped: dict[str, dict] = {}\n    for event in events:\n        normalized = dict(event)\n        normalized["issued_ms"] = _normalize_issued_ms(normalized.get("issued_ms", 0))\n        normalized["severity"] = _normalize_severity(normalized.get("severity", ""))\n        normalized["issuer"] = _normalize_issuer(normalized.get("issuer", ""))\n        normalized["dismissed"] = _normalize_dismissed(normalized.get("dismissed", False))\n        normalized["detector"] = _normalize_detector(normalized.get("detector", ""))\n        cert_id = str(normalized["cert_id"])\n        current = deduped.get(cert_id)\n        if current is None:\n            deduped[cert_id] = normalized\n            continue\n        replace = False\n        if normalized["issued_ms"] > current["issued_ms"]:\n            replace = True\n        elif normalized["issued_ms"] == current["issued_ms"]:\n            if _severity_rank(normalized["severity"]) > _severity_rank(current["severity"]):\n                replace = True\n            elif _severity_rank(normalized["severity"]) == _severity_rank(current["severity"]):\n                if int(_normalize_dismissed(normalized.get("dismissed", False))) < int(\n                    _normalize_dismissed(current.get("dismissed", False))\n                ):\n                    replace = True\n                elif int(_normalize_dismissed(normalized.get("dismissed", False))) == int(\n                    _normalize_dismissed(current.get("dismissed", False))\n                ):\n                    if _normalize_detector(normalized.get("detector", "")) > _normalize_detector(\n                        current.get("detector", "")\n                    ):\n                        replace = True\n                    elif _normalize_detector(normalized.get("detector", "")) == _normalize_detector(\n                        current.get("detector", "")\n                    ):\n                        if _normalize_issuer(\n                            normalized.get("issuer", "")\n                        ) > _normalize_issuer(current.get("issuer", "")):\n                            replace = True\n        if replace:\n            deduped[cert_id] = normalized\n    return sorted(deduped.values(), key=lambda row: row["issued_ms"])\n\n\ndef is_signal(event: dict) -> bool:\n    if _normalize_dismissed(event.get("dismissed", False)):\n        return False\n    return _normalize_severity(event.get("severity", "")) in ANOMALY_SEVERITIES\n\n\ndef build_issuer_matrix(events: list[dict]) -> dict[str, dict[str, int]]:\n    matrix: dict[str, dict[str, int]] = {}\n    for event in events:\n        issuer = _normalize_issuer(event.get("issuer", ""))\n        severity = _normalize_severity(event.get("severity", ""))\n        matrix.setdefault(issuer, {name: 0 for name in SEVERITY_ORDER})\n        if severity in matrix[issuer]:\n            matrix[issuer][severity] += 1\n    return {issuer: matrix[issuer] for issuer in sorted(matrix)}\n\n\ndef _compact_overrides(\n    rows: list[dict],\n) -> dict[tuple[str, str], list[tuple[int, int]]]:\n    by_key: dict[tuple[str, str], list[tuple[int, int]]] = {}\n    for row in rows:\n        issuer = _normalize_issuer(row.get("issuer", ""))\n        scope = _normalize_override_scope(row.get("severity_scope", ""))\n        if not scope:\n            continue\n        start_ms = _normalize_issued_ms(row.get("start_ms", 0))\n        end_ms = _normalize_issued_ms(row.get("end_ms", 0))\n        if end_ms <= start_ms:\n            continue\n        by_key.setdefault((issuer, scope), []).append((start_ms, end_ms))\n\n    compacted: dict[tuple[str, str], list[tuple[int, int]]] = {}\n    for key, intervals in by_key.items():\n        merged: list[list[int]] = []\n        for start_ms, end_ms in sorted(intervals):\n            if not merged or start_ms > merged[-1][1]:\n                merged.append([start_ms, end_ms])\n            else:\n                merged[-1][1] = max(merged[-1][1], end_ms)\n        compacted[key] = [(start_ms, end_ms) for start_ms, end_ms in merged]\n    return compacted\n\n\ndef _is_override_suppressed(\n    event: dict,\n    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]],\n) -> bool:\n    issuer = _normalize_issuer(event.get("issuer", ""))\n    severity = _normalize_severity(event.get("severity", ""))\n    issued_ms = _normalize_issued_ms(event.get("issued_ms", 0))\n    for scope in ("all", severity):\n        for start_ms, end_ms in compacted_overrides.get((issuer, scope), []):\n            if start_ms <= issued_ms < end_ms:\n                return True\n    return False\n\n\ndef _override_compaction_checksum(\n    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]]\n) -> str:\n    return hashlib.sha256(\n        "\\n".join(\n            f"{issuer}|{scope}|{start_ms}|{end_ms}"\n            for issuer, scope in sorted(compacted_overrides)\n            for start_ms, end_ms in compacted_overrides[(issuer, scope)]\n        ).encode("utf-8")\n    ).hexdigest()\n\n\ndef _probe_overlap_ms(\n    issued_ms: int,\n    spans: list[tuple[int, int]],\n    lookback_ms: int = 120,\n) -> int:\n    probe_start = issued_ms - lookback_ms\n    probe_end = issued_ms + 1\n    total = 0\n    for start_ms, end_ms in spans:\n        overlap_start = max(probe_start, start_ms)\n        overlap_end = min(probe_end, end_ms)\n        if overlap_end > overlap_start:\n            total += overlap_end - overlap_start\n    return total\n\n\ndef _annotate_chains(signals: list[dict]) -> None:\n    parent = list(range(len(signals)))\n\n    def find(index: int) -> int:\n        while parent[index] != index:\n            parent[index] = parent[parent[index]]\n            index = parent[index]\n        return index\n\n    def union(left: int, right: int) -> None:\n        left_root, right_root = find(left), find(right)\n        if left_root != right_root:\n            parent[max(left_root, right_root)] = min(left_root, right_root)\n\n    detector_tokens = [\n        set(str(row["detector"]).lower().split()) for row in signals\n    ]\n    for left in range(len(signals)):\n        for right in range(left + 1, len(signals)):\n            if abs(signals[left]["issued_ms"] - signals[right]["issued_ms"]) > 600:\n                continue\n            same_asset = (\n                signals[left]["issuer"] == signals[right]["issuer"]\n            )\n            shared_detector_tokens = len(\n                detector_tokens[left] & detector_tokens[right]\n            )\n            if same_asset or shared_detector_tokens >= 2:\n                union(left, right)\n\n    components: dict[int, list[int]] = {}\n    for index in range(len(signals)):\n        components.setdefault(find(index), []).append(index)\n    for indexes in components.values():\n        cert_ids = sorted(str(signals[index]["cert_id"]) for index in indexes)\n        observed = [signals[index]["issued_ms"] for index in indexes]\n        assets = {signals[index]["issuer"] for index in indexes}\n        span_ms = max(observed) - min(observed)\n        risk_score = (\n            sum(_severity_rank(signals[index]["severity"]) for index in indexes)\n            + (len(assets) * 2)\n            + (span_ms // 60)\n        )\n        chain_id = hashlib.sha1(",".join(cert_ids).encode("utf-8")).hexdigest()[:10]\n        chain_digest = hashlib.sha256(\n            (\n                f"{chain_id}|{len(indexes)}|{span_ms}|{risk_score}|"\n                f"{\',\'.join(cert_ids)}"\n            ).encode("utf-8")\n        ).hexdigest()[:12]\n        for index in indexes:\n            signals[index]["chain_id"] = chain_id\n            signals[index]["chain_size"] = len(indexes)\n            signals[index]["chain_span_ms"] = span_ms\n            signals[index]["chain_risk_score"] = risk_score\n            signals[index]["chain_digest"] = chain_digest\n\n\ndef _annotate_chain_reach(signals: list[dict]) -> None:\n    chains: dict[str, dict] = {}\n    for index, row in enumerate(signals):\n        chain = chains.setdefault(\n            row["chain_id"],\n            {\n                "indexes": [],\n                "start_ms": row["issued_ms"],\n                "end_ms": row["issued_ms"],\n                "assets": set(),\n                "tokens": set(),\n                "risk_score": row["chain_risk_score"],\n            },\n        )\n        chain["indexes"].append(index)\n        chain["start_ms"] = min(chain["start_ms"], row["issued_ms"])\n        chain["end_ms"] = max(chain["end_ms"], row["issued_ms"])\n        chain["assets"].add(row["issuer"])\n        chain["tokens"].update(str(row["detector"]).lower().split())\n\n    ordered = sorted(\n        chains.items(),\n        key=lambda item: (item[1]["start_ms"], item[1]["end_ms"], item[0]),\n    )\n    finalized: list[tuple[str, dict]] = []\n    for chain_id, chain in ordered:\n        best_score = chain["risk_score"]\n        best_path = (chain_id,)\n        for predecessor_id, predecessor in finalized:\n            gap_ms = chain["start_ms"] - predecessor["end_ms"]\n            if gap_ms <= 0 or gap_ms > 3000:\n                continue\n            shared_assets = len(chain["assets"] & predecessor["assets"])\n            shared_tokens = len(chain["tokens"] & predecessor["tokens"])\n            if shared_assets == 0 and shared_tokens == 0:\n                continue\n            edge_weight = (\n                1\n                + (2 * shared_assets)\n                + shared_tokens\n                + max(0, 3 - (gap_ms // 1000))\n            )\n            candidate_score = (\n                predecessor["reach_score"] + edge_weight + chain["risk_score"]\n            )\n            candidate_path = predecessor["reach_path"] + (chain_id,)\n            if candidate_score > best_score or (\n                candidate_score == best_score and candidate_path < best_path\n            ):\n                best_score = candidate_score\n                best_path = candidate_path\n        chain["reach_score"] = best_score\n        chain["reach_path"] = best_path\n        chain["reach_depth"] = len(best_path) - 1\n        chain["reach_digest"] = hashlib.sha256(\n            (\n                f"{chain_id}|{best_score}|{chain[\'reach_depth\']}|"\n                f"{\',\'.join(best_path)}"\n            ).encode("utf-8")\n        ).hexdigest()[:12]\n        finalized.append((chain_id, chain))\n\n    for chain_id, chain in finalized:\n        for index in chain["indexes"]:\n            signals[index]["chain_reach_score"] = chain["reach_score"]\n            signals[index]["chain_reach_depth"] = chain["reach_depth"]\n            signals[index]["chain_reach_path"] = list(\n                chain["reach_path"]\n            )\n            signals[index]["chain_reach_digest"] = chain[\n                "reach_digest"\n            ]\n\n\ndef _annotate_chain_influence(rows):\n    chains = {}\n    for row in rows:\n        chain = chains.setdefault(\n            row["chain_id"],\n            {\n                "start_ms": row["issued_ms"],\n                "end_ms": row["issued_ms"],\n                "assets": set(),\n                "tokens": set(),\n                "risk": row["chain_risk_score"],\n            },\n        )\n        chain["start_ms"] = min(chain["start_ms"], row["issued_ms"])\n        chain["end_ms"] = max(chain["end_ms"], row["issued_ms"])\n        chain["assets"].add(row["issuer"])\n        chain["tokens"].update(str(row["detector"]).lower().split())\n    order = sorted(chains)\n    neighbors = {chain_id: [] for chain_id in order}\n    for left_pos in range(len(order)):\n        for right_pos in range(left_pos + 1, len(order)):\n            left = chains[order[left_pos]]\n            right = chains[order[right_pos]]\n            gap_ms = max(\n                0,\n                max(left["start_ms"], right["start_ms"])\n                - min(left["end_ms"], right["end_ms"]),\n            )\n            if gap_ms > 3000:\n                continue\n            shared_assets = len(left["assets"] & right["assets"])\n            shared_tokens = len(left["tokens"] & right["tokens"])\n            if shared_assets == 0 and shared_tokens == 0:\n                continue\n            weight = 1 + (2 * shared_assets) + shared_tokens\n            neighbors[order[left_pos]].append((order[right_pos], weight))\n            neighbors[order[right_pos]].append((order[left_pos], weight))\n    influence = {chain_id: chains[chain_id]["risk"] for chain_id in order}\n    rounds = 0\n    while True:\n        updated = {}\n        for chain_id in order:\n            best = 0\n            for neighbor_id, weight in neighbors[chain_id]:\n                best = max(best, influence[neighbor_id] + weight)\n            updated[chain_id] = chains[chain_id]["risk"] + (best // 2)\n        if updated == influence:\n            break\n        influence = updated\n        rounds += 1\n    for row in rows:\n        chain_id = row["chain_id"]\n        score = influence[chain_id]\n        row["chain_influence_score"] = score\n        row["chain_influence_rounds"] = rounds\n        row["chain_influence_digest"] = hashlib.sha256(\n            f"{chain_id}|{score}|{rounds}".encode()\n        ).hexdigest()[:12]\n\n\ndef export_report(events: list[dict], output_dir: Path, override_rows: list[dict]) -> None:\n    output_dir.mkdir(parents=True, exist_ok=True)\n    canonical = canonicalize_events(events)\n    compacted_overrides = _compact_overrides(override_rows)\n\n    severity_counts = {severity: 0 for severity in SEVERITY_ORDER}\n    issuers: set[str] = set()\n    for event in canonical:\n        severity = _normalize_severity(event.get("severity", ""))\n        if severity in severity_counts:\n            severity_counts[severity] += 1\n        issuers.add(_normalize_issuer(event.get("issuer", "")))\n\n    signals = []\n    override_excluded_count = 0\n    for event in canonical:\n        if not is_signal(event):\n            continue\n        if _is_override_suppressed(event, compacted_overrides):\n            override_excluded_count += 1\n            continue\n        issuer = _normalize_issuer(event.get("issuer", ""))\n        severity = _normalize_severity(event.get("severity", ""))\n        issued_ms = _normalize_issued_ms(event.get("issued_ms", 0))\n        all_overlap_ms = _probe_overlap_ms(\n            issued_ms,\n            compacted_overrides.get((issuer, "all"), []),\n        )\n        severity_overlap_ms = _probe_overlap_ms(\n            issued_ms,\n            compacted_overrides.get((issuer, severity), []),\n        )\n        wide_all_overlap_ms = _probe_overlap_ms(\n            issued_ms,\n            compacted_overrides.get((issuer, "all"), []),\n            lookback_ms=300,\n        )\n        wide_severity_overlap_ms = _probe_overlap_ms(\n            issued_ms,\n            compacted_overrides.get((issuer, severity), []),\n            lookback_ms=300,\n        )\n        # PKI-5390 / PKI-5392: the near and wide probe families round in OPPOSITE\n        # directions. Near floors its all half and ceilings its scoped half; wide\n        # does the reverse. Neither direction may be inferred from the other.\n        override_pressure_score = (all_overlap_ms // 60) + (-(-severity_overlap_ms // 62))\n        wide_pressure_score = (\n            (-(-wide_all_overlap_ms // 64)) + (wide_severity_overlap_ms // 66)\n        )\n        pressure_index = override_pressure_score + wide_pressure_score\n        signals.append(\n            {\n                "cert_id": event["cert_id"],\n                "issued_ms": issued_ms,\n                "severity": severity,\n                "issuer": issuer,\n                "detector": _normalize_detector(event["detector"]),\n                "override_pressure_score": override_pressure_score,\n                "wide_pressure_score": wide_pressure_score,\n                "pressure_index": pressure_index,\n            }\n        )\n    _annotate_chains(signals)\n    _annotate_chain_reach(signals)\n    _annotate_chain_influence(signals)\n    for signal in signals:\n        signal["signal_digest"] = hashlib.sha1(\n            (\n                f"{signal[\'cert_id\']}|{signal[\'issued_ms\']}|"\n                f"{signal[\'severity\']}|{signal[\'issuer\']}|"\n                f"{signal[\'detector\']}|{signal[\'override_pressure_score\']}|"\n                f"{signal[\'pressure_index\']}|"\n                f"{signal[\'chain_id\']}|{signal[\'chain_size\']}|"\n                f"{signal[\'chain_span_ms\']}|{signal[\'chain_risk_score\']}|"\n                f"{signal[\'chain_digest\']}|"\n                f"{signal[\'chain_reach_score\']}|"\n                f"{signal[\'chain_reach_depth\']}|"\n                f"{\',\'.join(signal[\'chain_reach_path\'])}|"\n                f"{signal[\'chain_reach_digest\']}"\n            ).encode("utf-8")\n        ).hexdigest()[:12]\n    signals.sort(\n        key=lambda row: (\n            -row["issued_ms"],\n            -_severity_rank(row["severity"]),\n            -row["chain_risk_score"],\n            -row["chain_reach_score"],\n            -row["override_pressure_score"],\n            str(row["cert_id"]),\n        )\n    )\n\n    # Escalation-pressure ledger, sequential over the signals in escalated order.\n    # Carry propagates between consecutive rows and decays with the observed gap;\n    # the carry credit is divided by 3 and ROUNDED UP (ceil), while the decay and\n    # the chain-size debit are floored. ceil(x/3) == -(-x // 3). PKI-5396 couples the\n    # ledger to directed reach: escalation_pressure gains chain_reach_score // 6 (FLOORED),\n    # affecting the critical flag only, not carry_out.\n    ESCALATION_THRESHOLD = 21\n    ESCALATION_CARRY_CAP = 63\n    previous_issued_ms = None\n    previous_carry_out = 0\n    critical_escalation_ids = []\n    max_escalation_pressure = 0\n    ledger_rows = []\n    for signal in signals:\n        gap_ms = 0 if previous_issued_ms is None else max(previous_issued_ms - signal["issued_ms"], 0)\n        carry_in = max(previous_carry_out - (gap_ms // 150), 0)\n        escalation_pressure = signal["chain_risk_score"] + (-(-carry_in // 3)) + (signal["chain_reach_score"] // 6) + (signal["chain_influence_score"] // 8)\n        carry_out = min(\n            carry_in + signal["chain_risk_score"] - (signal["chain_size"] // 2),\n            ESCALATION_CARRY_CAP,\n        )\n        flag = 1 if escalation_pressure >= ESCALATION_THRESHOLD else 0\n        if flag:\n            critical_escalation_ids.append(str(signal["cert_id"]))\n        max_escalation_pressure = max(max_escalation_pressure, escalation_pressure)\n        ledger_rows.append(f"{signal[\'cert_id\']}|{escalation_pressure}|{flag}|{carry_out}")\n        previous_issued_ms = signal["issued_ms"]\n        previous_carry_out = carry_out\n    critical_escalation_ids.sort()\n    escalation_ledger_checksum = hashlib.sha256(\n        "\\n".join(ledger_rows).encode("utf-8")\n    ).hexdigest()\n\n    summary = {\n        "schema_version": SCHEMA_VERSION,\n        "raw_cert_count": len(events),\n        "unique_cert_ids": len({str(event["cert_id"]) for event in events}),\n        "total_certs": len(canonical),\n        "severity_counts": severity_counts,\n        "issuers": sorted(issuers),\n        "escalated_count": len(signals),\n        "dismissed_excluded_count": sum(\n            1\n            for event in canonical\n            if _normalize_dismissed(event.get("dismissed", False))\n            and _normalize_severity(event.get("severity", "")) in ANOMALY_SEVERITIES\n        ),\n        "override_excluded_count": override_excluded_count,\n        "override_compaction_checksum": _override_compaction_checksum(compacted_overrides),\n        "max_wide_pressure_score": max(\n            (row["wide_pressure_score"] for row in signals),\n            default=0,\n        ),\n        "max_pressure_index": max(\n            (row["pressure_index"] for row in signals),\n            default=0,\n        ),\n        "max_override_pressure_score": max(\n            (row["override_pressure_score"] for row in signals),\n            default=0,\n        ),\n        "chain_count": len({row["chain_id"] for row in signals}),\n        "max_chain_risk_score": max(\n            (row["chain_risk_score"] for row in signals),\n            default=0,\n        ),\n        "chain_digest_checksum": hashlib.sha256(\n            "|".join(row["chain_digest"] for row in signals).encode("utf-8")\n        ).hexdigest(),\n        "max_chain_reach_score": max(\n            (row["chain_reach_score"] for row in signals),\n            default=0,\n        ),\n        "chain_reach_digest_checksum": hashlib.sha256(\n            "|".join(\n                row["chain_reach_digest"] for row in signals\n            ).encode("utf-8")\n        ).hexdigest(),\n        "max_chain_influence_score": max(\n            (row["chain_influence_score"] for row in signals),\n            default=0,\n        ),\n        "chain_influence_digest_checksum": hashlib.sha256(\n            "|".join(row["chain_influence_digest"] for row in signals).encode("utf-8")\n        ).hexdigest(),\n        "signal_digest_checksum": hashlib.sha256(\n            "|".join(row["signal_digest"] for row in signals).encode("utf-8")\n        ).hexdigest(),\n        "critical_escalation_ids": critical_escalation_ids,\n        "critical_escalation_count": len(critical_escalation_ids),\n        "max_escalation_pressure": max_escalation_pressure,\n        "escalation_ledger_checksum": escalation_ledger_checksum,\n    }\n\n    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\\n")\n    (output_dir / "issuer_matrix.json").write_text(\n        json.dumps(build_issuer_matrix(canonical), indent=2) + "\\n"\n    )\n    with (output_dir / "escalated.jsonl").open("w", encoding="utf-8") as handle:\n        for row in signals:\n            handle.write(json.dumps(row, separators=(",", ":")) + "\\n")\n'

REPAIRED_MAIN = 'def main() -> None:\n    parser = argparse.ArgumentParser()\n    parser.add_argument("--input", default="/app/data/events.json")\n    parser.add_argument("--output-dir", default="/app/output")\n    args = parser.parse_args()\n\n    events = load_events(Path(args.input))\n    override_rows = load_overrides()\n    export_report(events, Path(args.output_dir), override_rows)\n    print(f"Wrote report to {args.output_dir}")\n\n\nif __name__ == "__main__":\n    main()\n'


def patch_workflow() -> None:
    """Rebuild the workflow by transforming the frozen broken snapshot.

    The repair derives the new source from the snapshot itself: it verifies the
    documented defect anchors are present, keeps the original header, imports,
    SCHEMA_VERSION and load_events, rewrites the module docstring, extends the
    imports, and splices the corrected processing core and entrypoint in place
    of the defective export_report/main pair.
    """
    original = ORIGINAL_PIPELINE.read_text()
    spec = load_spec()
    for token in spec["repair_audit"]["forbidden_executable_tokens"]:
        if token not in original:
            raise RuntimeError(f"frozen snapshot missing expected defect anchor: {token}")
    for anchor in ("def export_report(", "def main(", WORKFLOW_DOCSTRING_BROKEN, "import json"):
        if anchor not in original:
            raise RuntimeError(f"frozen snapshot missing structural anchor: {anchor}")
    head = original.split("def export_report(", 1)[0]
    head = head.replace(WORKFLOW_DOCSTRING_BROKEN, WORKFLOW_DOCSTRING_REPAIRED, 1)
    head = head.replace("import json", "import hashlib\nimport json", 1)
    repaired = head + REPAIRED_CORE + "\n\n" + REPAIRED_MAIN
    ast.parse(repaired)
    PIPELINE_PATH.write_text(repaired)


def build_diagnosis_report(
    status: str,
    events: list[dict],
    issues: list[dict],
    summary: dict | None = None,
    output_dir: Path | None = None,
) -> dict:
    report = {
        "pipeline_status": status,
        "issues_found": issues,
        "input_stats": input_stats(events),
    }
    if summary is not None and output_dir is not None:
        report["verified_summary"] = summary
        report["output_paths"] = {
            "summary_json": str(output_dir / "summary.json"),
            "escalated_jsonl": str(output_dir / "escalated.jsonl"),
            "issuer_matrix_json": str(output_dir / "issuer_matrix.json"),
        }
    return report


def cmd_diagnose(dossier: Path, report_path: Path) -> None:
    dossier_text = dossier.read_text(encoding="utf-8", errors="replace")
    spec = load_spec()
    original_pipeline = ORIGINAL_PIPELINE.read_text()
    events = load_events()
    issues = build_issues_from_sources(dossier_text, original_pipeline, spec)
    report = build_diagnosis_report("diagnosed", events, issues)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")


def cmd_repair(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnosis_path = output_dir / "diagnosis.json"
    audit_path = output_dir / "repair_audit.json"
    rerun_dir = output_dir / "rerun"
    dossier_path = Path("/app/incident/export_dossier.md")

    spec = load_spec()
    dossier_text = dossier_path.read_text(encoding="utf-8", errors="replace")
    original_pipeline = ORIGINAL_PIPELINE.read_text()
    issues = build_issues_from_sources(dossier_text, original_pipeline, spec)

    pre_audit = pre_repair_audit()
    patch_workflow()
    ast.parse(PIPELINE_PATH.read_text())

    subprocess.run(
        [
            sys.executable,
            str(PIPELINE_PATH),
            "--input",
            str(EVENTS_PATH),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
    )

    if rerun_dir.exists():
        for child in rerun_dir.iterdir():
            child.unlink()
    else:
        rerun_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            sys.executable,
            str(PIPELINE_PATH),
            "--input",
            str(EVENTS_PATH),
            "--output-dir",
            str(rerun_dir),
        ],
        check=True,
    )

    events = load_events()
    summary = json.loads((output_dir / "summary.json").read_text())
    diagnosis = build_diagnosis_report("repaired", events, issues, summary, output_dir)
    diagnosis_path.write_text(json.dumps(diagnosis, indent=2) + "\n")

    code = PIPELINE_PATH.read_text()
    audit = {
        "patched_workflow": str(PIPELINE_PATH),
        "processing_steps": spec["repair_audit"]["processing_steps"],
        "removed_tokens": {token: token not in code for token in FORBIDDEN_TOKENS},
        "pre_repair": pre_audit,
        "post_repair": {
            "escalated_count": summary["escalated_count"],
            "rerun_escalated_count": json.loads((rerun_dir / "summary.json").read_text())[
                "escalated_count"
            ],
        },
    }
    audit_path.write_text(json.dumps(audit, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Certwarden signal diagnostic CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser("diagnose")
    diag.add_argument("--dossier", type=Path, required=True)
    diag.add_argument("--report", type=Path, default=Path("/app/output/diagnosis.json"))

    repair = sub.add_parser("repair")
    repair.add_argument("--output-dir", type=Path, default=Path("/app/output"))

    args = parser.parse_args()
    if args.command == "diagnose":
        cmd_diagnose(args.dossier, args.report)
    else:
        cmd_repair(args.output_dir)


if __name__ == "__main__":
    main()
