#!/usr/bin/env python3
"""口數回測（T05/T06）：L1 OHLC ＋ L2 曝險序列 → L3 保證金引擎 → 口數級真實績效。

用法:
  python3 口數回測.py 曝險序列.csv [--capital 1000000] [--html 報告.html]

與倍數模型的差異來源: 口數整數化(捨去)、保證金追繳/強平(用當日low)、手續費+滑價、月轉倉。
口數版必然比倍數版差; 若更好=有bug。
"""
import csv, os, sys, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))

def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def main():
    args = sys.argv[1:]
    series_path = args[0]
    cap0 = float(args[args.index("--capital") + 1]) if "--capital" in args else 1_000_000
    html = args[args.index("--html") + 1] if "--html" in args else None

    eng = _load("保證金引擎")
    ohlc = {d: (o, h, l, c) for d, o, h, l, c in _load("期貨低接回測").load_ohlc()}
    tgt = [(r["date"], float(r["target_exposure"]))
           for r in csv.DictReader(open(series_path, encoding="utf-8-sig")) if r["date"] in ohlc]

    equity, contracts, prev_close = cap0, {"小台": 0, "微台": 0}, None
    prev_x = None
    rows, events = [], []
    eq_peak, maxdd = cap0, 0.0
    yearly, y0, eq_y = {}, tgt[0][0][:4], cap0

    for d, x in tgt:
        o, h, l, c = ohlc[d]
        if prev_close is None:
            prev_close = c
        ev = ""
        # ① 盤中 low 檢查(先於收盤結算): 強平以 low 成交、出場成本照收
        eq_low, forced, warn = eng.daily_low_check(equity, contracts, l, prev_close)
        if forced:
            equity = eq_low - eng.trade_cost(contracts, l)
            contracts = {"小台": 0, "微台": 0}
            ev = "💀強平"
            events.append((d, "強平", f"權益{equity:,.0f}"))
        elif warn:
            ev = "⚠追繳警告"
            events.append((d, "追繳警告", f"low權益{eq_low:,.0f}"))
        # ② 收盤 mark-to-market
        if not forced:
            equity += eng.point_value(contracts) * (c - prev_close)
        if equity <= 0:
            events.append((d, "畢業", "權益歸零"))
            rows.append([d, contracts["小台"], contracts["微台"], 0, 0, round(equity), ev or "畢業"])
            break
        # ③ 月結算轉倉: 全平重建,平/建各收一次成本
        if eng.is_rollover(d) and any(contracts.values()):
            equity -= 2 * eng.trade_cost(contracts, c)
            ev = ev or "轉倉"
        # ④ 收盤調倉: 只在策略目標改變時動作(避免天天微調)
        if x != prev_x:
            desired = eng.fit_to_margin(equity, c, x)
            delta = {k: desired[k] - contracts[k] for k in desired}
            if any(delta.values()):
                equity -= eng.trade_cost(delta, c)
                contracts = desired
            prev_x = x
        mg = eng.margin_used(contracts)
        rows.append([d, contracts["小台"], contracts["微台"], round(mg),
                     round(equity / mg * 100, 1) if mg else "", round(equity), ev])
        eq_peak = max(eq_peak, equity)
        maxdd = min(maxdd, equity / eq_peak - 1)
        y = d[:4]
        if y != y0:
            yearly[y0] = equity  # 存年末權益,後面換算
            y0 = y
        prev_close = c
    yearly[y0] = equity

    out = os.path.join(HERE, "..", "全職交易", "口數回測結果.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "小台口數", "微台口數", "保證金占用", "保證金比率%", "權益", "事件"])
        w.writerows(rows)

    # 年度報酬換算
    ys = sorted(yearly)
    yr, prev_eq = {}, cap0
    for y in ys:
        yr[y] = yearly[y] / prev_eq - 1
        prev_eq = yearly[y]

    n_forced = sum(1 for e in events if e[1] == "強平")
    n_warn = sum(1 for e in events if e[1] == "追繳警告")
    print(f"■ 口數回測: {series_path}  本金 {cap0:,.0f}")
    print(f"  總報酬 {(equity/cap0-1)*100:+.0f}%（終值 {equity:,.0f}）｜最大回撤 {maxdd*100:.0f}%")
    print(f"  強平 {n_forced} 次｜追繳警告 {n_warn} 次｜資料 {len(rows)} 日 → {out}")
    print("  年度: " + "  ".join(f"{y}:{v*100:+.0f}%" for y, v in yr.items()))
    if events[:8]:
        print("  事件(前8): " + "; ".join(f"{d} {t} {m}" for d, t, m in events[:8]))

    if html:
        _report(html, series_path, cap0, rows, yr, events, equity, maxdd)

def _report(path, src, cap0, rows, yr, events, equity, maxdd):
    """T06: 單檔 HTML 報告(equity 曲線 SVG + 年度表 + 事件清單)。"""
    eqs = [r[5] for r in rows]
    n = len(eqs); mx, mn = max(eqs), min(eqs)
    W, H = 720, 240
    pts = " ".join(f"{i/(n-1)*W:.1f},{H - (e-mn)/(mx-mn or 1)*(H-20) - 10:.1f}" for i, e in enumerate(eqs))
    yr_rows = "".join(f"<tr><td>{y}</td><td style='color:{'#c33' if v<0 else '#2a7'}'>{v*100:+.0f}%</td></tr>"
                      for y, v in yr.items())
    ev_rows = "".join(f"<tr><td>{d}</td><td>{t}</td><td>{m}</td></tr>" for d, t, m in events) or \
              "<tr><td colspan=3>無強平/追繳事件</td></tr>"
    doc = f"""<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>口數回測報告</title>
<style>body{{font:15px/1.6 -apple-system,'PingFang TC',sans-serif;max-width:760px;margin:1em auto;padding:0 12px}}
table{{border-collapse:collapse;width:100%;margin:.6em 0}}td,th{{border:1px solid #ccc;padding:4px 10px;text-align:left}}
svg{{width:100%;height:auto;border:1px solid #ddd;border-radius:6px}}
h1{{font-size:1.3em}}.kpi{{display:flex;gap:1em;flex-wrap:wrap}}.kpi div{{background:#f5f5f7;border-radius:8px;padding:8px 14px}}</style>
<h1>口數回測報告</h1>
<p>來源序列：{os.path.basename(src)}｜本金 {cap0:,.0f}｜含口數整數化/保證金low檢查/手續費滑價/月轉倉</p>
<div class=kpi><div>終值<br><b>{equity:,.0f}</b></div><div>總報酬<br><b>{(equity/cap0-1)*100:+.0f}%</b></div>
<div>最大回撤<br><b>{maxdd*100:.0f}%</b></div><div>強平<br><b>{sum(1 for e in events if e[1]=='強平')} 次</b></div></div>
<h2>權益曲線</h2><svg viewBox="0 0 {W} {H}"><polyline points="{pts}" fill="none" stroke="#2a6" stroke-width="1.5"/></svg>
<h2>年度報酬</h2><table><tr><th>年</th><th>報酬</th></tr>{yr_rows}</table>
<h2>追繳/強平事件</h2><table><tr><th>日期</th><th>事件</th><th>備註</th></tr>{ev_rows}</table>
<p style="color:#888">現貨日線近似：夜盤跳空/價差未計。生成於口數回測.py</p>"""
    open(path, "w", encoding="utf-8").write(doc)
    print(f"  ✓ HTML 報告 → {path}")

if __name__ == "__main__":
    main()
