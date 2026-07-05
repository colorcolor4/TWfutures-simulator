#!/usr/bin/env python3
"""保證金引擎（T03/T04）：從 TWfutures-simulator(index.html) 移植的口數級機制。

職責（L3 執行層）：曝險倍數→口數、逐日用 low 檢查維持保證金（追繳/強平）、
手續費+滑價+轉倉成本。不含策略邏輯（策略歸 期貨低接回測.py）。
規格數字以 TWfutures-simulator index.html 為準（2026-07 快照）。
"""
import datetime as dt

SPECS = {                                # mult=每點價值 im=原始保證金 mm=維持保證金
    "大台": dict(mult=200, im=184000, mm=141000),
    "小台": dict(mult=50,  im=46000,  mm=35250),
    "微台": dict(mult=10,  im=9200,   mm=7050),
}
FEE = {"大台": 100, "小台": 60, "微台": 25}   # 每口每邊 手續費+期交稅 概估(NT$)
SLIP_PTS = 1.0                                # 每邊滑價(點)

def to_contracts(equity, price, target_x):
    """目標曝險→口數(貪婪:小台優先、微台補零頭、餘數捨去=寧少勿多)。回傳 dict 口數。"""
    notional = max(equity * target_x, 0.0)
    n50 = int(notional // (price * SPECS["小台"]["mult"]))
    rem = notional - n50 * price * SPECS["小台"]["mult"]
    n10 = int(rem // (price * SPECS["微台"]["mult"]))
    return {"小台": n50, "微台": n10}

def point_value(contracts):
    return sum(SPECS[k]["mult"] * n for k, n in contracts.items())

def margin_used(contracts):
    return sum(SPECS[k]["im"] * n for k, n in contracts.items())

def maintenance(contracts):
    return sum(SPECS[k]["mm"] * n for k, n in contracts.items())

def fit_to_margin(equity, price, target_x):
    """開倉前檢查原始保證金:不夠就逐口縮(先砍小台)直到 margin_used ≤ equity。"""
    c = to_contracts(equity, price, target_x)
    while margin_used(c) > equity and (c["小台"] or c["微台"]):
        if c["小台"]: c["小台"] -= 1
        else: c["微台"] -= 1
    return c

def trade_cost(delta, price):
    """調倉成本:每口每邊 手續費 + 滑價1點×每點價值。delta=dict 口數變化量(絕對值)。"""
    cost = 0.0
    for k, n in delta.items():
        n = abs(n)
        cost += n * (FEE[k] + SLIP_PTS * SPECS[k]["mult"])
    return cost

def is_rollover(datestr):
    """每月第三個週三=結算,全部位平掉重建(T04)。"""
    d = dt.date.fromisoformat(datestr)
    return d.weekday() == 2 and 15 <= d.day <= 21

def daily_low_check(equity_prev_close, contracts, low, prev_close):
    """用當日 low 算最壞權益。回傳 (最壞權益, 是否強平, 是否追繳警告)。
    強平=最壞權益≤總維持保證金(以low成交出場)；警告=權益/原始保證金<110%。"""
    if not any(contracts.values()):
        return equity_prev_close, False, False
    eq_low = equity_prev_close + point_value(contracts) * (low - prev_close)
    mm = maintenance(contracts)
    forced = eq_low <= mm
    im = margin_used(contracts)
    warn = (not forced) and im > 0 and eq_low / im * 100 < 110
    return eq_low, forced, warn

if __name__ == "__main__":   # 自測(T03 驗收)
    # 500萬@47000 目標2倍 → 實際倍數落在 1.9~2.0
    c = to_contracts(5_000_000, 47000, 2.0)
    x = point_value(c) * 47000 / 5_000_000
    assert 1.9 <= x <= 2.0, (c, x)
    # 權益10萬持1口小台,low 大跌700點 → 最壞權益 100000-35000=65000 > 35250 不強平
    eq, forced, _ = daily_low_check(100_000, {"小台": 1, "微台": 0}, 17300, 18000)
    assert not forced and abs(eq - 65000) < 1
    # 再跌:low 較前收跌1400點 → 30000 ≤ 35250 強平
    eq, forced, _ = daily_low_check(100_000, {"小台": 1, "微台": 0}, 16600, 18000)
    assert forced and abs(eq - 30000) < 1
    # 保證金不夠會縮口數
    c2 = fit_to_margin(50_000, 47000, 2.0)
    assert margin_used(c2) <= 50_000
    # 轉倉日判定:2026-07-15 是第三個週三
    assert is_rollover("2026-07-15") and not is_rollover("2026-07-08")
    print("✓ 保證金引擎自測全過:", c, f"實際倍數{x:.3f}")
