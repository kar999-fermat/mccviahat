"""Matched E/N cross-pair contrasts with shared-run and crossed node/date dependence.

Inputs and previous analyses are read-only. Within each node x calendar-date
cell, every E run is compared with every N run (E1-N1, E1-N2, E2-N1, E2-N2).
Cross pairs reuse runs, so the model links pairs that share an E run, an N
run, a node/date cell, a node or a date. Only non-redundant pairs enter the
likelihood: in a 2+2 cell, E2-N2 = E2-N1 + E1-N2 - E1-N1 exactly.
"""
import re
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
from scipy.linalg import cho_factor, cho_solve, solve_triangular
from scipy.optimize import minimize
from scipy.stats import beta
from statsmodels.stats.multitest import multipletests

from fulltrace_node_day import load_recordings

SPECS = [
    ("core_power.throttle__mean_rate", "Mean throttle rate", "log_ratio", "% vs neutral"),
    ("core_power.throttle__variance", "Throttle variance", "log_ratio", "% vs neutral"),
    ("core_power.throttle__spectral_entropy", "Spectral entropy", "difference", "entropy units"),
    ("core_power.throttle__slope", "Throttle slope", "difference", "slope units"),
    ("core_power.throttle__per_cycle", "PTE / cycles", "log_ratio", "% vs neutral"),
]


def display_effect(values, scale):
    values = np.asarray(values)
    return 100 * np.expm1(values) if scale == "log_ratio" else values


def run_number(run_label):
    """Repeat number within a node/date, e.g. 244_2e, 222E2 and 232_E_2 -> 2."""
    match = re.match(r"^\d+\D*?(\d+)", run_label)
    if not match:
        raise ValueError(f"Cannot determine run number: {run_label}")
    return int(match.group(1))


def prepare(root, timezone="America/Chicago", features=None, first="emotional", second="neutral",
            exclude=()):
    """Cross pairs of `first` vs `second` runs. Columns named E_*/N_* hold the
    first/second condition (emotional/neutral by default). `exclude` lists
    (node, day) cells to drop before matching; they are reported in `excluded`."""
    specs = [s for s in SPECS if features is None or s[0] in features]
    if not specs or (features is not None and set(features) != {s[0] for s in specs}):
        raise ValueError("Select at least one known feature")
    traces, _, sources, excluded = load_recordings(root, timezone, conditions=(first, second))
    dropped = traces[[(node, day) in set(exclude) for node, day in zip(traces.node, traces.day)]]
    traces = traces.drop(dropped.index).reset_index(drop=True)
    excluded = pd.concat([excluded, dropped[["batch", "run_label", "condition"]].assign(
        reason="excluded node/date")], ignore_index=True)
    sources = sources.loc[sources.path.isin(set(traces.feature_file) | set(traces.metadata_file))]
    for column in ["n_prompts", "n_predict", "inter_prompt_pause_s", "model_size"]:
        if traces[column].nunique() != 1:
            raise ValueError(f"Mixed collection settings: {column}")
    if traces.groupby(["node", "day"]).batch.nunique().gt(1).any():
        raise ValueError("A node/date spans batches; review collection settings before matching")
    counts = traces.groupby(["node", "day", "condition"]).size().unstack(fill_value=0)
    counts = counts.reindex(columns=[first, second], fill_value=0)
    missing = counts.loc[(counts[first] == 0) | (counts[second] == 0)].reset_index()
    matched_keys = counts.loc[(counts[first] > 0) & (counts[second] > 0)].reset_index()
    blocks, contrasts = [], []
    for key in matched_keys.sort_values(["day", "node"]).itertuples(index=False):
        cell = traces.loc[(traces.node == key.node) & (traces.day == key.day)]
        e = cell.loc[cell.condition == first]
        n = cell.loc[cell.condition == second]
        e = e.assign(run=e.run_label.map(run_number)).sort_values("run")
        n = n.assign(run=n.run_label.map(run_number)).sort_values("run")
        block = dict(node=key.node, day=key.day, block=f"{key.day} / {key.node}",
                     batch=cell.batch.iloc[0], n_E=len(e), n_N=len(n),
                     runs_E=", ".join(e.run_label), runs_N=", ".join(n.run_label))
        blocks.append(block)
        for feature, label, scale, unit in specs:
            if scale == "log_ratio" and min(e[feature].min(), n[feature].min()) <= 0:
                raise ValueError(f"Positive run values required: {block['block']}: {feature}")
            # Every E run against every N run. Pairs with the first E run or
            # the first N run span the cell; the others follow exactly from them.
            for i, (e_label, e_value) in enumerate(zip(e.run_label, e[feature].astype(float))):
                for j, (n_label, n_value) in enumerate(zip(n.run_label, n[feature].astype(float))):
                    contrast = (np.log(e_value / n_value) if scale == "log_ratio"
                                else e_value - n_value)
                    contrasts.append(dict(**block, feature=feature, label=label, scale=scale,
                                          unit=unit, E_run=e_label, N_run=n_label,
                                          E_value=e_value, N_value=n_value, contrast=contrast,
                                          effect=float(display_effect(contrast, scale)),
                                          model_point=(i == 0 or j == 0)))
    return traces, pd.DataFrame(blocks), pd.DataFrame(contrasts), sources, excluded, missing


def block_summary(contrasts):
    """One descriptive row per node/date and feature: the mean of its cross pairs.

    On the log scale this equals mean log(E runs) - mean log(N runs).
    """
    grouped = contrasts.groupby(["block", "node", "day", "feature", "label", "scale", "unit"],
                                sort=False)
    summary = grouped.agg(n_pairs=("contrast", "size"), contrast=("contrast", "mean"),
                          E_mean=("E_value", lambda v: v.drop_duplicates().mean()),
                          N_mean=("N_value", lambda v: v.drop_duplicates().mean())).reset_index()
    summary["effect"] = [float(display_effect(c, s)) for c, s in zip(summary.contrast, summary.scale)]
    return summary


class CrossPairModel:
    """Profiled Gaussian ML for cross-pair contrasts.

    Cov(y) = s2 * (K_E + K_N + r_block K_block + r_node K_node + r_day K_day).
    K_E (K_N) links pairs sharing an E (N) run, so s2 is the run-to-run
    variance, assumed equal for E and N runs. Block, node and date terms are
    random contributions to the E-N gap. The intercept and s2 are profiled
    out; r=0 is a valid boundary estimate.
    """

    EFFECTS = ("block", "node", "day")

    def __init__(self, frame):
        self.n = len(frame)
        self.eye = np.eye(self.n)
        self.ones = np.ones(self.n)

        def same(values):
            values = np.asarray(values)
            return (values[:, None] == values[None, :]).astype(float)

        batch = frame.batch.astype(str) + "/"
        self.base = same(batch + frame.E_run) + same(batch + frame.N_run)
        self.kernels = {key: same(frame[key].astype(str)) for key in self.EFFECTS}

    def fit(self, y, initial=None, zero_mean=False):
        y = np.asarray(y, float)
        kernels = [self.kernels[key] for key in self.EFFECTS]
        rhs = np.column_stack((y, self.ones))

        def objective(ratios, details=False):
            shape = self.base + sum(r * k for r, k in zip(ratios, kernels))
            cf = cho_factor(shape, lower=True, check_finite=False)
            solved = cho_solve(cf, rhs, check_finite=False)
            mean = 0.0 if zero_mean else solved[:, 0].sum() / solved[:, 1].sum()
            alpha = solved[:, 0] - mean * solved[:, 1]
            ss = (y - mean) @ alpha
            if ss <= 0 or not np.isfinite(ss):
                raise ValueError("No positive residual variance; feature may be constant")
            nll = (2 * np.log(np.diag(cf[0])).sum() + self.n * np.log(ss / self.n)) / 2
            if details:
                variances = dict(residual=ss / self.n,
                                 **{key: r * ss / self.n for key, r in zip(self.EFFECTS, ratios)})
                return dict(nll=float(nll), mean=float(mean), variances=variances,
                            ratios=dict(zip(self.EFFECTS, ratios)), covariance=shape, alpha=alpha)
            inverse = cho_solve(cf, self.eye, check_finite=False)
            gradient = np.array([.5 * (np.sum(inverse * k) - self.n * (alpha @ k @ alpha) / ss)
                                 for k in kernels])
            return nll, gradient

        size = len(self.EFFECTS)
        starts = [np.zeros(size), np.ones(size), np.full(size, 10.0)]
        if initial is not None:
            starts.append(np.asarray(initial, float))
        candidates = []
        for start in starts:
            fitted = minimize(objective, start, jac=True, method="L-BFGS-B",
                              bounds=[(0, None)] * size,
                              options=dict(ftol=1e-11, gtol=1e-7, maxiter=300))
            # Accept boundary optima that stop at floating-point precision only
            # when the constrained first-order condition holds.
            _, gradient = objective(fitted.x)
            projected = np.where((fitted.x <= 1e-10) & (gradient > 0), 0, gradient)
            if np.isfinite(fitted.fun) and (fitted.success or np.max(np.abs(projected)) < 1e-5):
                candidates.append(fitted)
        if not candidates:
            raise RuntimeError("Variance optimization failed after multiple starting values")
        best = min(candidates, key=lambda result: result.fun)
        result = objective(best.x, details=True)
        if np.linalg.cond(result["covariance"]) > 1e12:
            raise RuntimeError("Near-singular fitted observation covariance")
        return result

    def fit_null(self, y, initial=None):
        return self.fit(y, initial=initial, zero_mean=True)

    def simulate(self, fit, rng):
        chol = np.linalg.cholesky(fit["covariance"])
        return fit["mean"] + np.sqrt(fit["variances"]["residual"]) * (chol @ rng.normal(size=self.n))


def pair_variance(variances):
    """Variance of one new cross pair: two runs plus block, node and date terms."""
    return 2 * variances["residual"] + sum(variances[key] for key in CrossPairModel.EFFECTS)


def mean_se(model, fitted):
    inverse_one = cho_solve(cho_factor(fitted["covariance"], lower=True), model.ones)
    return np.sqrt(fitted["variances"]["residual"] / inverse_one.sum())


def _analyze_feature(job):
    frame, seed, test_repeats, interval_repeats = job
    feature, label, scale, unit = frame.iloc[0][["feature", "label", "scale", "unit"]]
    block_means = frame.groupby("block").contrast.mean()
    points = frame.loc[frame.model_point].reset_index(drop=True)
    values = points.contrast.to_numpy(float)
    normalization = values.std(ddof=0)  # Do not subtract the mean: the null is zero.
    if normalization <= 0:
        raise ValueError(f"Constant contrasts require separate treatment: {feature}")
    y = values / normalization
    ratios = lambda fitted: list(fitted["ratios"].values())
    model = CrossPairModel(points)
    full = model.fit(y)
    null = model.fit_null(y, initial=ratios(full))
    if full["nll"] > null["nll"] + 1e-6:
        full = model.fit(y, initial=ratios(null))
    if full["nll"] > null["nll"] + 1e-6:
        raise RuntimeError("Observed models violate likelihood nesting")
    observed = max(0.0, 2 * (null["nll"] - full["nll"]))
    rng_null, rng_interval = [np.random.default_rng(s) for s in seed.spawn(2)]
    exceedances = 0
    for _ in range(test_repeats):
        simulated = model.simulate(null, rng_null)
        reduced = model.fit_null(simulated, initial=ratios(null))
        unrestricted = model.fit(simulated, initial=ratios(reduced))
        if unrestricted["nll"] > reduced["nll"] + 1e-6:
            raise RuntimeError("Bootstrap models violate likelihood nesting")
        statistic = max(0.0, 2 * (reduced["nll"] - unrestricted["nll"]))
        exceedances += statistic >= observed - 1e-10
    p = (exceedances + 1) / (test_repeats + 1)
    p_low = 0 if exceedances == 0 else beta.ppf(.025, exceedances, test_repeats - exceedances + 1)
    p_high = 1 if exceedances == test_repeats else beta.ppf(.975, exceedances + 1, test_repeats - exceedances)
    se = mean_se(model, full)
    t_boot, future = [], []
    bootstrap_means = []
    for _ in range(interval_repeats):
        simulated = model.simulate(full, rng_interval)
        fitted = model.fit(simulated, initial=ratios(full))
        standardized_error = (fitted["mean"] - full["mean"]) / mean_se(model, fitted)
        t_boot.append(standardized_error)
        bootstrap_means.append(fitted["mean"] * normalization)
        # Approximate predictive mixture for one cross pair on a new node AND
        # new date, under a comparable recording design.
        mean_draw = full["mean"] - standardized_error * se
        future.extend(mean_draw + np.sqrt(pair_variance(fitted["variances"])) * rng_interval.normal(size=20))
    q_low, q_high = np.quantile(t_boot, [.025, .975])
    interval = normalization * np.array([full["mean"] - q_high * se, full["mean"] - q_low * se])
    prediction = normalization * np.quantile(future, [.025, .975])
    estimate = full["mean"] * normalization
    total_variance = pair_variance(full["variances"])
    row = dict(feature=feature, label=label, scale=scale, unit=unit,
               estimate=estimate, ci_low=interval[0], ci_high=interval[1],
               prediction_low=prediction[0], prediction_high=prediction[1],
               mean_se=se * normalization, n_blocks=len(block_means), n_pairs=len(frame),
               n_model_pairs=len(points),
               n_positive=int((block_means > 0).sum()), n_negative=int((block_means < 0).sum()),
               n_zero=int((block_means == 0).sum()),
               n_pairs_positive=int((frame.contrast > 0).sum()), lrt=observed, p=p,
               p_mc_low=p_low, p_mc_high=p_high, exceedances=exceedances,
               test_repeats=test_repeats, interval_repeats=interval_repeats,
               boundary=any(full["variances"][k] < 1e-6 * total_variance for k in CrossPairModel.EFFECTS))
    for key in ("estimate", "ci_low", "ci_high", "prediction_low", "prediction_high"):
        row["display_" + key] = float(display_effect(row[key], scale))
    row["residual_fraction"] = 2 * full["variances"]["residual"] / total_variance
    for key in CrossPairModel.EFFECTS:
        row[key + "_fraction"] = full["variances"][key] / total_variance
    whitening = solve_triangular(np.linalg.cholesky(full["covariance"]),
                                 y - full["mean"], lower=True) / np.sqrt(full["variances"]["residual"])
    residuals = points[["block", "node", "day", "feature", "E_run", "N_run"]].copy()
    residuals["whitened_residual"] = whitening
    # Run-to-run part of each pair's deviation (BLUP), in units of the run SD.
    residuals["run_residual_sd"] = (model.base @ full["alpha"]) / np.sqrt(full["variances"]["residual"])
    omitted = []
    for grouping in ("day", "node"):
        groups = sorted(points[grouping].unique())
        if len(groups) < 2:  # omitting the only date/node would leave no data
            continue
        for group in groups:
            keep = (points[grouping] != group).to_numpy()
            fitted = CrossPairModel(points.loc[keep]).fit(y[keep])
            value = fitted["mean"] * normalization
            omitted.append(dict(feature=feature, label=label, scale=scale, unit=unit,
                                omitted_type=grouping, omitted=str(group),
                                n_remaining=int(points.loc[keep].block.nunique()),
                                estimate=value, display_estimate=float(display_effect(value, scale)),
                                same_sign=bool(np.sign(value) == np.sign(estimate))))
    return row, omitted, residuals, bootstrap_means


def analyze(contrasts, test_repeats=19999, interval_repeats=1999, seed=42, workers=4):
    seeds = np.random.SeedSequence(seed).spawn(len(SPECS))
    # Keep each feature's random stream unchanged when the test family is reduced.
    jobs = [(contrasts.loc[contrasts.feature == feature].reset_index(drop=True),
             seeds[i], test_repeats, interval_repeats) for i, (feature, *_rest) in enumerate(SPECS)
            if feature in set(contrasts.feature)]
    if not jobs or not set(contrasts.feature).issubset({s[0] for s in SPECS}):
        raise ValueError("Contrasts must contain known features")
    estimates, sensitivity, residuals, bootstraps = [], [], [], []
    executor = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        iterator = executor.map(_analyze_feature, jobs) if executor else map(_analyze_feature, jobs)
        for i, (row, omitted, residual, means) in enumerate(iterator, 1):
            estimates.append(row)
            sensitivity.extend(omitted)
            residuals.append(residual)
            bootstraps.extend(dict(feature=row["feature"], replicate=j, mean=value)
                              for j, value in enumerate(means))
            print(f"Completed {i}/{len(jobs)}: {row['label']}", flush=True)
    finally:
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)
    estimates = pd.DataFrame(estimates)
    estimates["p_bonferroni"] = multipletests(estimates.p, method="bonferroni")[1]
    for bound in ("low", "high"):
        estimates["p_bonferroni_mc_" + bound] = multipletests(estimates["p_mc_" + bound], method="bonferroni")[1]
    estimates["supported"] = estimates.p_bonferroni < .05
    estimates["mc_sensitive"] = (estimates.p_bonferroni_mc_low < .05) & (estimates.p_bonferroni_mc_high >= .05)
    estimates["prediction_crosses_zero"] = (estimates.prediction_low <= 0) & (estimates.prediction_high >= 0)
    return estimates, pd.DataFrame(sensitivity), pd.concat(residuals, ignore_index=True), pd.DataFrame(bootstraps)
