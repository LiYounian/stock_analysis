"""预测器(可解释因子加权 / 逻辑回归)——大盘预测的判别核心。

改造自 pattern_screener/regime.py 的"五因子加权"思路,但**加真标签 + 概率输出**:
四(现三)维因子 → 标准化 → 按训练集定向(与标签相关方向)→ 分维打分 → 组权重合成
→ 校准逻辑函数 → P(上涨) + 五档。

两个模型(都**非黑箱**):
  · CompositeModel —— 单一可解释综合分(每维贡献可追溯),v1 主模型。
  · LogisticModel  —— 全特征 L2 逻辑回归(numpy 自实现,无 sklearn 依赖),作对照。

标准化统计量 / 定向 / 系数**只用训练集拟合**(walk-forward 时训练集严格早于测试日),
保证无未来函数。predict_one 供每日生产:吃单日特征 → market_forecast.json。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS

from .features import FEATURE_COLS, _BREADTH_COLS, _SENTI_COLS, _TECH_COLS

_CFG = THRESHOLDS["大盘预测"]
_LABELS = _CFG["分档"]


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


# ————————————————————————— numpy L2 逻辑回归 —————————————————————————
class _LogReg:
    """极简 L2 正则逻辑回归(梯度下降)。自含,无 sklearn 依赖。"""

    def __init__(self, l2: float = 1.0, lr: float = 0.1, epochs: int = 500):
        self.l2, self.lr, self.epochs = l2, lr, epochs
        self.w = None
        self.b = 0.0

    def fit(self, X: np.ndarray, y: np.ndarray):
        n, d = X.shape
        self.w = np.zeros(d)
        self.b = 0.0
        for _ in range(self.epochs):
            p = _sigmoid(X @ self.w + self.b)
            g = p - y
            gw = X.T @ g / n + self.l2 * self.w / n
            gb = g.mean()
            self.w -= self.lr * gw
            self.b -= self.lr * gb
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return _sigmoid(X @ self.w + self.b)


# ————————————————————————— 标准化 —————————————————————————
class _Scaler:
    """标准化 + 温莎化(z 裁剪到 ±clip),防训练近零方差因子在测试日 z 爆掉。"""

    def __init__(self, clip: float = 4.0):
        self.clip = clip

    def fit(self, X):
        self.mu = np.nanmean(X, axis=0)
        sd = np.nanstd(X, axis=0)
        self.sd = np.where(sd < 1e-9, 1.0, sd)
        return self

    def transform(self, X):
        z = np.nan_to_num((X - self.mu) / self.sd, nan=0.0)
        return np.clip(z, -self.clip, self.clip)


# ————————————————————————— 档位映射 —————————————————————————
def bucket_label(fwd_or_idx, edges=None) -> str:
    """5 档序号(0..4)→ 标签名。"""
    i = int(round(float(fwd_or_idx)))
    i = max(0, min(len(_LABELS) - 1, i))
    return _LABELS[i]


def prob_to_bucket(p_up: float) -> int:
    """P(上涨) → 五档序号(等宽概率分档:<.35 大跌 .. >.65 大涨)。"""
    for i, hi in enumerate((0.35, 0.45, 0.55, 0.65)):
        if p_up <= hi:
            return i
    return 4


# ————————————————————————— 可解释综合模型 —————————————————————————
class CompositeModel:
    """三维因子加权综合分 → 校准 P(上涨)。每维贡献可追溯(报告/生产解释用)。"""

    def __init__(self, cfg=None):
        self.cfg = cfg or _CFG
        self.cols = list(FEATURE_COLS)
        self.scaler = _Scaler()
        self.orient = None       # 每特征定向符号(训练集 corr)
        self.calib = _LogReg(l2=1.0, lr=0.3, epochs=800)
        gw = self.cfg["因子权重"]
        self.group_w = {"技术": gw["技术"], "广度": gw["广度"], "消息面": gw["消息面"]}
        self.eff_group_w = dict(self.group_w)   # fit 时按覆盖率调整(见 fit)
        self._groups = {"技术": _TECH_COLS, "广度": _BREADTH_COLS, "消息面": _SENTI_COLS}

    def _dim_scores(self, Xs: np.ndarray) -> dict:
        """标准化+定向后,按维取均值 → {维: 分数向量}。"""
        col_idx = {c: i for i, c in enumerate(self.cols)}
        oriented = Xs * self.orient
        dims = {}
        for name, cols in self._groups.items():
            idx = [col_idx[c] for c in cols]
            dims[name] = oriented[:, idx].mean(axis=1)
        return dims

    def _composite_raw(self, Xs: np.ndarray) -> np.ndarray:
        dims = self._dim_scores(Xs)
        wsum = sum(self.eff_group_w.values()) or 1.0
        raw = sum(self.eff_group_w[k] * dims[k] for k in dims) / wsum
        return raw

    def fit(self, X: pd.DataFrame, y: np.ndarray):
        Xv = X[self.cols].to_numpy(dtype=float)
        self.scaler.fit(Xv)
        Xs = self.scaler.transform(Xv)
        # 消息面覆盖率:训练集里有非零消息面特征的样本占比 → 缩放该维权重,
        # 历史浅(几乎全 0)时消息面被自动降权,不喧宾夺主(诚实降级,可追溯)。
        col_idx = {c: i for i, c in enumerate(self.cols)}
        se_idx = [col_idx[c] for c in _SENTI_COLS]
        se_cov = float((np.abs(Xv[:, se_idx]).sum(axis=1) > 1e-9).mean()) if len(Xv) else 0.0
        self.se_coverage = se_cov
        self.eff_group_w = dict(self.group_w)
        self.eff_group_w["消息面"] = self.group_w["消息面"] * se_cov
        # 定向:每特征与标签的相关符号(训练集内)
        ori = np.zeros(Xs.shape[1])
        for j in range(Xs.shape[1]):
            col = Xs[:, j]
            if np.std(col) < 1e-9:
                continue
            c = np.corrcoef(col, y)[0, 1]
            ori[j] = np.sign(c) if not np.isnan(c) else 0.0
        self.orient = ori
        raw = self._composite_raw(Xs).reshape(-1, 1)
        self.calib.fit(raw, y)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        Xs = self.scaler.transform(X[self.cols].to_numpy(dtype=float))
        raw = self._composite_raw(Xs).reshape(-1, 1)
        return self.calib.predict_proba(raw)

    def explain(self, X: pd.DataFrame) -> dict:
        """单/多行 → 各维**加权**贡献(定向标准化均值 × 有效组权重 / Σ权重)。生产解释用。

        已温莎化(±4)+ 消息面按覆盖率降权,故贡献量级可比、不被稀疏消息面绑架。
        """
        Xs = self.scaler.transform(X[self.cols].to_numpy(dtype=float))
        dims = self._dim_scores(Xs)
        wsum = sum(self.eff_group_w.values()) or 1.0
        return {k: float(np.mean(self.eff_group_w[k] * v) / wsum) for k, v in dims.items()}


class LogisticModel:
    """全特征 L2 逻辑回归(对照模型)。"""

    def __init__(self, cfg=None):
        self.cfg = cfg or _CFG
        self.cols = list(FEATURE_COLS)
        self.scaler = _Scaler()
        self.clf = _LogReg(l2=2.0, lr=0.2, epochs=1200)

    def fit(self, X: pd.DataFrame, y: np.ndarray):
        Xs = self.scaler.transform(self.scaler.fit(
            X[self.cols].to_numpy(dtype=float)).transform(
            X[self.cols].to_numpy(dtype=float)))
        self.clf.fit(Xs, y)
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        Xs = self.scaler.transform(X[self.cols].to_numpy(dtype=float))
        return self.clf.predict_proba(Xs)

    def coef(self) -> dict:
        return {c: float(w) for c, w in zip(self.cols, self.clf.w)}


MODELS = {"composite": CompositeModel, "logistic": LogisticModel}
