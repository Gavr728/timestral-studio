"""Local Timestral Studio server.

Run with: py -3 timestral_studio_server.py
Then open: http://127.0.0.1:8765
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import torch
import numpy as np
import pandas as pd
from huggingface_hub import constants as hf_constants
from timestral import TimestralForecaster


ROOT = Path(__file__).resolve().parent
UI_FILE = ROOT / "timestral_studio_ui.html"
CACHE_DIR = Path(os.environ.get("TIMESTRAL_CACHE") or hf_constants.HF_HUB_CACHE)
HOST, PORT = "127.0.0.1", 8765

MODELS = {
    "5": "mistralai/Timestral-5M",
    "28": "mistralai/Timestral-28M",
    "123": "mistralai/Timestral-123M",
    "209": "mistralai/Timestral-209M",
}
INTERVALS = {
    "50": (0.25, 0.75),
    "80": (0.10, 0.90),
    "90": (0.05, 0.95),
    "98": (0.01, 0.99),
}
CAL_COL_RE = r"^(год|месяц|день|квартал|недел[яи]|день_недели|year|month|day|week|quarter|dayofweek)$"

_models: dict[tuple[str, str], TimestralForecaster] = {}
_load_lock = threading.Lock()
_status: dict[str, Any] = {"loading": None, "task": None}


def dir_size(folder: Path) -> int:
    total = 0
    try:
        for item in folder.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def repo_size(repo: str) -> int | None:
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo, files_metadata=True)
        return sum(item.size or 0 for item in info.siblings) or None
    except Exception:
        return None


def on_disk(repo: str) -> bool:
    snapshots = CACHE_DIR / ("models--" + repo.replace("/", "--")) / "snapshots"
    return snapshots.exists() and any(snapshots.glob("*/*"))


def tensor_values(value: torch.Tensor) -> np.ndarray:
    return value.detach().cpu().numpy()


def load_model(size: str, requested_device: str) -> TimestralForecaster:
    device = "cuda" if requested_device == "cuda" and torch.cuda.is_available() else "cpu"
    key = size, device
    if key in _models:
        return _models[key]
    with _load_lock:
        if key not in _models:
            repo = MODELS[size]
            _status["loading"] = {
                "model": f"Timestral-{size}M", "size": size, "device": device, "since": time.time(),
                "dir": str(CACHE_DIR / ("models--" + repo.replace("/", "--"))), "total": None,
            }
            if not on_disk(repo):
                _status["loading"]["total"] = repo_size(repo)
            print(f"Загрузка Timestral-{size}M ({device})…")
            try:
                import huggingface_hub.file_download as file_download
                from huggingface_hub.utils import disable_progress_bars

                disable_progress_bars()
                file_download.are_symlinks_supported = lambda cache_dir=None: False
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                _models[key] = TimestralForecaster.from_pretrained(
                    MODELS[size], device=device, cache_dir=str(CACHE_DIR)
                )
                print(f"Timestral-{size}M загружена.")
            finally:
                _status["loading"] = None
    return _models[key]


def call_forecast(model: TimestralForecaster, context: np.ndarray, horizon: int, **kwargs: Any) -> Any:
    sig = inspect.signature(model.forecast).parameters
    filtered = {k: v for k, v in kwargs.items() if k in sig and v is not None}
    return model.forecast(context, prediction_length=horizon, **filtered)


def regularize_grid(frame: pd.DataFrame, time_col: str, target_col: str) -> pd.DataFrame:
    if len(frame) < 3:
        return frame
    obs_mask = frame[target_col].notna()
    if not obs_mask.any():
        return frame
    last_obs_idx = int(np.flatnonzero(obs_mask.to_numpy())[-1])
    hist_part = frame.iloc[: last_obs_idx + 1]
    tail_part = frame.iloc[last_obs_idx + 1 :]
    dates = pd.DatetimeIndex(hist_part[time_col])
    try:
        freq = pd.infer_freq(dates)
    except ValueError:
        freq = None
    if not freq and len(dates) >= 3:
        days = dates.to_series().diff().dropna().median() / pd.Timedelta(days=1)
        if 0.95 <= days <= 1.05:
            freq = "D"
        elif 6.8 <= days <= 7.2:
            freq = "W"
        elif 27.5 <= days <= 31.5 and (dates.day == 1).all():
            freq = "MS"
    if not freq:
        return frame
    full_idx = pd.date_range(dates[0], dates[-1], freq=freq)
    if len(full_idx) <= len(hist_part) or len(full_idx) > len(hist_part) * 3:
        return frame
    reindexed = hist_part.set_index(time_col).reindex(full_idx).rename_axis(time_col).reset_index()
    if len(tail_part):
        return pd.concat([reindexed, tail_part], ignore_index=True)
    return reindexed


def selected_rows(payload: dict[str, Any]) -> tuple[list[str], list[list[Any]], dict[str, Any]]:
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    mapping = payload.get("mapping") or {}
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("Файл не распознан: ожидаются заголовки и строки.")
    time_col = mapping.get("time")
    target_col = mapping.get("target")
    if time_col not in columns or target_col not in columns:
        raise ValueError("Выберите столбцы даты и целевого показателя.")
    return columns, rows, mapping


def grouped_frames(payload: dict[str, Any]) -> list[tuple[str, pd.DataFrame, dict[str, Any]]]:
    columns, rows, mapping = selected_rows(payload)
    frame = pd.DataFrame(rows, columns=columns)
    time_col = mapping["time"]
    target_col = mapping["target"]
    id_col = mapping.get("id") or None

    try:
        frame[time_col] = pd.to_datetime(frame[time_col], format="mixed", errors="coerce")
    except TypeError:
        frame[time_col] = pd.to_datetime(frame[time_col], errors="coerce")
    if frame[time_col].isna().any():
        raise ValueError("В столбце даты есть нераспознанные или пустые значения. Проверьте столбец даты.")
    frame[target_col] = pd.to_numeric(
        frame[target_col].astype(str).str.replace(r"[\s\u00a0]", "", regex=True).str.replace(",", "."),
        errors="coerce",
    )

    if id_col and id_col in frame.columns:
        raw_groups = [(str(name), group.sort_values(time_col).reset_index(drop=True)) for name, group in frame.groupby(id_col, dropna=False)]
    else:
        raw_groups = [("Общий ряд", frame.sort_values(time_col).reset_index(drop=True))]

    groups = []
    for name, group in raw_groups:
        repeats = int(group[time_col].duplicated().sum())
        if repeats:
            raise ValueError(f"В ряду «{name}» найдено {repeats} повторяющихся дат. Выберите столбец в поле «Разбить на отдельные ряды».")
        groups.append((name, regularize_grid(group, time_col, target_col), mapping))
    return groups


def split_covariates(
    frame: pd.DataFrame, columns: list[str], count: int, label: str
) -> tuple[np.ndarray | None, np.ndarray | None]:
    if not columns:
        return None, None
    num_arrays: list[np.ndarray] = []
    cat_arrays: list[np.ndarray] = []
    for column in columns:
        if column not in frame.columns:
            raise ValueError(f"Не найден фактор «{column}».")
        raw_series = frame[column].iloc[:count]
        if len(raw_series) < count:
            raise ValueError(f"Для фактора «{column}» недостаточно строк ({label}).")
        cleaned = raw_series.astype(str).str.replace(r"[\s\u00a0]", "", regex=True).str.replace(",", ".")
        num_vals = pd.to_numeric(cleaned, errors="coerce")
        non_empty = raw_series.notna() & (raw_series.astype(str).str.strip() != "")
        is_cal = pd.Series([column.strip()]).str.match(CAL_COL_RE, case=False).iloc[0]
        is_numeric = non_empty.sum() > 0 and (num_vals.notna().sum() / non_empty.sum() >= 0.9) and not is_cal
        if is_numeric:
            num_arrays.append(num_vals.to_numpy(dtype=np.float32))
        else:
            codes, _ = pd.factorize(raw_series.where(non_empty, np.nan))
            cat_vals = codes.astype(np.float32)
            cat_vals[codes < 0] = np.nan
            cat_arrays.append(cat_vals)
    num_mat = np.asarray(num_arrays, dtype=np.float32)[None, :, :] if num_arrays else None
    cat_mat = np.asarray(cat_arrays, dtype=np.float32)[None, :, :] if cat_arrays else None
    return num_mat, cat_mat


def calendar_covariates(dates: pd.DatetimeIndex) -> np.ndarray | None:
    if len(dates) < 2:
        return None
    days = dates.to_series().diff().dropna().median() / pd.Timedelta(days=1)
    feats: list[np.ndarray] = []
    if days <= 1.5:
        feats.append(dates.dayofweek.to_numpy(dtype=np.float32))
        feats.append(dates.month.to_numpy(dtype=np.float32))
    elif days <= 8.0:
        feats.append(dates.month.to_numpy(dtype=np.float32))
    elif days <= 35.0:
        feats.append(dates.month.to_numpy(dtype=np.float32))
        feats.append(dates.quarter.to_numpy(dtype=np.float32))
    if not feats:
        return None
    return np.asarray(feats, dtype=np.float32)[None, :, :]


def future_dates(history: pd.DatetimeIndex, horizon: int) -> list[pd.Timestamp]:
    last = history[-1]
    if len(history) >= 3:
        try:
            freq = pd.infer_freq(history)
        except ValueError:
            freq = None
        if freq:
            return list(pd.date_range(last, periods=horizon + 1, freq=freq)[1:])
    if len(history) < 2:
        return [last + timedelta(days=index + 1) for index in range(horizon)]
    step = history.to_series().diff().dropna().median()
    days = step / pd.Timedelta(days=1)
    for months, low, high in ((1, 28, 31), (3, 89, 92), (12, 365, 366)):
        if low <= days <= high:
            return [last + pd.DateOffset(months=months * (index + 1)) for index in range(horizon)]
    return [last + step * (index + 1) for index in range(horizon)]


def forecast_one(
    name: str,
    frame: pd.DataFrame,
    mapping: dict[str, Any],
    horizon: int,
    model: TimestralForecaster,
    holdout: int = 0,
    auto_calendar: bool = True,
    clip_nonnegative: bool = True,
    quantiles: tuple[float, float] = (0.10, 0.90),
) -> tuple[list[dict[str, Any]], list[float], list[float]]:
    time_col, target_col = mapping["time"], mapping["target"]
    past_cols = mapping.get("past") or []
    known_cols = mapping.get("known") or []
    target = frame[target_col].to_numpy(dtype=np.float32)
    observed = np.flatnonzero(~np.isnan(target))
    if not len(observed):
        raise ValueError(f"В ряду «{name}» нет ни одного числового значения в целевом столбце.")
    last_observed = int(observed[-1]) - holdout
    if last_observed < 7:
        raise ValueError(f"В ряду «{name}» слишком короткая история (нужно хотя бы 8 точек).")
    context_end = last_observed + 1
    context = target[:context_end]
    if np.isnan(context).all():
        raise ValueError(f"В ряду «{name}» нет истории для прогноза.")
    full_needed = context_end + horizon
    if known_cols and len(frame) < full_needed:
        raise ValueError(f"В ряду «{name}» не хватает будущих строк ({horizon} шт.) для заранее известных факторов.")

    hist_dates = pd.DatetimeIndex(pd.to_datetime(frame[time_col].iloc[:context_end]))
    if known_cols:
        fut_dates = pd.to_datetime(frame[time_col].iloc[context_end : context_end + horizon]).tolist()
    else:
        fut_dates = future_dates(hist_dates, horizon)

    past_num, past_cat = split_covariates(frame, past_cols, context_end, "истории")
    known_num, known_cat = split_covariates(frame, known_cols, full_needed, "будущего горизонта")

    if auto_calendar:
        all_dates = pd.DatetimeIndex(list(hist_dates) + [pd.Timestamp(d) for d in fut_dates])
        cal_cat = calendar_covariates(all_dates)
        if cal_cat is not None:
            known_cat = np.concatenate([known_cat, cal_cat], axis=1) if known_cat is not None else cal_cat

    result = call_forecast(
        model,
        context[None, None, :],
        horizon,
        past_covariates=past_num,
        known_covariates=known_num,
        categorical_past_covariates=past_cat,
        categorical_known_covariates=known_cat,
        clip_nonnegative=clip_nonnegative,
    )
    q_low, q_high = quantiles
    median = tensor_values(result.median)[0, 0]
    low = tensor_values(result.quantile(q_low))[0, 0]
    high = tensor_values(result.quantile(q_high))[0, 0]

    rows = [
        {
            "category": name,
            "date": pd.Timestamp(date).isoformat(),
            "median": float(median[index]),
            "p10": float(low[index]),
            "p90": float(high[index]),
        }
        for index, date in enumerate(fut_dates)
    ]
    return rows, median.tolist(), target[context_end : context_end + horizon].tolist()


def try_multivariate_forecast(
    groups: list[tuple[str, pd.DataFrame, dict[str, Any]]],
    horizon: int,
    model: TimestralForecaster,
    auto_calendar: bool,
    clip_nonnegative: bool,
    quantiles: tuple[float, float],
) -> list[dict[str, Any]] | None:
    if len(groups) < 2:
        return None
    mapping = groups[0][2]
    if mapping.get("past") or mapping.get("known"):
        return None
    time_col, target_col = mapping["time"], mapping["target"]
    series_map: dict[str, pd.Series] = {}
    for name, frame, _ in groups:
        obs = frame[target_col].notna()
        if obs.sum() < 8:
            return None
        s = frame.set_index(time_col)[target_col]
        series_map[name] = s
    combined = pd.DataFrame(series_map).sort_index()
    if len(combined) < 8:
        return None
    mat = combined.to_numpy(dtype=np.float32).T[None, :, :]  # (1, V, T)
    hist_dates = pd.DatetimeIndex(combined.index)
    fut_dates = future_dates(hist_dates, horizon)
    all_dates = pd.DatetimeIndex(list(hist_dates) + [pd.Timestamp(d) for d in fut_dates])
    cal_cat = calendar_covariates(all_dates) if auto_calendar else None

    result = call_forecast(
        model,
        mat,
        horizon,
        categorical_known_covariates=cal_cat,
        clip_nonnegative=clip_nonnegative,
    )
    q_low, q_high = quantiles
    med = tensor_values(result.median)[0]
    lo = tensor_values(result.quantile(q_low))[0]
    hi = tensor_values(result.quantile(q_high))[0]
    names = list(series_map.keys())
    all_rows: list[dict[str, Any]] = []
    for v_idx, name in enumerate(names):
        for t_idx, date in enumerate(fut_dates):
            all_rows.append({
                "category": name,
                "date": pd.Timestamp(date).isoformat(),
                "median": float(med[v_idx, t_idx]),
                "p10": float(lo[v_idx, t_idx]),
                "p90": float(hi[v_idx, t_idx]),
            })
    return all_rows


def extra_steps(frame: pd.DataFrame, mapping: dict[str, Any], global_last: pd.Timestamp | None) -> int:
    if global_last is None or mapping.get("known"):
        return 0
    target = frame[mapping["target"]].to_numpy(dtype=np.float32)
    observed = np.flatnonzero(~np.isnan(target))
    if not len(observed):
        return 0
    history = pd.DatetimeIndex(pd.to_datetime(frame[mapping["time"]].iloc[: int(observed[-1]) + 1]))
    if history[-1] >= global_last:
        return 0
    return sum(1 for date in future_dates(history, 120) if date <= global_last)


def run_forecast(payload: dict[str, Any]) -> dict[str, Any]:
    size = str(payload.get("model", "5"))
    if size not in MODELS:
        raise ValueError("Неизвестный размер модели.")
    horizon = int(payload.get("horizon", 30))
    if horizon < 1 or horizon > 1024:
        raise ValueError("Горизонт прогноза должен быть от 1 до 1024 шагов.")
    device = "cuda" if str(payload.get("device", "auto")).lower() == "cuda" else "cpu"
    auto_calendar = bool(payload.get("auto_calendar", True))
    clip_nonnegative = bool(payload.get("clip_nonnegative", True))
    multivariate = bool(payload.get("multivariate", False))
    interval_key = str(payload.get("interval", "80"))
    quantiles = INTERVALS.get(interval_key, (0.10, 0.90))

    model = load_model(size, device)
    groups = grouped_frames(payload)

    if multivariate and len(groups) > 1:
        _status["task"] = {"done": 0, "total": len(groups)}
        mv_rows = try_multivariate_forecast(groups, horizon, model, auto_calendar, clip_nonnegative, quantiles)
        if mv_rows is not None:
            return {"rows": mv_rows, "model": f"Timestral-{size}M", "horizon": horizon, "interval": interval_key}

    all_rows: list[dict[str, Any]] = []
    last_dates = []
    for _, frame, mapping in groups:
        target = frame[mapping["target"]].to_numpy(dtype=np.float32)
        observed = np.flatnonzero(~np.isnan(target))
        if len(observed):
            last_dates.append(pd.Timestamp(frame[mapping["time"]].iloc[int(observed[-1])]))
    global_last = max(last_dates) if last_dates else None
    for index, (name, frame, mapping) in enumerate(groups):
        _status["task"] = {"done": index, "total": len(groups)}
        extra = extra_steps(frame, mapping, global_last)
        rows, _, _ = forecast_one(
            name, frame, mapping, min(horizon + extra, 1024), model,
            auto_calendar=auto_calendar, clip_nonnegative=clip_nonnegative, quantiles=quantiles,
        )
        for row in rows:
            if global_last is not None and pd.Timestamp(row["date"]) <= global_last:
                row["fill"] = True
        all_rows.extend(rows)
    return {"rows": all_rows, "model": f"Timestral-{size}M", "horizon": horizon, "interval": interval_key}


def wape(pred: np.ndarray, act: np.ndarray) -> float | None:
    denom = float(np.sum(np.abs(act)))
    if not denom:
        return None
    value = float(np.sum(np.abs(pred - act)) / denom)
    return value if np.isfinite(value) else None


def season_length(dates: pd.Series) -> int | None:
    if len(dates) < 3:
        return None
    days = dates.diff().dropna().dt.total_seconds().median() / 86400
    if days <= 1.5:
        return 7
    if 6 <= days <= 8:
        return 52
    if 27 <= days <= 32:
        return 12
    return None


def run_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    size = str(payload.get("model", "5"))
    if size not in MODELS:
        raise ValueError("Неизвестный размер модели.")
    horizon = max(1, int(payload.get("horizon", 30)))
    folds = 3
    device = "cuda" if str(payload.get("device", "auto")).lower() == "cuda" else "cpu"
    auto_calendar = bool(payload.get("auto_calendar", True))
    clip_nonnegative = bool(payload.get("clip_nonnegative", True))
    model = load_model(size, device)

    P: list[float] = []
    A: list[float] = []
    N: list[float] = []
    S: list[float] = []
    recs: list[tuple[str, float, float, float]] = []
    per_series: list[dict[str, Any]] = []
    used_folds: list[int] = []
    used_windows: list[int] = []
    last_error: Exception | None = None
    groups = grouped_frames(payload)

    for index, (name, frame, mapping) in enumerate(groups):
        _status["task"] = {"done": index, "total": len(groups)}
        target = frame[mapping["target"]].to_numpy(dtype=np.float32)
        observed = np.flatnonzero(~np.isnan(target))
        if not len(observed):
            continue
        n = int(observed[-1]) + 1
        h = min(horizon, max(1, len(observed) // 4))
        season = season_length(pd.to_datetime(frame[mapping["time"]]))
        sp: list[float] = []
        sa: list[float] = []
        sn: list[float] = []
        folds_ran = 0
        for fold in range(folds):
            holdout = h * (fold + 1)
            end = n - holdout
            if end < 8:
                break
            try:
                _, predicted, actual = forecast_one(
                    name, frame, mapping, h, model, holdout=holdout,
                    auto_calendar=auto_calendar, clip_nonnegative=clip_nonnegative,
                )
            except ValueError as error:
                last_error = error
                break
            context = target[:end]
            dates = pd.to_datetime(frame[mapping["time"]]).iloc[end : end + h].reset_index(drop=True)
            valid = context[~np.isnan(context)]
            last = float(valid[-1]) if len(valid) else float("nan")
            had_valid_point = False
            for i, (p, a) in enumerate(zip(predicted, actual)):
                if np.isnan(a):
                    continue
                had_valid_point = True
                sp.append(float(p))
                sa.append(float(a))
                sn.append(last)
                recs.append((dates.iloc[i].isoformat(), float(p), float(a), last))
                S.append(float(context[end - season + i % season]) if season and end >= season else float("nan"))
            if had_valid_point:
                folds_ran += 1
        if sa and folds_ran > 0:
            pa, aa, na = np.asarray(sp), np.asarray(sa), np.asarray(sn)
            P.extend(sp)
            A.extend(sa)
            N.extend(sn)
            used_folds.append(folds_ran)
            used_windows.append(h)
            per_series.append({
                "name": name,
                "points": len(sa),
                "wape": wape(pa, aa),
                "naive_wape": wape(na, aa),
                "actual_sum": float(np.abs(aa).sum()),
            })
    if not A:
        raise ValueError(str(last_error) if last_error else "Слишком короткая история: для проверки нужно минимум 9 известных значений в ряду.")

    f_min = min(used_folds) if used_folds else folds
    f_max = max(used_folds) if used_folds else folds
    w_min = min(used_windows) if used_windows else horizon
    w_max = max(used_windows) if used_windows else horizon

    by_date: dict[str, list[float]] = {}
    for date, pred_value, actual_value, naive_value in recs:
        entry = by_date.setdefault(date, [0.0, 0.0, 0.0, 0.0])
        entry[0] += actual_value
        entry[1] += pred_value
        entry[2] += naive_value
        entry[3] += 1
    ordered = sorted(by_date.items())

    if ordered:
        max_series_on_date = max(e[3] for _, e in ordered)
        min_required = max(1, int(max_series_on_date * 0.5)) if max_series_on_date > 2 else 1
        filtered_ordered = [(d, e) for d, e in ordered if e[3] >= min_required]
        if filtered_ordered:
            ordered = filtered_ordered

    max_dates = f_max * w_max
    if len(ordered) > max_dates:
        ordered = ordered[-max_dates:]

    ta = np.asarray([e[0] for _, e in ordered])
    tp = np.asarray([e[1] for _, e in ordered])
    tn = np.asarray([e[2] for _, e in ordered])
    total = {
        "wape": wape(tp, ta),
        "naive_wape": wape(tn, ta),
        "points": [{"date": d, "actual": e[0], "predicted": e[1], "naive": e[2], "series": int(e[3])} for d, e in ordered],
    }
    pa, aa, na, sa_ = (np.asarray(x, dtype=float) for x in (P, A, N, S))
    baselines = [{"name": "Простой прогноз («как в прошлой точке»)", "wape": wape(na, aa), "model_wape": wape(pa, aa)}]
    mask = ~np.isnan(sa_)
    if mask.any():
        baselines.append({"name": "Сезонный прогноз («как в прошлом периоде/сезоне»)", "wape": wape(sa_[mask], aa[mask]), "model_wape": wape(pa[mask], aa[mask])})
    per_series.sort(key=lambda item: item["actual_sum"], reverse=True)
    return {
        "points": len(A),
        "mae": float(np.mean(np.abs(pa - aa))),
        "rmse": float(np.sqrt(np.mean((pa - aa) ** 2))),
        "mape": None,
        "wape": wape(pa, aa),
        "baselines": baselines,
        "series": per_series,
        "series_total": len(per_series),
        "skipped": len(groups) - len(per_series),
        "total": total,
        "folds": f_max,
        "folds_min": f_min,
        "horizon": w_max,
        "horizon_min": w_min,
        "model": f"Timestral-{size}M",
    }


INJECTED_SCRIPT = r"""
<script>
(() => {
  const root = document.getElementById('timestral-studio');
  const run = root.querySelector('#run-forecast');
  const history = root.querySelector('#run-backtest');
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const pluralSteps = n => {
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return `${n} шаг`;
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return `${n} шага`;
    return `${n} шагов`;
  };
  const selection = () => ({
    time: root.querySelector('#time-col').value,
    target: root.querySelector('#target-col').value,
    id: root.querySelector('#id-col').value,
    past: [...root.querySelector('#past-cov').selectedOptions].map(x => x.value).filter(Boolean),
    known: [...root.querySelector('#known-cov').selectedOptions].map(x => x.value).filter(Boolean),
  });
  const payload = () => ({
    columns: window.timestralUploaded.columns,
    rows: window.timestralUploaded.rows,
    mapping: selection(),
    horizon: Number(root.querySelector('#horizon').value || 30),
    model: root.querySelector('#model').value,
    device: root.querySelector('#device').value === 'GPU (CUDA)' ? 'cuda' : 'cpu',
    auto_calendar: root.querySelector('#toggle-cal') ? root.querySelector('#toggle-cal').checked : true,
    clip_nonnegative: root.querySelector('#toggle-zero') ? root.querySelector('#toggle-zero').checked : true,
    multivariate: root.querySelector('#toggle-mv') ? root.querySelector('#toggle-mv').checked : false,
    interval: root.querySelector('#interval-sel') ? root.querySelector('#interval-sel').value : '80',
  });
  const request = async (path, body) => {
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Не удалось выполнить расчёт.');
    return data;
  };
  run.textContent = 'Построить прогноз';
  run.addEventListener('click', async () => {
    if (!window.timestralUploaded) return;
    run.disabled = true; run.textContent = 'Модель считает…';
    try {
      const result = await request('/api/forecast', payload());
      root.querySelector('#results-empty').hidden = true;
      root.querySelector('#results-content').hidden = false;
      root.querySelector('#results-title').textContent = `${result.model}: прогноз на ${pluralSteps(result.horizon)}`;
      root.querySelector('#kpi-horizon').textContent = pluralSteps(result.horizon);
      root.querySelector('#kpi-model').textContent = result.model.replace('Timestral-', '');
    } catch (error) { root.querySelector('#run-status').textContent = error.message; }
    finally { run.disabled = false; run.textContent = 'Построить прогноз'; }
  });
  history.addEventListener('click', async () => {
    if (!window.timestralUploaded) return;
    if (window.clearBacktest) window.clearBacktest();
    history.disabled = true; history.textContent = 'Проверяем точность…';
    try {
      const result = await request('/api/backtest', payload());
      if (window.renderBacktest) window.renderBacktest(result); else root.querySelector('#backtest-state').textContent = JSON.stringify(result);
    } catch (error) {
      if (window.clearBacktest) window.clearBacktest();
      root.querySelector('#backtest-state').innerHTML = `<div class="ts-alert ts-alert-err"><b>Не удалось провести проверку:</b> ${esc(error.message)}</div>`;
    }
    finally { history.disabled = false; history.textContent = 'Проверить точность на истории'; }
  });
})();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self, "path", "") == "/api/status":
            return
        print(f"{self.address_string()} — {format % args}")

    def send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            loading = _status["loading"]
            self.send_json(HTTPStatus.OK, {
                "loading": {**loading, "elapsed": time.time() - loading["since"], "bytes": dir_size(Path(loading["dir"]))} if loading else None,
                "task": _status["task"],
                "loaded": [f"{size}/{device}" for size, device in _models],
                "cuda": bool(torch.cuda.is_available()),
                "on_disk": {size: on_disk(repo) for size, repo in MODELS.items()},
            })
            return
        if self.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        source = UI_FILE.read_text(encoding="utf-8")
        page = f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Timestral Studio</title></head><body style='margin:0'>{source}{INJECTED_SCRIPT}</body></html>"
        body = page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path not in {"/api/forecast", "/api/backtest"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = run_forecast(payload) if self.path == "/api/forecast" else run_backtest(payload)
            self.send_json(HTTPStatus.OK, result)
        except (ValueError, KeyError, TypeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Ошибка Timestral: {error}"})
        finally:
            _status["task"] = None


if __name__ == "__main__":
    if not UI_FILE.exists():
        raise SystemExit(f"Не найден интерфейс: {UI_FILE}")
    print(f"Timestral Studio: http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()