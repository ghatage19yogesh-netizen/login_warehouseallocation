"""Simplified COM allocation engine (demo).

Mirrors the structure of the production pipeline:
  1. sort products (ok / low velocity / no demand)
  2. place special products by rules and reduce capacity
  3. solve a min-cost LP for the rest (scipy HiGHS instead of Gurobi)
  4. combine and summarise

All data here is SAMPLE data. Nothing comes from a real system.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

WAREHOUSES = ["A-East", "B-West", "C-Central"]
ZONES = ["Northeast", "Southeast", "West", "Midwest"]
STORAGE_TYPES = ["FLA", "HFG"]


# --------------------------------------------------------------------------- #
# Sample data
# --------------------------------------------------------------------------- #
def sample_costs() -> pd.DataFrame:
    """Shipping cost per unit by warehouse x zone. One lane is missing on purpose."""
    data = {
        "A-East": [2.0, 3.0, 6.0, 4.0],
        "B-West": [6.0, 5.0, 2.0, 4.0],
        "C-Central": [np.nan, 3.5, 4.0, 2.0],  # missing lane -> data-quality warning
    }
    return pd.DataFrame(data, index=ZONES).T.rename_axis("warehouse")


def sample_products() -> pd.DataFrame:
    rows = [
        # product_type, storage, velocity, NE, SE, W, MW, order_qty
        ("101-2001-high", "FLA", "high", 600, 300, 400, 350, 0),
        ("101-2002-high", "FLA", "high", 300, 250, 500, 200, 0),
        ("205-3001-medium", "HFG", "medium", 200, 150, 300, 180, 0),
        ("205-3002-high", "HFG", "high", 250, 200, 150, 220, 0),
        ("310-4001-low", "FLA", "low", 20, 15, 25, 10, 0),
        ("310-4002-low", "HFG", "low", 12, 8, 15, 10, 0),
        ("420-5001-high", "FLA", "high", 0, 0, 0, 0, 90),
        ("512-6001-medium", "FLA", "medium", 150, 120, 180, 160, 0),
    ]
    cols = ["product_type", "storage", "velocity"] + ZONES + ["order_qty"]
    return pd.DataFrame(rows, columns=cols)


def sample_capacity() -> pd.DataFrame:
    return pd.DataFrame(
        {"FLA": [2200, 1800, 1500], "HFG": [700, 650, 500]}, index=WAREHOUSES
    ).rename_axis("warehouse")


def sample_limits() -> pd.DataFrame:
    return pd.DataFrame(
        {"min_pct": [20.0, 20.0, 20.0], "max_pct": [50.0, 50.0, 50.0]}, index=WAREHOUSES
    ).rename_axis("warehouse")


def sample_restrictions() -> pd.DataFrame:
    """Business rule: product (dept-vendor) is forbidden in a warehouse."""
    return pd.DataFrame([{"product": "205-3002", "warehouse": "C-Central", "rule": "Forbidden"}])


def strip_velocity(product_type: str) -> str:
    return "-".join(product_type.split("-")[:-1])


# --------------------------------------------------------------------------- #
# Pre-run checks
# --------------------------------------------------------------------------- #
def pre_run_checks(products, costs, capacity, limits) -> list[dict]:
    """Return a list of {level: error|warning|info, message}."""
    out = []
    bad = limits[limits["min_pct"] > limits["max_pct"]]
    for wh in bad.index:
        out.append({"level": "error", "message": f"{wh}: min share is larger than max share"})
    if limits["max_pct"].sum() < 100:
        out.append({"level": "error", "message": "Max shares add up to less than 100% — demand can't be met"})
    if limits["min_pct"].sum() > 100:
        out.append({"level": "error", "message": "Min shares add up to more than 100% — would force extra stock"})

    n_missing = int(costs.isna().sum().sum())
    if n_missing:
        out.append({"level": "warning",
                    "message": f"{n_missing} shipping-cost lane(s) missing — filled with the warehouse average"})

    demand_by_storage = products.assign(total=products[ZONES].sum(axis=1) + products["order_qty"]) \
        .groupby("storage")["total"].sum()
    for k, dem in demand_by_storage.items():
        cap = capacity[k].sum()
        if dem > cap:
            out.append({"level": "warning",
                        "message": f"{k}: demand {dem:,.0f} > total space {cap:,.0f} — overflow will be penalised"})

    nd = products[(products[ZONES].sum(axis=1) == 0)]
    if len(nd):
        out.append({"level": "info", "message": f"{len(nd)} product(s) with no demand — split evenly"})
    if not any(c["level"] != "info" for c in out):
        out.insert(0, {"level": "ok", "message": "Share limits are valid and data is complete"})
    return out


def has_errors(checks) -> bool:
    return any(c["level"] == "error" for c in checks)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def _fill_costs(costs: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    filled = costs.copy()
    missing = [(wh, z) for wh in costs.index for z in costs.columns if pd.isna(costs.loc[wh, z])]
    for wh, z in missing:
        filled.loc[wh, z] = costs.loc[wh].mean(skipna=True)
    return filled, missing


def run_allocation(products, costs, capacity, limits, restrictions,
                   penalty: float = 50.0, hfg_cost_factor: float = 1.2,
                   time_limit: float = 60.0) -> dict:
    products = products.copy()
    capacity = capacity.astype(float).copy()
    costs, missing_lanes = _fill_costs(costs)

    def unit_cost(wh, zone, storage):
        return float(costs.loc[wh, zone]) * (hfg_cost_factor if storage == "HFG" else 1.0)

    forbidden = {(r["product"], r["warehouse"]) for _, r in restrictions.iterrows()
                 if r.get("rule", "Forbidden") == "Forbidden"}

    def allowed(pt):
        return [w for w in WAREHOUSES if (strip_velocity(pt), w) not in forbidden]

    # ---- 1. sort products --------------------------------------------------
    products["total_demand"] = products[ZONES].sum(axis=1)
    no_demand = products[products["total_demand"] == 0]
    low = products[(products["velocity"] == "low") & (products["total_demand"] > 0)]
    ok = products[(products["velocity"] != "low") & (products["total_demand"] > 0)]

    rows, warnings = [], []

    # ---- 2. rule-based placements -----------------------------------------
    for _, p in low.iterrows():
        cands = allowed(p.product_type)
        # deterministic: cheapest warehouse for this product's demand (production picks randomly)
        best = min(cands, key=lambda w: sum(p[z] * unit_cost(w, z, p.storage) for z in ZONES))
        for z in ZONES:
            if p[z] > 0:
                c = unit_cost(best, z, p.storage)
                rows.append(dict(warehouse=best, zone=z, product_type=p.product_type, storage=p.storage,
                                 units=float(p[z]), unit_cost=c, method="Rule: slow seller → 1 warehouse"))
        capacity.loc[best, p.storage] -= p.total_demand

    for _, p in no_demand.iterrows():
        cands = allowed(p.product_type)
        share = p.order_qty / len(cands)
        for w in cands:
            rows.append(dict(warehouse=w, zone="—", product_type=p.product_type, storage=p.storage,
                             units=float(share), unit_cost=np.nan, method="Rule: no demand → even split"))
            capacity.loc[w, p.storage] -= share

    # medium velocity -> limited to the 2 cheapest allowed warehouses
    medium_allowed = {}
    for _, p in ok[ok["velocity"] == "medium"].iterrows():
        cands = allowed(p.product_type)
        ranked = sorted(cands, key=lambda w: sum(p[z] * unit_cost(w, z, p.storage) for z in ZONES))
        medium_allowed[p.product_type] = ranked[:2]

    # ---- 3. LP ------------------------------------------------------------
    I, J, P, K = len(WAREHOUSES), len(ZONES), len(ok), len(STORAGE_TYPES)
    ok = ok.reset_index(drop=True)
    nx = I * J * P
    no = I * K

    def xi(i, j, p):
        return (i * J + j) * P + p

    def oi(i, k):
        return nx + i * K + k

    c = np.zeros(nx + no)
    bounds = [(0, None)] * (nx + no)
    for i, w in enumerate(WAREHOUSES):
        for p, prod in ok.iterrows():
            blocked = w not in allowed(prod.product_type) or (
                prod.product_type in medium_allowed and w not in medium_allowed[prod.product_type])
            for j, z in enumerate(ZONES):
                c[xi(i, j, p)] = unit_cost(w, z, prod.storage)
                if blocked:
                    bounds[xi(i, j, p)] = (0, 0)
    for i in range(I):
        for k in range(K):
            c[oi(i, k)] = penalty

    total_lp = float(ok[ZONES].to_numpy().sum())
    n_rows = J * P + I * K + 2 * I
    A = lil_matrix((n_rows, nx + no))
    b = np.zeros(n_rows)
    r = 0
    # demand: sum_i x >= d   ->  -sum_i x <= -d
    for j, z in enumerate(ZONES):
        for p, prod in ok.iterrows():
            for i in range(I):
                A[r, xi(i, j, p)] = -1
            b[r] = -prod[z]
            r += 1
    # capacity (soft): sum x - overflow <= cap
    for i, w in enumerate(WAREHOUSES):
        for k, s in enumerate(STORAGE_TYPES):
            for p, prod in ok.iterrows():
                if prod.storage == s:
                    for j in range(J):
                        A[r, xi(i, j, p)] = 1
            A[r, oi(i, k)] = -1
            b[r] = max(capacity.loc[w, s], 0.0)
            r += 1
    # share limits
    for i, w in enumerate(WAREHOUSES):
        max_u = limits.loc[w, "max_pct"] / 100 * total_lp
        min_u = min(limits.loc[w, "min_pct"] / 100 * total_lp, 0.95 * max(capacity.loc[w].sum(), 0))
        for j in range(J):
            for p in range(P):
                A[r, xi(i, j, p)] = 1
                A[r + 1, xi(i, j, p)] = -1
        b[r], b[r + 1] = max_u, -min_u
        r += 2

    res = linprog(c, A_ub=A.tocsr(), b_ub=b, bounds=bounds, method="highs",
                  options={"time_limit": float(time_limit)})

    status_map = {0: "Optimal", 1: "Stopped at time limit", 2: "Infeasible", 3: "Unbounded", 4: "Solver error"}
    status = status_map.get(res.status, "Solver error")
    overflow = pd.DataFrame(0.0, index=WAREHOUSES, columns=STORAGE_TYPES)

    if res.x is not None and res.status in (0, 1):
        x = res.x
        for i, w in enumerate(WAREHOUSES):
            for k, s in enumerate(STORAGE_TYPES):
                overflow.loc[w, s] = round(float(x[oi(i, k)]), 2)
            for p, prod in ok.iterrows():
                for j, z in enumerate(ZONES):
                    v = float(x[xi(i, j, p)])
                    if v > 1e-6:
                        rows.append(dict(warehouse=w, zone=z, product_type=prod.product_type,
                                         storage=prod.storage, units=round(v, 2),
                                         unit_cost=unit_cost(w, z, prod.storage), method="Optimizer"))
    else:
        warnings.append({"level": "error", "message": f"Optimizer result: {status}. {res.message}"})

    alloc = pd.DataFrame(rows, columns=["warehouse", "zone", "product_type", "storage",
                                        "units", "unit_cost", "method"])
    alloc["cost"] = (alloc["units"] * alloc["unit_cost"]).fillna(0.0)

    # ---- 4. summary & warnings -------------------------------------------
    summary = summarise(alloc, overflow)

    for w in WAREHOUSES:
        for s in STORAGE_TYPES:
            if overflow.loc[w, s] > 0.5:
                warnings.append({"level": "high",
                                 "message": f"{w} {s} is over its space limit by {overflow.loc[w, s]:,.0f} units"})
    for w, z in missing_lanes:
        warnings.append({"level": "high",
                         "message": f"Shipping cost {w} → {z} was missing; filled with warehouse average"})
    n_low = len(low)
    if n_low:
        warnings.append({"level": "info", "message": f"{n_low} slow seller(s) placed in one warehouse by rule"})
    if len(no_demand):
        warnings.append({"level": "info", "message": f"{len(no_demand)} product(s) with no demand split evenly"})
    for pt, whs in medium_allowed.items():
        warnings.append({"level": "info", "message": f"{pt} (medium) limited to {', '.join(whs)}"})
    if restrictions is not None and len(restrictions):
        warnings.append({"level": "info", "message": f"{len(restrictions)} business rule(s) applied"})

    return {"status": status, "objective": float(res.fun) if res.fun is not None else None,
            "allocation": alloc, "overflow": overflow, "summary": summary, "warnings": warnings}


def summarise(alloc: pd.DataFrame, overflow: pd.DataFrame) -> dict:
    total_units = float(alloc["units"].sum())
    by_wh = alloc.groupby("warehouse").agg(units=("units", "sum"), cost=("cost", "sum")) \
        .reindex(WAREHOUSES, fill_value=0.0)
    by_wh["share_pct"] = by_wh["units"] / total_units * 100 if total_units else 0.0
    by_method = alloc.groupby(alloc["method"].str.startswith("Rule"))["units"].sum()
    return {
        "total_cost": float(alloc["cost"].sum()),
        "total_units": total_units,
        "optimizer_units": float(by_method.get(False, 0.0)),
        "rule_units": float(by_method.get(True, 0.0)),
        "overflow_units": float(overflow.to_numpy().sum()),
        "by_warehouse": by_wh,
    }


def utilisation(alloc: pd.DataFrame, capacity: pd.DataFrame) -> pd.DataFrame:
    used = alloc.groupby(["warehouse", "storage"])["units"].sum().unstack(fill_value=0.0) \
        .reindex(index=WAREHOUSES, columns=STORAGE_TYPES, fill_value=0.0)
    rows = []
    for w in WAREHOUSES:
        for s in STORAGE_TYPES:
            cap = float(capacity.loc[w, s])
            u = float(used.loc[w, s])
            rows.append({"Warehouse": w, "Storage": s, "Allocated": round(u),
                         "Space": round(cap), "Space used %": round(u / cap * 100, 1) if cap else None})
    return pd.DataFrame(rows)
