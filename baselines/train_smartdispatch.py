# SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
# SPDX-License-Identifier: Apache-2.0

"""Train the SmartDispatch router (word+char hashing, gain heads, split alpha).

Extends the public train_hash_regex recipe: a second character n-gram hash
block, extra dense features, difference ("gain") targets, and independent
ridge regularization for quality-side and cost-side heads. Safety ratios are
selected on train OOF predictions with the official Decimal scorer, then
re-calibrated on Dev when provided, exactly like the public baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from ossp_router import smartdispatch
from ossp_router.protocol import (
    MODEL_IDS,
    TIERS,
    Decision,
    InputBatch,
    Outcome,
    OutcomeBatch,
    ProtocolError,
    RoutingPolicy,
    Submission,
    load_bundled_policy,
    load_input,
    load_outcomes,
    load_policy,
    policy_sha256,
)
from ossp_router.scoring import score_submissions

SCORE_COLUMNS = len(MODEL_IDS)
COST_COLUMNS = len(MODEL_IDS)
GAIN_COLUMNS = len(smartdispatch.GAIN_KEYS)


def _require_numpy() -> None:
    if np is None:
        raise RuntimeError(
            "학습에는 NumPy가 필요합니다. baselines/requirements-train.txt를 설치해 주세요."
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _outcome_cost(outcome: Outcome, policy: RoutingPolicy) -> float:
    rates = policy.models[outcome.model_id]
    unit = Decimal(policy.token_unit)
    cost = (
        rates.fixed_cost
        + Decimal(outcome.input_tokens) * rates.input_token_rate / unit
        + Decimal(outcome.output_tokens) * rates.output_token_rate / unit
    )
    result = float(cost)
    if not math.isfinite(result) or result <= 0:
        raise ProtocolError("학습 outcome의 모델 비용은 0보다 커야 합니다.")
    return result


def _training_matrix(
    inputs: InputBatch,
    outcomes: OutcomeBatch,
    policy: RoutingPolicy,
    word_bins: int,
    char_bins: int,
) -> Tuple[Any, Any]:
    _require_numpy()
    if inputs.schema_version != outcomes.schema_version:
        raise ProtocolError("Train 입력과 outcome의 schema_version이 다릅니다.")
    if inputs.challenge_id != outcomes.challenge_id or inputs.split != outcomes.split:
        raise ProtocolError("Train 입력과 outcome의 실행 메타데이터가 다릅니다.")
    outcome_index = {
        (outcome.episode_id, outcome.model_id): outcome
        for outcome in outcomes.outcomes
    }
    expected = {
        (episode.episode_id, model_id)
        for episode in inputs.episodes
        for model_id in MODEL_IDS
    }
    if set(outcome_index) != expected:
        raise ProtocolError("Train outcome 행렬이 입력과 모델 전체를 포함하지 않습니다.")
    matrix = np.asarray(
        [
            smartdispatch.raw_feature_vector(episode, word_bins, char_bins)
            for episode in inputs.episodes
        ],
        dtype=np.float64,
    )
    targets = []
    for episode in inputs.episodes:
        rows = [outcome_index[(episode.episode_id, model_id)] for model_id in MODEL_IDS]
        scores = [float(row.score) for row in rows]
        log_costs = [math.log(_outcome_cost(row, policy)) for row in rows]
        gains = [
            scores[1] - scores[0],
            scores[2] - max(scores[0], scores[1]),
        ]
        targets.append(scores + log_costs + gains)
    return matrix, np.asarray(targets, dtype=np.float64)


def _fit_ridge(matrix: Any, targets: Any, alpha: float):
    _require_numpy()
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    standardized = (matrix - mean) / scale
    intercept = targets.mean(axis=0)
    centered = targets - intercept
    rows, columns = standardized.shape
    if rows <= columns:
        system = standardized @ standardized.T + alpha * np.eye(rows)
        coefficients = standardized.T @ np.linalg.solve(system, centered)
    else:
        system = standardized.T @ standardized + alpha * np.eye(columns)
        coefficients = np.linalg.solve(system, standardized.T @ centered)
    return mean, scale, intercept, coefficients


def _predict_ridge(matrix, mean, scale, intercept, coefficients):
    return (matrix - mean) / scale @ coefficients + intercept


def _oof_predictions(matrix, targets, *, folds: int, alpha: float):
    _require_numpy()
    rows = matrix.shape[0]
    predictions = np.empty_like(targets)
    fold_ids = np.arange(rows) % folds
    for fold in range(folds):
        validation = fold_ids == fold
        training = ~validation
        mean, scale, intercept, coefficients = _fit_ridge(
            matrix[training], targets[training], alpha
        )
        predictions[validation] = _predict_ridge(
            matrix[validation], mean, scale, intercept, coefficients
        )
    return predictions


def _select_alpha_for_group(
    matrix, group_targets, *, folds: int, candidates: Sequence[float]
) -> Tuple[float, Any, Mapping[str, float]]:
    """Pick the alpha minimizing OOF MSE for one target group."""
    _require_numpy()
    best = None
    diagnostics: Dict[str, float] = {}
    for alpha in candidates:
        predictions = _oof_predictions(
            matrix, group_targets, folds=folds, alpha=alpha
        )
        objective = float(np.mean((predictions - group_targets) ** 2))
        diagnostics[format(alpha, ".12g")] = objective
        rank = (objective, alpha)
        if best is None or rank < best[0]:
            best = (rank, alpha, predictions)
    assert best is not None
    return best[1], best[2], diagnostics


def _prediction_rows(
    quality_predictions, cost_predictions, selection_mode: str
) -> Tuple[Sequence[Mapping[str, float]], Sequence[Mapping[str, float]]]:
    """quality_predictions: columns [s1,s2,s3,g_ax31,g_think]; cost: [c1,c2,c3]."""
    scores = []
    costs = []
    for q_row, c_row in zip(quality_predictions, cost_predictions):
        if selection_mode == "gain":
            light = min(1.0, max(0.0, float(q_row[0])))
            score_row = {
                MODEL_IDS[0]: light,
                MODEL_IDS[1]: min(1.0, max(0.0, light + float(q_row[SCORE_COLUMNS]))),
                MODEL_IDS[2]: min(
                    1.0, max(0.0, light + float(q_row[SCORE_COLUMNS + 1]))
                ),
            }
        else:
            score_row = {
                model_id: min(1.0, max(0.0, float(q_row[index])))
                for index, model_id in enumerate(MODEL_IDS)
            }
        cost_row = {
            model_id: math.exp(min(50.0, max(-50.0, float(c_row[index]))))
            for index, model_id in enumerate(MODEL_IDS)
        }
        light_cost = cost_row[MODEL_IDS[0]]
        cost_row[MODEL_IDS[1]] = max(cost_row[MODEL_IDS[1]], light_cost * (1.0 + 1e-12))
        cost_row[MODEL_IDS[2]] = max(
            cost_row[MODEL_IDS[2]], cost_row[MODEL_IDS[1]] * (1.0 + 1e-12)
        )
        scores.append(score_row)
        costs.append(cost_row)
    return scores, costs


def _submission(inputs, policy, tier, selected) -> Submission:
    return Submission(
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


def _score_one_tier(inputs, outcomes, policy, tier, selected) -> Mapping[str, Any]:
    all_light = tuple(policy.light_model_id for _episode in inputs.episodes)
    submissions = [
        _submission(
            inputs, policy, candidate, selected if candidate == tier else all_light
        )
        for candidate in TIERS
    ]
    return score_submissions(inputs, outcomes, submissions, policy)["tiers"][tier]


def _safety_candidates(policy, tier, size: int) -> Tuple[float, ...]:
    minimum = 1.0 / float(policy.tiers[tier].budget_multiplier)
    if size <= 1 or minimum >= 1.0:
        return (min(1.0, minimum),)
    return tuple(
        minimum + (1.0 - minimum) * index / (size - 1) for index in range(size)
    )


def _select_with_fill(
    predicted_scores, predicted_costs, policy, tier, safety, fill_safety
):
    selected, ratio = smartdispatch.select_models(
        predicted_scores,
        predicted_costs,
        budget_multiplier=float(policy.tiers[tier].budget_multiplier),
        safety_ratio=safety,
    )
    if tier == "premium":
        selected, ratio = smartdispatch.fill_ax31_upgrades(
            selected,
            predicted_scores,
            predicted_costs,
            budget_multiplier=float(policy.tiers[tier].budget_multiplier),
            safety_ratio=fill_safety,
        )
    return selected, ratio


def _calibrate_safety(
    inputs,
    outcomes,
    policy,
    predicted_scores,
    predicted_costs,
    grid_size: int,
    fill_safety: float,
    require_pass: bool,
) -> Tuple[Mapping[str, float], Mapping[str, Any]]:
    calibrated: Dict[str, float] = {}
    reports: Dict[str, Any] = {}
    for tier in TIERS:
        best = None
        for safety in _safety_candidates(policy, tier, grid_size):
            selected, predicted_ratio = _select_with_fill(
                predicted_scores, predicted_costs, policy, tier, safety, fill_safety
            )
            report = _score_one_tier(inputs, outcomes, policy, tier, selected)
            if require_pass and not report["budget_passed"]:
                continue
            rank = (
                Decimal(report["tier_score"]),
                -Decimal(report["budget_ratio"]),
                -Decimal(str(safety)),
            )
            if best is None or rank > best[0]:
                best = (rank, safety, predicted_ratio, report)
        if best is None:
            raise RuntimeError(f"{tier} 예산을 통과하는 안전계수가 없습니다.")
        calibrated[tier] = best[1]
        reports[tier] = {
            "safety_ratio": best[1],
            "predicted_budget_ratio": best[2],
            "actual_budget_ratio": best[3]["budget_ratio"],
            "tier_score": best[3]["tier_score"],
            "budget_passed": best[3]["budget_passed"],
        }
    return calibrated, reports


def _head_dict(intercept: float, coefficients) -> Mapping[str, Any]:
    return {
        "intercept": float(intercept),
        "coefficients": [float(value) for value in coefficients],
    }


def _artifact_dict(
    *,
    word_bins: int,
    char_bins: int,
    policy: RoutingPolicy,
    mean,
    scale,
    quality_intercept,
    quality_coefficients,
    cost_intercept,
    cost_coefficients,
    selection_mode: str,
    safety_ratios: Mapping[str, float],
    fill_safety: float,
    training_summary: Mapping[str, Any],
) -> Mapping[str, Any]:
    return {
        "artifact_type": smartdispatch.ARTIFACT_TYPE,
        "schema_version": 1,
        "hash_algorithm": smartdispatch.HASH_ALGORITHM,
        "word_hash_bins": word_bins,
        "char_hash_bins": char_bins,
        "dense_feature_names": list(smartdispatch.DENSE_FEATURE_NAMES),
        "model_ids": list(MODEL_IDS),
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "feature_mean": [float(value) for value in mean],
        "feature_scale": [float(value) for value in scale],
        "score_heads": {
            model_id: _head_dict(
                quality_intercept[index], quality_coefficients[:, index]
            )
            for index, model_id in enumerate(MODEL_IDS)
        },
        "log_cost_heads": {
            model_id: _head_dict(cost_intercept[index], cost_coefficients[:, index])
            for index, model_id in enumerate(MODEL_IDS)
        },
        "gain_heads": {
            key: _head_dict(
                quality_intercept[SCORE_COLUMNS + index],
                quality_coefficients[:, SCORE_COLUMNS + index],
            )
            for index, key in enumerate(smartdispatch.GAIN_KEYS)
        },
        "selection_mode": selection_mode,
        "tier_safety_ratios": {
            tier: float(safety_ratios[tier]) for tier in TIERS
        },
        "premium_fill_safety_ratio": float(fill_safety),
        "training_summary": dict(training_summary),
    }


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o644)
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def train(
    *,
    input_path: Path,
    outcomes_path: Path,
    artifact_path: Path,
    report_path: Path,
    policy: RoutingPolicy,
    word_bins: int,
    char_bins: int,
    selection_mode: str,
    requested_folds: int,
    alpha_candidates: Sequence[float],
    cost_alpha_candidates: Sequence[float],
    safety_grid_size: int,
    fill_safety: float,
    validation_input_path: Optional[Path] = None,
    validation_outcomes_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    _require_numpy()
    inputs = load_input(input_path)
    outcomes = load_outcomes(outcomes_path)
    if inputs.schema_version != policy.schema_version:
        raise ProtocolError("Train 입력과 정책의 schema_version이 다릅니다.")
    if (validation_input_path is None) != (validation_outcomes_path is None):
        raise ValueError("Dev 입력과 outcome 경로는 함께 지정해야 합니다.")
    folds = min(requested_folds, len(inputs.episodes))
    matrix, targets = _training_matrix(
        inputs, outcomes, policy, word_bins, char_bins
    )
    quality_targets = np.concatenate(
        [targets[:, :SCORE_COLUMNS], targets[:, SCORE_COLUMNS + COST_COLUMNS :]],
        axis=1,
    )
    cost_targets = targets[:, SCORE_COLUMNS : SCORE_COLUMNS + COST_COLUMNS]

    quality_alpha, quality_oof, quality_diag = _select_alpha_for_group(
        matrix, quality_targets, folds=folds, candidates=alpha_candidates
    )
    cost_alpha, cost_oof, cost_diag = _select_alpha_for_group(
        matrix, cost_targets, folds=folds, candidates=cost_alpha_candidates
    )

    oof_scores, oof_costs = _prediction_rows(quality_oof, cost_oof, selection_mode)
    safety_ratios, oof_reports = _calibrate_safety(
        inputs,
        outcomes,
        policy,
        oof_scores,
        oof_costs,
        safety_grid_size,
        fill_safety,
        require_pass=False,
    )

    q_mean, q_scale, q_intercept, q_coefficients = _fit_ridge(
        matrix, quality_targets, quality_alpha
    )
    c_mean, c_scale, c_intercept, c_coefficients = _fit_ridge(
        matrix, cost_targets, cost_alpha
    )
    # A single standardization is stored in the artifact; both fits use the
    # full matrix so mean/scale are identical between the two groups.
    training_summary = {
        "num_episodes": len(inputs.episodes),
        "folds": folds,
        "quality_ridge_alpha": quality_alpha,
        "cost_ridge_alpha": cost_alpha,
        "word_hash_bins": word_bins,
        "char_hash_bins": char_bins,
        "selection_mode": selection_mode,
        "input_sha256": _file_sha256(input_path),
        "outcomes_sha256": _file_sha256(outcomes_path),
        "optimizer": "numpy-ridge-splitalpha-oof-grid-v1",
    }
    initial_value = _artifact_dict(
        word_bins=word_bins,
        char_bins=char_bins,
        policy=policy,
        mean=q_mean,
        scale=q_scale,
        quality_intercept=q_intercept,
        quality_coefficients=q_coefficients,
        cost_intercept=c_intercept,
        cost_coefficients=c_coefficients,
        selection_mode=selection_mode,
        safety_ratios=safety_ratios,
        fill_safety=fill_safety,
        training_summary=training_summary,
    )
    validation_reports = None
    if validation_input_path is not None and validation_outcomes_path is not None:
        validation_inputs = load_input(validation_input_path)
        validation_outcomes = load_outcomes(validation_outcomes_path)
        artifact = smartdispatch.parse_artifact(initial_value)
        predictions = [
            smartdispatch.predict_episode(episode, artifact)
            for episode in validation_inputs.episodes
        ]
        val_scores = [item[0] for item in predictions]
        val_costs = [item[1] for item in predictions]
        safety_ratios, validation_reports = _calibrate_safety(
            validation_inputs,
            validation_outcomes,
            policy,
            val_scores,
            val_costs,
            safety_grid_size,
            fill_safety,
            require_pass=True,
        )
        training_summary.update(
            {
                "validation_num_episodes": len(validation_inputs.episodes),
                "validation_input_sha256": _file_sha256(validation_input_path),
                "validation_outcomes_sha256": _file_sha256(validation_outcomes_path),
            }
        )
    artifact_value = _artifact_dict(
        word_bins=word_bins,
        char_bins=char_bins,
        policy=policy,
        mean=q_mean,
        scale=q_scale,
        quality_intercept=q_intercept,
        quality_coefficients=q_coefficients,
        cost_intercept=c_intercept,
        cost_coefficients=c_coefficients,
        selection_mode=selection_mode,
        safety_ratios=safety_ratios,
        fill_safety=fill_safety,
        training_summary=training_summary,
    )
    artifact = smartdispatch.parse_artifact(artifact_value)
    submissions = [
        smartdispatch.make_smartdispatch_submission(
            inputs, policy, artifact, tier
        ).submission
        for tier in TIERS
    ]
    fitted_report = score_submissions(inputs, outcomes, submissions, policy)
    report = {
        "report_type": "smartdispatch-training-v1",
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256(policy),
        "training_summary": training_summary,
        "feature_dimension": int(matrix.shape[1]),
        "quality_alpha_objectives": quality_diag,
        "cost_alpha_objectives": cost_diag,
        "oof_tier_selection": oof_reports,
        "fitted_train_self_check": fitted_report,
    }
    if validation_reports is not None:
        report["validation_safety_calibration"] = validation_reports
    _write_json_atomic(artifact_path, artifact_value)
    _write_json_atomic(report_path, report)
    return report


def _positive_float_list(value: str) -> Tuple[float, ...]:
    try:
        result = tuple(float(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("alpha 목록을 해석할 수 없습니다.") from exc
    if not result or any(not math.isfinite(item) or item <= 0 for item in result):
        raise argparse.ArgumentTypeError("alpha는 0보다 큰 유한한 수여야 합니다.")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="공개 Train으로 SmartDispatch 라우터를 학습합니다."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--validation-input", type=Path)
    parser.add_argument("--validation-outcomes", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--word-hash-bins", type=int, default=1024)
    parser.add_argument("--char-hash-bins", type=int, default=4096)
    parser.add_argument(
        "--selection-mode", choices=("per-model", "gain"), default="per-model"
    )
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument(
        "--alphas",
        type=_positive_float_list,
        default=_positive_float_list("300,1000,3000,10000,30000"),
    )
    parser.add_argument(
        "--cost-alphas",
        type=_positive_float_list,
        default=_positive_float_list("300,1000,3000,10000,30000"),
    )
    parser.add_argument("--safety-grid-size", type=int, default=121)
    parser.add_argument("--premium-fill-safety", type=float, default=0.65)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = (
            load_policy(args.policy)
            if args.policy is not None
            else load_bundled_policy()
        )
        report = train(
            input_path=args.input,
            outcomes_path=args.outcomes,
            artifact_path=args.artifact,
            report_path=args.report,
            policy=policy,
            word_bins=args.word_hash_bins,
            char_bins=args.char_hash_bins,
            selection_mode=args.selection_mode,
            requested_folds=args.folds,
            alpha_candidates=args.alphas,
            cost_alpha_candidates=args.cost_alphas,
            safety_grid_size=args.safety_grid_size,
            fill_safety=args.premium_fill_safety,
            validation_input_path=args.validation_input,
            validation_outcomes_path=args.validation_outcomes,
        )
    except (OSError, ProtocolError, RuntimeError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(
        "OK: smartdispatch artifact를 생성했습니다 "
        f"(Train self-check {report['fitted_train_self_check']['final_score']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
