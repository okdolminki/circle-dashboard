# Circle (CRCL) 모니터링 대시보드

Circle의 금리 민감도, RLDC 마진, 기타 수익 비중을 한 화면에서 추적한다.
전부 무료 인프라(GitHub Actions + GitHub Pages)와 무료 공개 API만 쓴다. API 키 불필요.

## 지표와 갱신 주기

| 지표 | 소스 | 갱신 |
|---|---|---|
| USDC 유통량 | DefiLlama | 수집 시마다 (기본 6시간) |
| USYC AUM (근사) | CoinGecko 시가총액 | 수집 시마다 |
| SOFR | NY Fed | 영업일 |
| 국채 1M/3M/6M/1Y | US Treasury | 영업일 |
| CRCL 주가 | Yahoo Finance | 수집 시마다 |
| 금리 민감도 | 위 데이터로 자동 계산 | 수집 시마다 |
| RLDC 마진, 기타 수익 비중 | SEC EDGAR XBRL (10-Q/10-K) | 분기 공시 직후 자동 반영 |
| Net Reserve Margin | SEC EDGAR XBRL | 분기 공시 직후 자동 반영 |
| RRR − SOFR 격차 | XBRL + NY Fed 분기평균 SOFR | 분기 공시 직후 자동 반영 |
| 비용 강도, ARC 제외 계열 | XBRL + 수동 입력 | 실적 발표 후 수동 (아래 참고) |
| 최근 공시 목록 | SEC EDGAR submissions | 수집 시마다 |

분기 지표는 회사가 10-Q/10-K를 제출하는 순간 XBRL에 실리므로, 다음 수집 사이클에
자동으로 새 분기가 추가된다. 실적 발표일에는 Actions 탭에서 `Run workflow`를 눌러
즉시 수집하면 된다.

## 로컬 실행

```bash
python3 fetch_data.py        # data/latest.json 생성
python3 -m http.server 8741  # 정적 서빙
# 브라우저에서 http://localhost:8741 접속
```

`fetch()`가 로컬 파일을 읽어야 하므로 반드시 http 서버로 열어야 한다
(파일 더블클릭 file:// 로는 안 열림).

## GitHub 무료 배포 (5분)

1. GitHub에서 새 저장소 생성 (public이면 Pages 무료).
2. 이 폴더를 통째로 push:
   ```bash
   cd dashboard
   git init && git add -A && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<계정>/<저장소>.git
   git push -u origin main
   ```
3. 저장소 Settings → Pages → Source: `Deploy from a branch` → `main` / `/ (root)`.
4. Settings → Actions → General → Workflow permissions: `Read and write permissions` 선택.
5. 끝. 6시간마다 Actions가 데이터를 갱신-커밋하고 Pages가 자동 재배포한다.
   대시보드 주소: `https://<계정>.github.io/<저장소>/`

## 금리 민감도 방법론

실시간 계산 가능 여부에 대한 답: **가능하다. 이유는 준비금이 초단기 자산뿐이라
민감도 구조가 단순하기 때문이다.**

- 준비금 구성: 약 85~90%는 Circle Reserve Fund(USDXX, BlackRock 운용 정부 MMF -
  T-bill 93일 이하 + 익일물 repo), 나머지는 은행 현금. 듀레이션이 사실상 0에
  가까워서, 금리가 움직이면 자산 가격 손익은 무시할 수준이고 **이자 수익이
  거의 전액 따라 움직인다.**
- 그래서 총수익 민감도는 사실상 `USDC 유통량 x 금리 변동폭`이 전부다.
  자산별 민감도를 몰라도 된다. 필요한 건 유통량(실시간)과 반영 시차(WAM)뿐.
- 반영 시차: **사실상 없다.** USDXX의 WAM은 2026년 6월 팩트시트 기준 **5일**이다
  (이전에 35일로 잡아뒀던 것은 오류). 포트폴리오가 일주일이면 통째로 새 금리로
  갈아탄다. 즉 **만기를 길게 잡아 인하를 늦게 맞는 시차 헤지 효과가 없다.**
  법상 상한은 GENIUS Act(개별 국채 93일)와 Rule 2a-7(WAM 60일, WAL 120일)이지만,
  실제로는 사업 모델이 먼저 막는다. Q2'26 분기 상환 회전율이 1.19배
  (소각 $87B / 유통 $73.3B)라 만기를 늘리면 상환 대응을 시장 매각으로 해야 한다.
- Circle 순효과: 총수익 변동의 전부가 Circle 몫이 아니다. 유통비용(코인베이스 등)이
  수익에 연동돼 같이 움직인다. 10-K 민감도 공시(+100bp -> 총수익 +$618M,
  유통비용 +$315M)에서 도출한 패스스루 49%를 적용한다.

### RRR − SOFR 격차 = BlackRock 수수료

준비금 실효 수익률(RRR)이 SOFR보다 낮은 이유는 사실상 전부 **Circle Reserve Fund의
운용보수**다. USDXX 실부담 보수율 0.17% × 펀드 비중 0.846 = **14.4bp**이고,
Q2'26 실측 격차가 12.8bp다 (분기별 12.8~19.1bp, 평균 약 15bp).

그래서 준비금 이자를 이렇게 모델링할 수 있다.

```
준비금 이자 = 평균 USDC 유통량 × (SOFR − 0.144%)
```

이 격차가 좁아지면 Circle이 OCC 신탁은행 인가를 지렛대로 수수료 경제를 회수하고
있다는 신호다. 재협상이든 부분 내재화든 **경로와 무관하게 이 하나로 관측된다.**

### 수동 갱신 항목 (`fetch_data.py`의 CONFIG)

| 항목 | 현재값 | 갱신 시점 | 출처 |
|---|---|---|---|
| `net_passthrough` | **0.512** (2026-08-11 확정) | 매 10-Q Item 7A 갱신 시 | Q2'26 10-Q (2026-08-05 제출) |
| `reserve_fund_share` | 0.846 | 분기 | BlackRock 팩트시트 순자산 ÷ 10-Q 분리자산 |
| `usdxx_expense_ratio` | 0.0017 | 반기 | BlackRock 팩트시트 (총 0.21%, 면제 후 0.17%) |
| `usdxx_wam_days` | 5 | 월 1회 확인 | BlackRock USDXX 팩트시트, SEC N-MFP |

`net_passthrough`는 Q2'26 10-Q Item 7A로 **0.512로 확정**했다(2026-08-11). 직전
분기 대비 온플랫폼 비중이 7.5%→19.5%로 뛰면서 패스스루도 49.0%→51.2%로 실측
상승 — 온플랫폼 잔고에 유통비용이 안 붙는다는 가설을 실측이 뒷받침한다. 10-Q의
이 표는 "만약 7/1일에 금리가 바뀌었다면"을 가정한 12개월 전망 모델이라, 다음
분기 10-Q에서 갱신되면 값을 다시 확인해 교체할 것.

### 분기 수동 입력 (`fetch_data.py`의 QUARTERLY_MANUAL)

비GAAP 지표와 실적 보도자료에만 나오는 운영 지표는 XBRL에 태그가 없다.
**실적 발표 후 분기당 한 번**, 보도자료를 보고 4개 숫자를 넣는다.

| 키 | 어디서 | 비고 |
|---|---|---|
| `adj_opex` | Non-GAAP 조정표 | Adjusted Operating Expenses |
| `adj_ebitda` | Non-GAAP 조정표 | Adjusted EBITDA (New Definition) |
| `arc_recognized` | 가이던스 각주 | 해당 분기 인식 ARC 프리세일 수익. FY26 총 $180M 예정 |
| `on_platform_pct` | Key Operating Indicators | USDC on Platform, daily weighted average % |
| `usdc_avg` | Key Operating Indicators | 비우면 DefiLlama로 근사 (격차가 흔들릴 수 있음) |

**검산법**: `Adjusted EBITDA ≈ RLDC − Adjusted OpEx`가 5개 분기 모두 오차 $2.2M
이내로 성립한다. 입력 후 이 항등식이 깨지면 숫자를 잘못 넣은 것이다.

Q3'26부터는 ARC 프리세일 $180M이 유통비용 0으로 RLDC에 꽂히므로 RLDC 마진이 약
400bp, Adjusted EBITDA 마진이 약 16%p 부풀려진다. 반드시 `*_ex_arc` 계열로 볼 것.

## 한계 (정직하게)

- **유통·거래·기타비용 합계는 역산값이다.** XBRL에 별도 태그가 없어
  `총매출 - 영업비용 - 영업이익`으로 계산한다. FY2025 공시값($1,663.7M)과
  일치함을 검증했지만, 특정 분기에 영업비용 밖의 일회성 항목이 있으면 왜곡될 수
  있다 (예: 2024Q4의 Binance 선불금 $60.25M -> 마진 30.05%로 하락한 것은 실제).
  여기서 `OtherCostOfOperatingRevenue` 태그로 기타비용을 빼면 Net Reserve Margin에
  필요한 순수 유통·거래비용이 나온다 (Q2'26 검증: 410.4M = 공시와 일치).
- 조정 EBITDA와 Adjusted OpEx는 non-GAAP이라 XBRL에 없다 → `QUARTERLY_MANUAL`로 입력.
- **RRR은 분기 평균값이다.** 평균 유통량을 실적 발표치로 쓰는 분기가 정확하고,
  DefiLlama 근사로 채운 분기는 격차가 실제보다 흔들린다 (표에서 확인 가능).
- USYC AUM은 CoinGecko 시가총액 근사치다 (온체인 토큰 기준).
- 8-K 보도자료 수치는 XBRL보다 몇 시간~며칠 먼저 나온다. 공시 목록 카드에서
  8-K가 뜨면 링크로 바로 확인하고, 정밀값은 10-Q 반영 시 자동 갱신된다.
