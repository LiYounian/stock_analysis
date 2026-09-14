"""RegimeTimingModel —— regime 路线B 预测性择时参考实现(**归档,不接生产**)。

诊断结论:该模型在生产口径 hs300 上**未达显著门槛**(见 2026-09-14_regime路线B择时评估报告.md),
故按"只建不切、不显著不采纳"**留作归档 + 复现**,不接入 pipeline。待 hs300 真历史累积、
重跑 timing_eval.py 达标后再考虑升级(kill-switch 思路)。

模型:4 因子(指数多头/量能/宽度/涨跌停)标准化 → 训练集定向(与前瞻收益相关符号,自动实现量能/宽度倒挂取负)
→ 组权重(等权/IC加权/逻辑回归)→ 概率校准 → P(上涨)。**朝向/权重/校准只在训练集拟合**(防未来函数)。
非投资建议。
"""
from __future__ import annotations
import numpy as np, pandas as pd

FCOLS = ["指数多头", "量能", "宽度", "涨跌停"]


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class _LogReg:
    def __init__(self, l2=1.0, lr=0.3, epochs=600):
        self.l2, self.lr, self.epochs, self.w, self.b = l2, lr, epochs, None, 0.0

    def fit(self, X, y):
        n, d = X.shape
        self.w = np.zeros(d); self.b = 0.0
        for _ in range(self.epochs):
            p = _sigmoid(X @ self.w + self.b); g = p - y
            self.w -= self.lr * (X.T @ g / n + self.l2 * self.w / n)
            self.b -= self.lr * g.mean()
        return self

    def predict_proba(self, X):
        return _sigmoid(X @ self.w + self.b)


class RegimeTimingModel:
    """cols=FCOLS。method ∈ {equal, ic, logreg}。fit(X, fwd_ret);predict_proba(X)→P(上涨)。"""

    def __init__(self, method: str = "ic", cols=None):
        self.method = method
        self.cols = list(cols or FCOLS)
        self.mu = self.sd = self.orient = self.w = None
        self.cal = None
        self.lr = None

    def fit(self, X: pd.DataFrame, fwd_ret) -> "RegimeTimingModel":
        A = X[self.cols].to_numpy(float)
        fwd = np.asarray(fwd_ret, float)
        y = (fwd > 0).astype(float)
        self.mu = np.nanmean(A, 0); sd = np.nanstd(A, 0)
        self.sd = np.where(sd < 1e-9, 1.0, sd)
        Xs = np.clip(np.nan_to_num((A - self.mu) / self.sd), -4, 4)
        # 定向:与前瞻收益相关符号(倒挂→负)
        self.orient = np.array([np.sign(np.corrcoef(Xs[:, j], fwd)[0, 1])
                                if np.std(Xs[:, j]) > 1e-9 else 0.0 for j in range(Xs.shape[1])])
        Xo = Xs * self.orient
        if self.method == "logreg":
            self.lr = _LogReg().fit(Xo, y); return self
        if self.method == "ic":
            ic = np.array([max(0.0, np.corrcoef(Xo[:, j], fwd)[0, 1]) if np.std(Xo[:, j]) > 1e-9 else 0.0
                           for j in range(Xo.shape[1])])
            self.w = ic / (ic.sum() or 1.0)
        else:  # equal
            self.w = np.ones(len(self.cols)) / len(self.cols)
        comp = Xo @ self.w
        self.cal = _LogReg().fit(comp.reshape(-1, 1), y)
        return self

    def _oriented(self, X):
        A = X[self.cols].to_numpy(float)
        return np.clip(np.nan_to_num((A - self.mu) / self.sd), -4, 4) * self.orient

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        Xo = self._oriented(X)
        if self.method == "logreg":
            return self.lr.predict_proba(Xo)
        return self.cal.predict_proba((Xo @ self.w).reshape(-1, 1))

    def orientation(self) -> dict:
        return dict(zip(self.cols, [int(x) for x in self.orient])) if self.orient is not None else {}
