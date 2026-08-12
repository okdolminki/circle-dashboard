#!/usr/bin/env python3
"""Circle (CRCL) 모니터링 대시보드 - 데이터 수집 스크립트.

표준 라이브러리만 사용한다 (pip 설치 불필요). GitHub Actions 무료 러너와
로컬 어디서든 그대로 실행된다.

    python3 fetch_data.py

결과는 data/latest.json 에 쓰고, 일별 스냅샷을 data/history.json 에 누적한다.

데이터 소스 (전부 무료, API 키 불필요):
  - SEC EDGAR companyfacts XBRL  : 분기 재무 (총매출, 준비금수익, 기타수익, RLDC)
  - SEC EDGAR submissions        : 최근 공시 목록 (8-K, 10-Q 감지)
  - DefiLlama                    : USDC 유통량 (현재 + 히스토리)
  - CoinGecko                    : USYC AUM (시가총액)
  - NY Fed                       : SOFR
  - US Treasury                  : 단기 국채 수익률 곡선 (1M/3M/6M/1Y)
  - Yahoo Finance chart          : CRCL 주가
"""
import json
import ssl
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 수동 갱신 설정값 (출처와 갱신 주기는 README 참고)
# ---------------------------------------------------------------------------
CONFIG = {
    # 금리 민감도 패스스루 계수: 준비금 총수익 변동 중 Circle에 남는 비율.
    # 출처: Q2'26 10-Q Item 7A (2026-08-05 제출, "Change in interest rates from
    #        average yield of 3.49% in June 2026" 표).
    #   +100bp -> 준비금수익 +$737M, 유통·거래비용 +$360M -> 순효과 +$377M = 51.15%
    #   +200bp -> 준비금수익 +$1,475M, 유통·거래비용 +$720M -> 순효과 +$755M = 51.19%
    # 같은 표의 전년 비교행("yield 4.26% in June 2025")은 618/315/303 = 49.03%로,
    # 이전 CONFIG 값 0.49와 정확히 일치 (기존 값의 출처 검증 완료).
    # 즉 49.0% -> 51.2%로 실측 상승. 온플랫폼 비중이 7.5%(Q2'25) -> 19.5%(Q2'26)로
    # 뛴 것과 같은 방향 - 온플랫폼 잔고는 유통비용이 안 붙어 한계 패스스루를 높인다는
    # 가설을 실측이 뒷받침한다. 다음 10-Q에서 이 표가 갱신되면 재확인할 것.
    "net_passthrough": 0.512,
    # 준비금 중 Circle Reserve Fund(USDXX, BlackRock) 비중.
    # 2026-06 실측: $61,917M / $73,161M = 0.846
    # (BlackRock 팩트시트 순자산 / 10-Q 대차대조표 stablecoin holder 분리자산)
    "reserve_fund_share": 0.846,
    # USDXX 실부담 보수율. 총보수 0.21%, 면제 후 0.17% (BlackRock 팩트시트).
    # 이 수수료가 RRR과 SOFR의 격차를 사실상 전부 설명한다:
    #   Q2'26 관측 격차 14.5bp  vs  이론값 0.17% x 0.846 = 14.4bp
    # 격차가 좁아지면 Circle이 OCC 신탁은행 인가를 지렛대로 수수료 경제를
    # 회수하고 있다는 신호다 (재협상이든 부분 내재화든 경로 무관하게 관측된다).
    "usdxx_expense_ratio": 0.0017,
    # USDXX 가중평균만기(WAM), 일. 2026-06 팩트시트 실측 5일.
    # 초단기라 금리 변동이 사실상 즉시 수익률에 반영된다 = 시차 헤지 효과가 없다.
    # 법상 상한은 GENIUS Act(개별 국채 93일)와 Rule 2a-7(WAM 60일)이지만,
    # 분기 상환 회전율이 1.19배(Q2'26 소각 $87B / 유통 $73.3B)라 사업 모델이 먼저 막는다.
    "usdxx_wam_days": 5,
    "circle_cik": "0001876042",
}

# ---------------------------------------------------------------------------
# 분기별 수동 입력: 비GAAP 지표와 실적 보도자료에만 나오는 운영 지표.
# SEC XBRL에 태그가 없어 자동 수집이 불가능하다. 실적 발표 때 분기당 한 번 갱신한다.
#
# 출처: 분기 실적 보도자료의 Key Financial Results / Key Operating Indicators /
#       Non-GAAP 조정표.
#   adj_opex        : Adjusted Operating Expenses
#   adj_ebitda      : Adjusted EBITDA (New Definition)
#   arc_recognized  : 해당 분기 인식한 ARC 토큰 프리세일 수익 (미인식 분기는 0)
#                     FY26 가이던스에 총 $180M 포함 (프리세일 총액 $242M의 약 75%).
#   usdc_avg        : USDC in Circulation, average of period. 비우면 DefiLlama로 근사.
#   on_platform_pct : USDC on Platform, daily weighted average percentage
#
# 참고: Adjusted EBITDA ~= RLDC - Adjusted OpEx 항등식이 5개 분기 모두 오차 $2.2M
#       이내로 성립한다. 입력값 검산에 쓸 수 있다.
# ---------------------------------------------------------------------------
QUARTERLY_MANUAL = {
    "2025Q2": {"adj_opex": 119.366e6, "adj_ebitda": 132.997e6, "arc_recognized": 0,
               "usdc_avg": 61.2e9, "on_platform_pct": 7.5},
    "2025Q3": {"adj_opex": 122.686e6, "adj_ebitda": 171.476e6, "arc_recognized": 0,
               "usdc_avg": None, "on_platform_pct": None},
    "2025Q4": {"adj_opex": 132.806e6, "adj_ebitda": 175.910e6, "arc_recognized": 0,
               "usdc_avg": None, "on_platform_pct": None},
    "2026Q1": {"adj_opex": 135.677e6, "adj_ebitda": 151.401e6, "arc_recognized": 0,
               "usdc_avg": None, "on_platform_pct": None},
    "2026Q2": {"adj_opex": 146.380e6, "adj_ebitda": 143.478e6, "arc_recognized": 0,
               "usdc_avg": 76.5e9, "on_platform_pct": 19.5},
}

UA = {"User-Agent": "Third State Research okdolminki@gmail.com"}
try:  # macOS 기본 파이썬은 루트 인증서가 연결 안 된 경우가 있어 certifi 우선
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()


def get(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers=headers or UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.read()
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
        # 공개 데이터 조회용 최후 폴백 (로컬 인증서 미설정 환경)
        unverified = ssl._create_unverified_context()  # noqa: SLF001
        with urllib.request.urlopen(req, timeout=timeout, context=unverified) as r:
            return r.read()


def get_json(url, headers=None):
    return json.loads(get(url, headers))


def safe(fn, label):
    """소스 하나가 죽어도 전체 수집이 멈추지 않게 한다."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {label} failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 1) SEC EDGAR: 분기 재무 -> RLDC 마진, 기타 수익 비중 (소수점 둘째 자리)
# ---------------------------------------------------------------------------
def fetch_edgar_quarters():
    cik = CONFIG["circle_cik"]
    facts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    gaap = facts["facts"]["us-gaap"]

    def frames(tag):
        out = {}
        for v in gaap.get(tag, {}).get("units", {}).get("USD", []):
            if v.get("form") not in ("10-Q", "10-K"):
                continue
            s, e = v.get("start"), v.get("end")
            if s and e:
                out[(s, e)] = v["val"]  # 나중 공시가 이전 값을 덮어씀 (정정 반영)
        return out

    rev = frames("Revenues")
    reserve = frames("InterestAndDividendIncomeOperating")
    opex = frames("OperatingExpenses")
    opinc = frames("OperatingIncomeLoss")
    # 손익계산서의 '기타비용(Other costs)' 라인. 유통·거래비용과 분리해야
    # Net Reserve Margin을 회사 정의대로 정확히 계산할 수 있다.
    othercost = frames("OtherCostOfOperatingRevenue")
    # 감가상각비. Adjusted OpEx에서 제외되는 항목이라, 신사업 투자 강도를 보려면
    # Adjusted OpEx에 다시 더해야 한다 (자본화된 엔지니어링 비용의 상각 포함).
    dna = frames("DepreciationAndAmortization")

    q_ends = ["03-31", "06-30", "09-30", "12-31"]

    def quarterly(series):
        """직접 보고된 분기값 + YTD 차분으로 분기 시계열 복원."""
        years = sorted({k[1][:4] for k in series})
        out = {}
        for y in years:
            ytd = {}  # 분기번호 -> 연초누적값
            for (s, e), val in series.items():
                if s == f"{y}-01-01" and e[:4] == y and e[5:] in q_ends:
                    ytd[q_ends.index(e[5:]) + 1] = val
            direct = {}
            for (s, e), val in series.items():
                if e[:4] == y and e[5:] in q_ends and s[:4] == y:
                    n = q_ends.index(e[5:]) + 1
                    q_start = f"{y}-{['01-01','04-01','07-01','10-01'][n-1]}"
                    if s == q_start:
                        direct[n] = val
            for n in range(1, 5):
                if n in direct:
                    out[f"{y}Q{n}"] = direct[n]
                elif n in ytd and (n == 1 or (n - 1) in ytd):
                    out[f"{y}Q{n}"] = ytd[n] - (ytd.get(n - 1, 0) if n > 1 else 0)
                elif n in ytd and n > 1:
                    prev = sum(out.get(f"{y}Q{i}", 0) for i in range(1, n))
                    known = all(f"{y}Q{i}" in out for i in range(1, n))
                    if known:
                        out[f"{y}Q{n}"] = ytd[n] - prev
        return out

    q_rev = quarterly(rev)
    q_res = quarterly(reserve)
    q_opex = quarterly(opex)
    q_opinc = quarterly(opinc)
    q_othercost = quarterly(othercost)
    q_dna = quarterly(dna)

    rows = []
    for q in sorted(set(q_rev) & set(q_opex) & set(q_opinc)):
        total = q_rev[q]
        res = q_res.get(q)
        other = (total - res) if res is not None else None
        # 유통·거래·기타비용 합계는 XBRL에 별도 태그가 없어 역산한다:
        # DTOC = 총매출 - 영업비용 - 영업이익  (FY25 검증: $1,663.7M = 공시와 일치)
        dtoc = total - q_opex[q] - q_opinc[q]
        rldc = total - dtoc
        # Net Reserve Margin = (준비금이자 - 유통·거래비용) / 준비금이자
        # 회사 각주 정의상 '기타비용'은 빠진다. Q2'26 검산: (667.7-410.3)/667.7 = 38.5%
        oc = q_othercost.get(q)
        dist_txn = (dtoc - oc) if oc is not None else None
        nrm = (round((res - dist_txn) / res * 100, 2)
               if (dist_txn is not None and res) else None)
        rows.append({
            "quarter": q,
            "total_revenue": total,
            "reserve_income": res,
            "other_revenue": other,
            "dtoc": dtoc,
            "other_costs": oc,
            "distribution_transaction_costs": dist_txn,
            "dna": q_dna.get(q),
            "rldc": rldc,
            "rldc_margin_pct": round(rldc / total * 100, 2),
            "net_reserve_margin_pct": nrm,
            "other_share_pct": round(other / total * 100, 2) if other is not None else None,
        })
    return rows


def fetch_edgar_filings():
    cik = CONFIG["circle_cik"]
    sub = get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    rec = sub["filings"]["recent"]
    out = []
    for i in range(min(12, len(rec["form"]))):
        acc = rec["accessionNumber"][i].replace("-", "")
        doc = rec["primaryDocument"][i]
        out.append({
            "form": rec["form"][i],
            "date": rec["filingDate"][i],
            "desc": rec.get("primaryDocDescription", [""] * 99)[i] or rec["form"][i],
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{doc}",
        })
    return out


# ---------------------------------------------------------------------------
# 2) 실시간 시장 데이터
# ---------------------------------------------------------------------------
def fetch_usdc():
    d = get_json("https://stablecoins.llama.fi/stablecoins")
    for a in d["peggedAssets"]:
        if a["symbol"] == "USDC" and a.get("name") == "USD Coin":
            circ = a["circulating"]["peggedUSD"]
            asset_id = a["id"]
            hist = safe(lambda: get_json(
                f"https://stablecoins.llama.fi/stablecoincharts/all?stablecoin={asset_id}"
            ), "usdc history")
            series = []
            if hist:
                for p in hist[-400:]:
                    v = p.get("totalCirculating", {}).get("peggedUSD")
                    if v:
                        series.append([int(p["date"]), round(v)])
            return {"circulating": circ, "history": series}
    return None


def fetch_usyc():
    d = get_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        "?vs_currency=usd&ids=hashnote-usyc"
    )
    if d:
        return {"aum": d[0]["market_cap"], "updated": d[0]["last_updated"]}
    return None


def fetch_sofr():
    d = get_json("https://markets.newyorkfed.org/api/rates/secured/sofr/last/10.json")
    rows = [
        {"date": r["effectiveDate"], "rate": r["percentRate"]}
        for r in d["refRates"]
    ]
    return sorted(rows, key=lambda r: r["date"])  # API가 최신순으로 주므로 정렬


def fetch_sofr_quarterly_avg(years_back=3):
    """분기 평균 SOFR. RRR(분기 평균 수익률)과 같은 기간 기준으로 비교하기 위함."""
    end = date.today()
    start = date(end.year - years_back, 1, 1)
    d = get_json(
        "https://markets.newyorkfed.org/api/rates/secured/sofr/search.json"
        f"?startDate={start}&endDate={end}"
    )
    buckets = {}
    for r in d.get("refRates", []):
        eff, rate = r.get("effectiveDate"), r.get("percentRate")
        if not eff or rate is None:
            continue
        q = f"{eff[:4]}Q{(int(eff[5:7]) - 1) // 3 + 1}"
        buckets.setdefault(q, []).append(rate)
    return {q: round(sum(v) / len(v), 4) for q, v in buckets.items()}


def fetch_treasury():
    y = date.today().year
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={y}&page&_format=csv"
    )
    text = get(url).decode()
    lines = [l for l in text.splitlines() if l.strip()]
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    latest = lines[1].split(",")
    want = {"Date": None, "1 Mo": None, "3 Mo": None, "6 Mo": None, "1 Yr": None}
    for k in want:
        if k in header:
            want[k] = latest[header.index(k)].strip().strip('"')
    return want


def fetch_crcl():
    d = get_json(
        "https://query1.finance.yahoo.com/v8/finance/chart/CRCL?range=6mo&interval=1d",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    res = d["chart"]["result"][0]
    meta = res["meta"]
    ts = res.get("timestamp", [])
    closes = res["indicators"]["quote"][0].get("close", [])
    series = [
        [t, round(c, 2)] for t, c in zip(ts, closes) if c is not None
    ]
    return {
        "price": meta.get("regularMarketPrice"),
        "high52w": meta.get("fiftyTwoWeekHigh"),
        "low52w": meta.get("fiftyTwoWeekLow"),
        "history": series,
    }


# ---------------------------------------------------------------------------
# 3) 파생 계산
# ---------------------------------------------------------------------------
def usdc_quarterly_avg(usdc):
    """DefiLlama 히스토리로 분기 평균 유통량 근사. 수동 입력이 없을 때만 쓴다."""
    if not usdc or not usdc.get("history"):
        return {}
    buckets = {}
    for ts, val in usdc["history"]:
        d = datetime.fromtimestamp(ts, timezone.utc)
        buckets.setdefault(f"{d.year}Q{(d.month - 1) // 3 + 1}", []).append(val)
    return {q: sum(v) / len(v) for q, v in buckets.items()}


def enrich_quarters(quarters, sofr_q, usdc):
    """분기 행에 수동 입력값을 합치고 파생 지표를 계산한다.

    핵심 지표 두 가지:
      - rrr_sofr_spread_bps : RRR과 SOFR의 격차. 사실상 BlackRock 펀드 수수료다.
        좁아지면 Circle이 준비금 운용 경제를 회수하고 있다는 신호.
      - *_ex_arc            : ARC 토큰 프리세일(1회성)을 걷어낸 계열.
        ARC는 유통비용이 0이라 RLDC와 Adjusted EBITDA에 1:1로 꽂힌다.
    """
    if not quarters:
        return quarters
    fee_drag_bps = round(
        CONFIG["usdxx_expense_ratio"] * CONFIG["reserve_fund_share"] * 10000, 1
    )
    llama_avg = usdc_quarterly_avg(usdc)

    for r in quarters:
        q = r["quarter"]
        m = QUARTERLY_MANUAL.get(q, {})
        r["fee_drag_bps_theory"] = fee_drag_bps

        # --- Reserve Return Rate 와 SOFR 격차 ---------------------------------
        avg = m.get("usdc_avg") or llama_avg.get(q)
        r["usdc_avg"] = round(avg) if avg else None
        r["usdc_avg_source"] = ("실적발표" if m.get("usdc_avg")
                                else ("DefiLlama 근사" if avg else None))
        rrr = (r["reserve_income"] * 4 / avg * 100
               if (avg and r.get("reserve_income")) else None)
        r["rrr_pct"] = round(rrr, 3) if rrr else None
        r["sofr_avg_pct"] = sofr_q.get(q)
        r["rrr_sofr_spread_bps"] = (
            round((r["sofr_avg_pct"] - rrr) * 100, 1)
            if (rrr and r["sofr_avg_pct"]) else None
        )

        # --- 수동 입력 + ARC 제외 계열 ----------------------------------------
        arc = m.get("arc_recognized")
        for k in ("adj_opex", "adj_ebitda", "on_platform_pct"):
            r[k] = m.get(k)
        r["arc_recognized"] = arc
        if arc is not None:
            r["other_revenue_ex_arc"] = r["other_revenue"] - arc
            rev_ex, rldc_ex = r["total_revenue"] - arc, r["rldc"] - arc
            r["rldc_ex_arc"] = rldc_ex
            r["rldc_margin_ex_arc_pct"] = round(rldc_ex / rev_ex * 100, 2)
            r["other_share_ex_arc_pct"] = round(
                r["other_revenue_ex_arc"] / rev_ex * 100, 2)

        # --- Adjusted EBITDA 마진과 실질 비용 강도 ------------------------------
        # Adjusted EBITDA Margin의 분모는 총매출이 아니라 RLDC다 (회사 각주 정의).
        # 항등식 Adjusted EBITDA ~= RLDC - Adjusted OpEx 이므로
        # 이 마진은 곧 "순매출 중 운영비가 먹지 않은 비율"이다.
        if r.get("adj_ebitda"):
            r["adj_ebitda_margin_pct"] = round(r["adj_ebitda"] / r["rldc"] * 100, 2)
            if arc:
                r["adj_ebitda_margin_ex_arc_pct"] = round(
                    (r["adj_ebitda"] - arc) / r["rldc_ex_arc"] * 100, 2)
        # 비용 강도: Adjusted OpEx는 D&A를 제외하는데 D&A에 자본화된 엔지니어링
        # 상각(=신사업 개발비 일부)이 들어있다. 다시 더해야 실제 투자 강도가 보인다.
        if r.get("adj_opex") and r.get("dna"):
            r["cost_intensity_pct"] = round(
                (r["adj_opex"] + r["dna"]) / r["rldc"] * 100, 2)
    return quarters


def compute_sensitivity(usdc, sofr):
    if not usdc:
        return None
    circ = usdc["circulating"]
    latest_sofr = sofr[-1]["rate"] if sofr else None
    per_100bp_gross = circ * 0.01
    net = CONFIG["net_passthrough"]
    # RRR = SOFR - (USDXX 보수율 x 펀드 비중). Q2'26 검증: 3.68 - 0.144 = 3.54 vs 실제 3.48
    # (잔차는 은행예금 비중 15.4%의 수익률 차이. 격차의 대부분은 펀드 수수료다.)
    fee_drag = CONFIG["usdxx_expense_ratio"] * CONFIG["reserve_fund_share"] * 100
    implied_rrr = (latest_sofr - fee_drag) if latest_sofr else None
    return {
        "usdc_circulating": circ,
        "sofr_latest": latest_sofr,
        "fee_drag_bps": round(fee_drag * 100, 1),
        "implied_rrr_pct": round(implied_rrr, 3) if implied_rrr else None,
        # 준비금 총수익 연환산 = 유통량 x (SOFR - 펀드 수수료)
        "gross_income_run_rate": circ * (implied_rrr / 100) if implied_rrr else None,
        "per_100bp_gross": per_100bp_gross,
        "per_25bp_gross": per_100bp_gross / 4,
        "per_100bp_net_to_circle": per_100bp_gross * net,
        "per_25bp_net_to_circle": per_100bp_gross / 4 * net,
        "net_passthrough": net,
        "phase_in_days_approx": CONFIG["usdxx_wam_days"],
    }


# ---------------------------------------------------------------------------
def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    quarters = safe(fetch_edgar_quarters, "edgar quarters")
    filings = safe(fetch_edgar_filings, "edgar filings")
    usdc = safe(fetch_usdc, "usdc")
    usyc = safe(fetch_usyc, "usyc")
    sofr = safe(fetch_sofr, "sofr")
    sofr_q = safe(fetch_sofr_quarterly_avg, "sofr quarterly") or {}
    treasury = safe(fetch_treasury, "treasury")
    crcl = safe(fetch_crcl, "crcl")
    sens = compute_sensitivity(usdc, sofr or [])
    quarters = safe(lambda: enrich_quarters(quarters, sofr_q, usdc), "enrich") or quarters

    latest = {
        "fetched_at": now,
        "config": CONFIG,
        "sofr_quarterly_avg": sofr_q,
        "quarters": quarters,
        "filings": filings,
        "usdc": usdc,
        "usyc": usyc,
        "sofr": sofr,
        "treasury": treasury,
        "crcl": crcl,
        "sensitivity": sens,
    }
    (DATA / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=1)
    )

    # 일별 스냅샷 누적 (대시보드가 자체 히스토리를 갖게 됨)
    hist_path = DATA / "history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else {}
    hist[str(date.today())] = {
        "usdc": usdc and round(usdc["circulating"]),
        "usyc": usyc and usyc["aum"],
        "sofr": sofr and sofr[-1]["rate"],
        "crcl": crcl and crcl["price"],
    }
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=1))

    ok = [k for k, v in latest.items()
          if v is not None and k not in ("fetched_at", "config")]
    print(f"OK {now} sources: {', '.join(ok)}")
    if quarters:
        q = quarters[-1]
        print(
            f"  latest quarter {q['quarter']}: RLDC margin {q['rldc_margin_pct']}%"
            f", other share {q['other_share_pct']}%"
            f", net reserve margin {q.get('net_reserve_margin_pct')}%"
        )
        print(
            f"  RRR {q.get('rrr_pct')}% vs SOFR {q.get('sofr_avg_pct')}%"
            f" -> spread {q.get('rrr_sofr_spread_bps')}bp"
            f" (이론 {q.get('fee_drag_bps_theory')}bp = BlackRock 수수료)"
        )


if __name__ == "__main__":
    main()
