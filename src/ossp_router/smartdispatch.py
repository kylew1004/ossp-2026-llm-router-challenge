# SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
# SPDX-License-Identifier: Apache-2.0

"""SmartDispatch router: linear heads over word+char hashed prompt features.

Extends the public hash-regex baseline with a second character n-gram hash
block, additional dense domain features, and difference ("gain") heads that
predict the quality margin of upgrading, which is what tier selection
actually consumes. Inference is stdlib-only and reads a JSON artifact.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from ossp_router.heuristic import (
    episode_text,
    extract_features,
    write_submission_atomic,
)
from ossp_router.protocol import (
    MODEL_IDS,
    TIERS,
    Decision,
    Episode,
    InputBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    load_bundled_policy,
    load_input,
    load_json,
    load_policy,
    parse_submission,
    policy_sha256,
    submission_to_dict,
)

ARTIFACT_TYPE = "smartdispatch-linear-v1"
HASH_ALGORITHM = "fnv1a64-signed-word12-char34"
MIN_HASH_BINS = 16
MAX_HASH_BINS = 65_536
_FNV_OFFSET = 14_695_981_039_346_656_037
_FNV_PRIME = 1_099_511_628_211
_UINT64_MASK = (1 << 64) - 1
_TOKEN = re.compile(r"[A-Za-z]+|[가-힣]+|\d+|[^\w\s]", re.UNICODE)
_FORMAL_REASONING = re.compile(
    r"\b(?:prove|derive|theorem|lemma|counterexample|induction|"
    r"증명|유도|정리|보조정리|반례|귀납)\b",
    re.IGNORECASE,
)
_PROGRAM_ANALYSIS = re.compile(
    r"```|\b(?:traceback|exception|complexity|big[- ]?o|"
    r"시간\s*복잡도|공간\s*복잡도|예외|스택\s*추적)\b",
    re.IGNORECASE,
)
_MULTI_CONSTRAINT = re.compile(
    r"\b(?:exactly|at least|at most|must|only|without|"
    r"정확히|이상|이하|반드시|오직|제외하고)\b",
    re.IGNORECASE,
)
_SIMPLE_TRANSFORM = re.compile(
    r"\b(?:summari[sz]e|rewrite|translate|list|extract|"
    r"요약|바꾸|번역|나열|추출)\b",
    re.IGNORECASE,
)
_PYTHON_SYNTAX = re.compile(
    r"\bdef |\breturn\b|==|\*\*|\bimport\b|\blambda\b|\bfor .+ in\b"
)
_MATH_PROCEDURAL = re.compile(
    r"\b(?:Let|Suppose|Find|Solve|Calculate|Simplify|Evaluate|"
    r"derivative|factor|divisible|remainder|probability|prime|polynomial)\b"
)

DENSE_FEATURE_NAMES = (
    "log_character_count",
    "log_word_count",
    "log_sentence_count",
    "log_message_count",
    "hangul_ratio",
    "log_code_marker_count",
    "log_math_marker_count",
    "numeric_density",
    "long_context",
    "log_reasoning_marker_count",
    "formal_reasoning",
    "program_analysis",
    "log_multi_constraint_count",
    "simple_transform",
    "starts_def",
    "log_python_syntax_count",
    "log_math_procedural_count",
    "pure_ascii",
    "log_question_count",
)


@dataclass(frozen=True)
class LinearHead:
    intercept: float
    coefficients: Tuple[float, ...]


@dataclass(frozen=True)
class SmartDispatchArtifact:
    word_hash_bins: int
    char_hash_bins: int
    feature_mean: Tuple[float, ...]
    feature_scale: Tuple[float, ...]
    score_heads: Mapping[str, LinearHead]
    log_cost_heads: Mapping[str, LinearHead]
    gain_heads: Mapping[str, LinearHead]
    selection_mode: str
    tier_safety_ratios: Mapping[str, float]
    premium_fill_safety_ratio: float
    policy_id: str
    policy_digest: str
    training_summary: Mapping[str, Any]


@dataclass(frozen=True)
class SmartDispatchPlan:
    submission: Submission
    predicted_budget_ratio: float
    safety_ratio: float


def _stable_hash(value: str) -> int:
    digest = _FNV_OFFSET
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * _FNV_PRIME) & _UINT64_MASK
    return digest


def _normalized_tokens(text: str) -> Tuple[str, ...]:
    result = []
    for token in _TOKEN.findall(text):
        normalized = token.casefold()
        if normalized.isdecimal():
            normalized = "<number>"
        result.append(normalized)
    return tuple(result)


def _validate_bins(bins: int, label: str) -> None:
    if (
        isinstance(bins, bool)
        or not isinstance(bins, int)
        or not MIN_HASH_BINS <= bins <= MAX_HASH_BINS
        or bins & (bins - 1)
    ):
        raise ValueError(f"{label}은 허용 범위의 2의 거듭제곱이어야 합니다.")


def _hash_block(values: Sequence[str], bins: int) -> Tuple[float, ...]:
    block = [0.0] * bins
    for value in values:
        digest = _stable_hash(value)
        index = digest & (bins - 1)
        block[index] += -1.0 if digest & (1 << 63) else 1.0
    norm = math.sqrt(sum(item * item for item in block))
    if norm:
        block = [item / norm for item in block]
    return tuple(block)


def raw_feature_vector(
    episode: Episode, word_hash_bins: int, char_hash_bins: int
) -> Tuple[float, ...]:
    """Dense features plus signed word 1-2gram and char 3-4gram hash blocks."""

    _validate_bins(word_hash_bins, "word_hash_bins")
    _validate_bins(char_hash_bins, "char_hash_bins")
    features = extract_features(episode)
    text = episode_text(episode)
    dense = (
        math.log1p(features.character_count),
        math.log1p(features.word_count),
        math.log1p(features.sentence_count),
        math.log1p(features.message_count),
        features.hangul_ratio,
        math.log1p(features.code_marker_count),
        math.log1p(features.math_marker_count),
        features.numeric_density,
        float(features.long_context),
        math.log1p(features.reasoning_marker_count),
        float(bool(_FORMAL_REASONING.search(text))),
        float(bool(_PROGRAM_ANALYSIS.search(text))),
        math.log1p(len(_MULTI_CONSTRAINT.findall(text))),
        float(bool(_SIMPLE_TRANSFORM.search(text))),
        float(text.lstrip().startswith("def ")),
        math.log1p(len(_PYTHON_SYNTAX.findall(text))),
        math.log1p(len(_MATH_PROCEDURAL.findall(text))),
        float(features.hangul_ratio == 0.0),
        math.log1p(text.count("?")),
    )
    tokens = _normalized_tokens(text)
    word_values = [f"w1:{token}" for token in tokens]
    word_values.extend(
        f"w2:{left}\x1f{right}" for left, right in zip(tokens, tokens[1:])
    )
    folded = text.casefold()
    char_values = [f"c3:{folded[i:i + 3]}" for i in range(len(folded) - 2)]
    char_values.extend(f"c4:{folded[i:i + 4]}" for i in range(len(folded) - 3))
    return (
        dense
        + _hash_block(word_values, word_hash_bins)
        + _hash_block(char_values, char_hash_bins)
    )


GAIN_KEYS = ("gain_ax31", "gain_think")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{label}은(는) JSON 객체여야 합니다.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    missing = sorted(set(expected) - set(value))
    extra = sorted(set(value) - set(expected))
    if missing or extra:
        raise ProtocolError(f"{label} 필드 오류: 누락={missing}, 초과={extra}")


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(f"{label} 값이 허용 범위를 벗어났습니다.")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ProtocolError(f"{label}은(는) 유한한 숫자여야 합니다.")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError(f"{label}은(는) 유한한 숫자여야 합니다.")
    return result


def _vector(value: Any, length: int, label: str) -> Tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise ProtocolError(f"{label}은(는) 길이 {length}의 배열이어야 합니다.")
    return tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))


def _head(value: Any, length: int, label: str) -> LinearHead:
    raw = _object(value, label)
    _exact_keys(raw, ("intercept", "coefficients"), label)
    return LinearHead(
        intercept=_number(raw["intercept"], f"{label}.intercept"),
        coefficients=_vector(raw["coefficients"], length, f"{label}.coefficients"),
    )


def parse_artifact(value: Any) -> SmartDispatchArtifact:
    root = _object(value, "artifact")
    expected = (
        "artifact_type",
        "schema_version",
        "hash_algorithm",
        "word_hash_bins",
        "char_hash_bins",
        "dense_feature_names",
        "model_ids",
        "policy_id",
        "policy_sha256",
        "feature_mean",
        "feature_scale",
        "score_heads",
        "log_cost_heads",
        "gain_heads",
        "selection_mode",
        "tier_safety_ratios",
        "premium_fill_safety_ratio",
        "training_summary",
    )
    _exact_keys(root, expected, "artifact")
    if root["artifact_type"] != ARTIFACT_TYPE:
        raise ProtocolError("지원하지 않는 smartdispatch artifact_type입니다.")
    if _integer(root["schema_version"], "artifact.schema_version", 1, 1) != 1:
        raise ProtocolError("지원하지 않는 smartdispatch artifact 버전입니다.")
    if root["hash_algorithm"] != HASH_ALGORITHM:
        raise ProtocolError("지원하지 않는 feature hash 방식입니다.")
    word_bins = _integer(
        root["word_hash_bins"], "artifact.word_hash_bins", MIN_HASH_BINS, MAX_HASH_BINS
    )
    char_bins = _integer(
        root["char_hash_bins"], "artifact.char_hash_bins", MIN_HASH_BINS, MAX_HASH_BINS
    )
    if word_bins & (word_bins - 1) or char_bins & (char_bins - 1):
        raise ProtocolError("hash bins는 2의 거듭제곱이어야 합니다.")
    if root["dense_feature_names"] != list(DENSE_FEATURE_NAMES):
        raise ProtocolError("dense feature 정의가 현재 런타임과 다릅니다.")
    if root["model_ids"] != list(MODEL_IDS):
        raise ProtocolError("artifact.model_ids가 공개 정책 모델과 다릅니다.")
    if root["selection_mode"] not in ("per-model", "gain"):
        raise ProtocolError("artifact.selection_mode가 올바르지 않습니다.")
    length = len(DENSE_FEATURE_NAMES) + word_bins + char_bins
    mean = _vector(root["feature_mean"], length, "artifact.feature_mean")
    scale = _vector(root["feature_scale"], length, "artifact.feature_scale")
    if any(item <= 0 for item in scale):
        raise ProtocolError("artifact.feature_scale은 모두 0보다 커야 합니다.")
    score_raw = _object(root["score_heads"], "artifact.score_heads")
    cost_raw = _object(root["log_cost_heads"], "artifact.log_cost_heads")
    gain_raw = _object(root["gain_heads"], "artifact.gain_heads")
    if set(score_raw) != set(MODEL_IDS) or set(cost_raw) != set(MODEL_IDS):
        raise ProtocolError("artifact 선형 head의 모델 집합이 올바르지 않습니다.")
    if set(gain_raw) != set(GAIN_KEYS):
        raise ProtocolError("artifact.gain_heads 구성이 올바르지 않습니다.")
    safety_raw = _object(root["tier_safety_ratios"], "artifact.tier_safety_ratios")
    if set(safety_raw) != set(TIERS):
        raise ProtocolError("artifact 등급별 안전계수가 완전하지 않습니다.")
    safety = {
        tier: _number(safety_raw[tier], f"artifact.tier_safety_ratios.{tier}")
        for tier in TIERS
    }
    if any(not 0 < value <= 1 for value in safety.values()):
        raise ProtocolError("artifact 안전계수는 0보다 크고 1 이하여야 합니다.")
    fill_safety = _number(
        root["premium_fill_safety_ratio"], "artifact.premium_fill_safety_ratio"
    )
    if not 0 < fill_safety <= 1:
        raise ProtocolError("premium_fill_safety_ratio는 0보다 크고 1 이하여야 합니다.")
    policy_id = root["policy_id"]
    policy_digest = root["policy_sha256"]
    if not isinstance(policy_id, str) or not policy_id:
        raise ProtocolError("artifact.policy_id가 올바르지 않습니다.")
    if (
        not isinstance(policy_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", policy_digest) is None
    ):
        raise ProtocolError("artifact.policy_sha256가 올바르지 않습니다.")
    return SmartDispatchArtifact(
        word_hash_bins=word_bins,
        char_hash_bins=char_bins,
        feature_mean=mean,
        feature_scale=scale,
        score_heads={
            model_id: _head(score_raw[model_id], length, f"score_heads.{model_id}")
            for model_id in MODEL_IDS
        },
        log_cost_heads={
            model_id: _head(cost_raw[model_id], length, f"log_cost_heads.{model_id}")
            for model_id in MODEL_IDS
        },
        gain_heads={
            key: _head(gain_raw[key], length, f"gain_heads.{key}")
            for key in GAIN_KEYS
        },
        selection_mode=root["selection_mode"],
        tier_safety_ratios=safety,
        premium_fill_safety_ratio=fill_safety,
        policy_id=policy_id,
        policy_digest=policy_digest,
        training_summary=dict(_object(root["training_summary"], "training_summary")),
    )


def load_artifact(path: Path) -> SmartDispatchArtifact:
    return parse_artifact(load_json(path))


def _linear(head: LinearHead, values: Sequence[float]) -> float:
    return head.intercept + math.fsum(
        coefficient * value
        for coefficient, value in zip(head.coefficients, values)
    )


def predict_episode(
    episode: Episode, artifact: SmartDispatchArtifact
) -> Tuple[Mapping[str, float], Mapping[str, float]]:
    """Return (effective scores, predicted costs) for one episode.

    In "gain" mode the effective scores are anchored on the light model's
    predicted score with additive predicted upgrade margins, which avoids the
    per-model clipping distortion around score differences.
    """

    raw = raw_feature_vector(
        episode, artifact.word_hash_bins, artifact.char_hash_bins
    )
    standardized = tuple(
        (value - mean) / scale
        for value, mean, scale in zip(
            raw, artifact.feature_mean, artifact.feature_scale
        )
    )
    if artifact.selection_mode == "gain":
        light = min(
            1.0,
            max(0.0, _linear(artifact.score_heads[MODEL_IDS[0]], standardized)),
        )
        gain_mid = _linear(artifact.gain_heads["gain_ax31"], standardized)
        gain_think = _linear(artifact.gain_heads["gain_think"], standardized)
        scores = {
            MODEL_IDS[0]: light,
            MODEL_IDS[1]: min(1.0, max(0.0, light + gain_mid)),
            MODEL_IDS[2]: min(1.0, max(0.0, light + gain_think)),
        }
    else:
        scores = {
            model_id: min(
                1.0, max(0.0, _linear(artifact.score_heads[model_id], standardized))
            )
            for model_id in MODEL_IDS
        }
    costs = {
        model_id: math.exp(
            min(50.0, max(-50.0, _linear(artifact.log_cost_heads[model_id], standardized)))
        )
        for model_id in MODEL_IDS
    }
    light_cost = costs[MODEL_IDS[0]]
    costs[MODEL_IDS[1]] = max(costs[MODEL_IDS[1]], light_cost * (1.0 + 1e-12))
    costs[MODEL_IDS[2]] = max(costs[MODEL_IDS[2]], costs[MODEL_IDS[1]] * (1.0 + 1e-12))
    return scores, costs


def select_models(
    predicted_scores: Sequence[Mapping[str, float]],
    predicted_costs: Sequence[Mapping[str, float]],
    *,
    budget_multiplier: float,
    safety_ratio: float,
) -> Tuple[Tuple[str, ...], float]:
    """Select one model per row with a batch-level Lagrangian budget."""

    if len(predicted_scores) != len(predicted_costs) or not predicted_scores:
        raise ValueError("예측 score와 cost는 같은 길이의 비어 있지 않은 배열이어야 합니다.")
    light_total = math.fsum(row[MODEL_IDS[0]] for row in predicted_costs)
    effective_ratio = max(1.0, budget_multiplier * safety_ratio)
    cap = light_total * effective_ratio

    def choose(penalty: float) -> Tuple[Tuple[str, ...], float]:
        selected = []
        for scores, costs in zip(predicted_scores, predicted_costs):
            model_id = max(
                MODEL_IDS,
                key=lambda candidate: (
                    scores[candidate] - penalty * costs[candidate] / light_total,
                    -MODEL_IDS.index(candidate),
                ),
            )
            selected.append(model_id)
        total = math.fsum(
            costs[model_id]
            for costs, model_id in zip(predicted_costs, selected)
        )
        return tuple(selected), total

    selected, total = choose(0.0)
    if total > cap:
        low = 0.0
        high = 1.0
        selected, total = choose(high)
        while total > cap and high < 2**60:
            low = high
            high *= 2.0
            selected, total = choose(high)
        for _iteration in range(80):
            middle = (low + high) / 2.0
            candidate, candidate_total = choose(middle)
            if candidate_total <= cap:
                high = middle
                selected, total = candidate, candidate_total
            else:
                low = middle
    if total > cap:
        selected = tuple(MODEL_IDS[0] for _row in predicted_scores)
        total = light_total
    return selected, total / light_total


def fill_ax31_upgrades(
    selected: Sequence[str],
    predicted_scores: Sequence[Mapping[str, float]],
    predicted_costs: Sequence[Mapping[str, float]],
    *,
    budget_multiplier: float,
    safety_ratio: float,
) -> Tuple[Tuple[str, ...], float]:
    """Lock existing choices and fill unused predicted budget with AX31 upgrades."""

    if (
        len(selected) != len(predicted_scores)
        or len(selected) != len(predicted_costs)
        or not selected
    ):
        raise ValueError("선택과 예측 배열은 같은 길이의 비어 있지 않은 배열이어야 합니다.")
    if any(model_id not in MODEL_IDS for model_id in selected):
        raise ValueError("선택 배열에 알 수 없는 모델이 있습니다.")
    if not 0 < safety_ratio <= 1:
        raise ValueError("AX31 fill 안전계수는 0보다 크고 1 이하여야 합니다.")

    light_id, ax31_id, _think_id = MODEL_IDS
    light_total = math.fsum(row[light_id] for row in predicted_costs)
    current_total = math.fsum(
        costs[model_id]
        for costs, model_id in zip(predicted_costs, selected)
    )
    cap = max(
        current_total,
        light_total * max(1.0, budget_multiplier * safety_ratio),
    )

    def choose(penalty: float) -> Tuple[Tuple[str, ...], float]:
        filled = []
        for model_id, scores, costs in zip(
            selected, predicted_scores, predicted_costs
        ):
            if model_id != light_id:
                filled.append(model_id)
                continue
            incremental_score = scores[ax31_id] - scores[light_id]
            incremental_cost = costs[ax31_id] - costs[light_id]
            if incremental_score - penalty * incremental_cost / light_total > 0:
                filled.append(ax31_id)
            else:
                filled.append(light_id)
        total = math.fsum(
            costs[model_id]
            for costs, model_id in zip(predicted_costs, filled)
        )
        return tuple(filled), total

    filled, total = choose(0.0)
    if total > cap:
        low = 0.0
        high = 1.0
        filled, total = choose(high)
        while total > cap and high < 2**60:
            low = high
            high *= 2.0
            filled, total = choose(high)
        for _iteration in range(80):
            middle = (low + high) / 2.0
            candidate, candidate_total = choose(middle)
            if candidate_total <= cap:
                high = middle
                filled, total = candidate, candidate_total
            else:
                low = middle
    if total > cap:
        return tuple(selected), current_total / light_total
    return filled, total / light_total


def make_smartdispatch_submission(
    inputs: InputBatch,
    policy: RoutingPolicy,
    artifact: SmartDispatchArtifact,
    tier: str,
) -> SmartDispatchPlan:
    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("입력과 정책의 schema_version이 일치하지 않습니다.")
    if tier not in TIERS:
        raise ProtocolError(f"알 수 없는 tier: {tier}")
    if artifact.policy_id != policy.policy_id:
        raise ProtocolError("artifact와 정책의 policy_id가 다릅니다.")
    if artifact.policy_digest != policy_sha256(policy):
        raise ProtocolError("artifact와 현재 정책의 SHA-256이 다릅니다.")
    predictions = [predict_episode(episode, artifact) for episode in inputs.episodes]
    scores = [item[0] for item in predictions]
    costs = [item[1] for item in predictions]
    safety = artifact.tier_safety_ratios[tier]
    selected, ratio = select_models(
        scores,
        costs,
        budget_multiplier=float(policy.tiers[tier].budget_multiplier),
        safety_ratio=safety,
    )
    if tier == "premium":
        selected, ratio = fill_ax31_upgrades(
            selected,
            scores,
            costs,
            budget_multiplier=float(policy.tiers[tier].budget_multiplier),
            safety_ratio=artifact.premium_fill_safety_ratio,
        )
    submission = Submission(
        schema_version=inputs.schema_version,
        challenge_id=inputs.challenge_id,
        policy_id=policy.policy_id,
        split=inputs.split,
        tier=tier,
        decisions=tuple(
            Decision(episode.episode_id, model_id)
            for episode, model_id in zip(inputs.episodes, selected)
        ),
    )
    return SmartDispatchPlan(
        submission=parse_submission(submission_to_dict(submission)),
        predicted_budget_ratio=ratio,
        safety_ratio=safety,
    )


def _default_artifact_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "smartdispatch.v1.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartdispatch-run",
        description="SmartDispatch 학습형 라우터를 한 등급에 대해 실행합니다.",
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--tier", choices=TIERS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--artifact", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        inputs = load_input(args.input)
        policy = (
            load_policy(args.policy)
            if args.policy is not None
            else load_bundled_policy()
        )
        artifact_path = (
            args.artifact if args.artifact is not None else _default_artifact_path()
        )
        artifact = load_artifact(artifact_path)
        plan = make_smartdispatch_submission(inputs, policy, artifact, args.tier)
        write_submission_atomic(args.output, plan.submission)
    except (OSError, ProtocolError, ValueError, json.JSONDecodeError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(
        "OK: "
        f"{args.tier} 제출 파일을 생성했습니다 "
        f"(예측 비용 비율 {plan.predicted_budget_ratio:.6f}, "
        f"안전계수 {plan.safety_ratio:.4f})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
