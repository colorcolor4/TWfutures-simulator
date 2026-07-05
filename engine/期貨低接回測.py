#!/usr/bin/env python3
"""期貨低接策略回測：TAIEX 日線模擬「分批加碼低接」的權益曲線。

用法:
  .venv/bin/python 工具/期貨低接回測.py                # 內建情境比較(現行馬丁 vs 建議v1等)
  .venv/bin/python 工具/期貨低接回測.py --json '{"name":"自訂","base":0.5,"tranches":[[6,0.5],[10,0.5],[15,0.75],[20,0.75]],"cap":2.5,"bear_filter":true,"eq_stop":20}'

模型(簡化但方向保守):
  - 曝險 = base + 各批加碼。指數從滾動高點每回落 tranches[i][0]% 觸發第 i 批,加 tranches[i][1] 倍曝險。
  - 點數模式: tranches_pts=[[1000,1.0],...] 用絕對點數觸發(模擬使用者現行「每跌千點加一倍」)。
  - cap = 總曝險上限。bear_filter: 指數<240MA 且 240MA 下彎 → 該批加碼減半(空頭濾網)。
  - eq_stop: 權益從高點回落 X% → 全平歸 base,指數創 60 日高才重啟加碼(認錯冷卻)。
  - 出場: 指數創滾動高點新高 → 加碼部位全平(獲利落袋),曝險回 base。
  - 每日收盤 mark-to-market: eq *= (1 + exposure*ret)。忽略保證金追繳/轉倉成本(實際只會更差)。
資料: Yahoo ^TWII,快取 全職交易/TWII_日線.csv(已存在就不重抓,防 IP 限流)。
"""
import csv, json, os, sys, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "..", "全職交易", "TWII_日線.csv")
OHLC = os.path.join(HERE, "..", "全職交易", "TWII_日線OHLC.csv")

def _stock():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stock", os.path.join(HERE, "stock.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def load_ohlc():
    """T01: OHLC 日線快取(口數回測用 low 檢查保證金)。欄位 date,open,high,low,close。"""
    if not os.path.exists(OHLC):
        d = _stock()._get("https://query2.finance.yahoo.com/v8/finance/chart/%5ETWII?range=10y&interval=1d")["chart"]["result"][0]
        q = d["indicators"]["quote"][0]
        with open(OHLC, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["date", "open", "high", "low", "close"])
            for t, o, h, l, c in zip(d["timestamp"], q["open"], q["high"], q["low"], q["close"]):
                if None in (o, h, l, c):
                    continue                       # STOP: 缺值列跳過,不補值
                w.writerow([dt.date.fromtimestamp(t).isoformat(),
                            round(o, 2), round(h, 2), round(l, 2), round(c, 2)])
    rows = list(csv.DictReader(open(OHLC, encoding="utf-8-sig")))
    return [(r["date"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for r in rows if float(r["close"]) > 0]

def load_index():
    if not os.path.exists(CACHE):
        import importlib.util
        spec = importlib.util.spec_from_file_location("stock", os.path.join(HERE, "stock.py"))
        stock = importlib.util.module_from_spec(spec); spec.loader.exec_module(stock)
        _, rows = stock.chart("^TWII", "10y")          # 複用 stock.py 的 Yahoo chart API(免cookie)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f); w.writerow(["date", "close"])
            for r in rows:
                w.writerow([r[0], round(float(r[1]), 2)])
    rows = list(csv.DictReader(open(CACHE, encoding="utf-8-sig")))
    return [(r["date"], float(r["close"])) for r in rows if float(r["close"]) > 0]

def sma(vals, n, i):
    return sum(vals[i-n+1:i+1]) / n if i >= n-1 else None

def run(data, cfg, start="2016-01-01"):
    dates = [d for d, _ in data]; closes = [c for _, c in data]
    i0 = next(i for i, d in enumerate(dates) if d >= start)
    eq, eq_peak, idx_peak = 1.0, 1.0, closes[i0]
    exposure, fired, frozen, armed, touched = cfg.get("base", 0.5), set(), False, False, set()
    run_low, probing, ref_low, breach = None, False, None, 0   # probe 探底模式狀態
    yearly, y0, eq_y = {}, dates[i0][:4], 1.0
    maxdd, stops = 0.0, 0
    series = []                                    # T02: (date, 當日收盤後目標曝險)
    for i in range(i0+1, len(closes)):
        series.append((dates[i-1], exposure))      # 迴圈頂記前一日收盤後最終曝險(躲過所有continue)
        ret = closes[i]/closes[i-1] - 1
        eq *= (1 + exposure*ret)
        if eq <= 0:   # 畢業
            return dict(name=cfg["name"], ruin=dates[i], total="-100%", maxdd="-100%", stops=stops, yearly=yearly)
        eq_peak = max(eq_peak, eq)
        maxdd = min(maxdd, eq/eq_peak - 1)
        y = dates[i][:4]
        if y != y0:
            yearly[y0] = eq/eq_y - 1; y0, eq_y = y, eq
        # 權益停損。requick=true: 不進60日高冷卻,改「重置基準點」→之後從停損日再跌-6%...才重新分批(配vol_gate)
        if cfg.get("eq_stop") and eq/eq_peak - 1 <= -cfg["eq_stop"]/100 and exposure > cfg.get("base", 0.5):
            exposure, fired, touched, stops = cfg.get("base", 0.5), set(), set(), stops+1
            if cfg.get("requick"): idx_peak = closes[i]
            else: frozen = True
        # 指數新高 → 出場邏輯二選一:
        #   預設: 立刻獲利了結加碼部位
        #   exit_ma=N: 只「武裝」(armed),續抱到收盤跌破 N 日線才了結(吃魚身到均線折返)
        if closes[i] >= idx_peak:
            idx_peak, frozen = closes[i], False
            run_low, probing, ref_low, breach = None, False, None, 0
            if any(cfg.get(k) for k in ("exit_ma", "exit_cross", "exit_daydrop", "exit_flat")):
                if exposure > cfg.get("base", 0.5): armed = True
            else:
                exposure, fired, touched = cfg.get("base", 0.5), set(), set()
            continue
        if armed:
            hit = False
            if cfg.get("exit_ma"):
                ma = sma(closes, cfg["exit_ma"], i)
                hit = ma and closes[i] < ma
            if cfg.get("exit_cross"):          # [快,慢]: 快線下穿慢線才出(例 [5,10])
                f_, s_ = cfg["exit_cross"]
                fa, sl = sma(closes, f_, i), sma(closes, s_, i)
                hit = hit or (fa and sl and fa < sl)
            if cfg.get("exit_daydrop"):        # 單日跌幅 ≥ x% 出場(獲利保護)
                hit = hit or ret <= -cfg["exit_daydrop"]/100
            if cfg.get("exit_flat"):           # 均線走平=橫盤出場: N日線 5 天斜率 < 0.1%
                n = cfg["exit_flat"]
                m0, m5 = sma(closes, n, i), sma(closes, n, i-5)
                hit = hit or (m0 and m5 and abs(m0/m5 - 1) < 0.001)
            if hit:
                exposure, fired, touched, armed = cfg.get("base", 0.5), set(), set(), False
        if frozen:   # 冷卻中:創60日高才解凍
            hi60 = max(closes[max(0, i-59):i+1])
            if closes[i] >= hi60: frozen = False
            else: continue
        # probe 探底模式: 破「買入時的前低」連續 N 天沒站回 → 砍回 base,彈回可再試
        if cfg.get("probe"):
            run_low = closes[i] if run_low is None else min(run_low, closes[i])
            ddp0 = (idx_peak - closes[i]) / idx_peak * 100
            # hybrid: 分批買上來的既有部位一進 -20% 深水區,立刻掛上前低停損(不需等vol條件)
            if not probing and ddp0 >= cfg.get("probe_start", 20) and exposure > cfg.get("base", 0.5):
                probing, ref_low, breach = True, run_low, 0
            if probing:
                breach = breach + 1 if closes[i] < ref_low else 0
                if breach >= cfg.get("probe_reclaim", 3):
                    exposure, probing, breach = cfg.get("base", 0.5), False, 0
                    fired, touched = set(), set()      # 停損後分批檻位重置,由probe重試接管
        # 波動率閘門: 20日年化波動 < vol_gate% → 恐慌未到,不准加碼(抓capitulation的代理指標)
        if cfg.get("vol_gate") and i >= 20:
            rets = [closes[j]/closes[j-1]-1 for j in range(i-19, i+1)]
            mu = sum(rets)/20
            vol = (sum((r-mu)**2 for r in rets)/20)**0.5 * (240**0.5) * 100
            if vol < cfg["vol_gate"]:
                continue
        # 空頭濾網
        half = 1.0
        if cfg.get("bear_filter"):
            m = sma(closes, 240, i); m_prev = sma(closes, 240, i-20)
            if m and m_prev and closes[i] < m and m < m_prev: half = 0.5
        # probe 買入: 跌逾 probe_start% + vol_gate已過(恐慌) + 價在最低點之上(有彈) → 一次上滿 cap
        if cfg.get("probe"):
            ddp = (idx_peak - closes[i]) / idx_peak * 100
            if not probing and ddp >= cfg.get("probe_start", 20) and closes[i] > run_low:
                exposure, probing, ref_low, breach = cfg.get("cap", 2.0), True, run_low, 0
            if not cfg.get("hybrid") or ddp >= cfg.get("probe_start", 20):
                continue                               # 純probe不走批次;hybrid深水區由probe接管
        # hybrid 淺水區(<probe_start)走下方分批邏輯
        # 觸發加碼批次。confirm_ma=N: 右側確認——檻位到了先掛起,等收盤站回N日線才真正成交
        dd_pct = (idx_peak - closes[i]) / idx_peak * 100
        dd_pts = idx_peak - closes[i]
        cm = cfg.get("confirm_ma")
        ok = True
        if cm:
            m = sma(closes, cm, i)
            ok = m is not None and closes[i] > m
        for k, (trig, add) in enumerate(cfg.get("tranches", [])):
            key = ("pct", k)
            if key in fired: continue
            if dd_pct >= trig: touched.add(key)      # 檻位碰過就記住(等右側確認補成交)
            if key in touched and ok:
                exposure = min(exposure + add*half, cfg.get("cap", 99)); fired.add(key)
        for k, (trig, add) in enumerate(cfg.get("tranches_pts", [])):
            key = ("pts", k)
            if key not in fired and dd_pts >= trig*(k+1):
                exposure = min(exposure + add*half, cfg.get("cap", 99)); fired.add(key)
                # 點數模式:每滿 N*1000 點加一批(馬丁),批數無上限→靠 cap 擋
        if cfg.get("tranches_pts") and len(fired) < 12:   # 馬丁補批:跌越深越多批
            k = len([f for f in fired if f[0] == "pts"])
            trig, add = cfg["tranches_pts"][0]
            while dd_pts >= trig*(k+1) and k < 12:
                exposure = min(exposure + add*half, cfg.get("cap", 99)); fired.add(("pts", k)); k += 1
    yearly[y0] = eq/eq_y - 1
    series.append((dates[-1], exposure))
    return dict(name=cfg["name"], ruin=None, total=f"{(eq-1)*100:+.0f}%",
                maxdd=f"{maxdd*100:.0f}%", stops=stops, series=series,
                yearly={k: f"{v*100:+.0f}%" for k, v in sorted(yearly.items())})

SCENARIOS = [
    dict(name="A 現行:5x起+每千點加1x(無上限無濾網)", base=5.0, tranches_pts=[[1000, 1.0]], cap=12),
    dict(name="B 現行改良:同上但總曝險cap 6x", base=5.0, tranches_pts=[[1000, 1.0]], cap=6),
    dict(name="C v1建議:0.5x起,-6/-10/-15/-20%加0.5/0.5/0.75/0.75,cap2.5,濾網+權益停損20%",
         base=0.5, tranches=[[6, 0.5], [10, 0.5], [15, 0.75], [20, 0.75]], cap=2.5,
         bear_filter=True, eq_stop=20),
    dict(name="D v1無濾網(看濾網值多少)", base=0.5, tranches=[[6, 0.5], [10, 0.5], [15, 0.75], [20, 0.75]],
         cap=2.5, eq_stop=20),
    dict(name="E 純1x buy&hold對照", base=1.0, tranches=[], cap=1.0),
]

def main():
    data = load_index()
    cfgs, emit = SCENARIOS, None
    if "--emit" in sys.argv:                       # T02: --emit 檔名 → 輸出 date,target_exposure
        k = sys.argv.index("--emit"); emit = sys.argv[k+1]
    if len(sys.argv) > 2 and sys.argv[1] == "--json":
        cfgs = [json.loads(sys.argv[2])]
    print(f"資料: {data[0][0]} ~ {data[-1][0]}  共{len(data)}日  (回測起點=資料起點)\n")
    for cfg in cfgs:
        r = run(data, cfg)
        print(f"■ {r['name']}")
        if r["ruin"]:
            print(f"  💀 {r['ruin']} 權益歸零(畢業)")
        print(f"  總報酬 {r['total']}｜最大回撤 {r['maxdd']}｜權益停損觸發 {r['stops']} 次")
        if emit and r.get("series"):
            with open(emit, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f); w.writerow(["date", "target_exposure"])
                w.writerows([(d, round(x, 4)) for d, x in r["series"]])
            print(f"  ✓ 曝險序列 {len(r['series'])} 列 → {emit}")
        print("  年度: " + "  ".join(f"{y}:{v}" for y, v in list(r["yearly"].items())[-9:]) + "\n")

if __name__ == "__main__":
    main()
