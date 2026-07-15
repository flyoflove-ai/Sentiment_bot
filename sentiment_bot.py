# -*- coding: utf-8 -*-
"""
시장 심리(공포/탐욕) 진단 텔레그램 봇 v5
==========================================
v6 변경 (무료 운영 구조 — 상시 리스너 제거):
  - 리스너(15분 주기) 삭제 → Actions 사용량 월 150분 미만 (무료 한도 내 여유)
  - 답장 대기 방식 전환: 진단 발송 직후 같은 실행 안에서 최대 15분간(long-poll)
    사용자 답장을 대기 → 수신 즉시 최종 진단 발송 후 종료. 미수신 시 지난 응답
    기반 잠정 진단으로 완결 (ANSWER_WAIT_MIN 환경변수로 조정, 0이면 대기 없음)
  - --daily : 평일 장 마감(15:40 KST) 1회 실행 (v7)
      ① 메시지 수거: 하루 중 보낸 "심리"/"긴급"/"진단" 요청과 숫자 답변을 일괄 처리
         — 상시 리스너 없이 텔레그램 트리거 지원, 추가 Actions 사용량 0
      ② 급락 체크: KOSPI 전일比 -10% 이하(서킷브레이커급)이면 긴급 진단 자동 발송
        (하루 1회 쿨다운)
  - 긴급 즉시 실행: GitHub 앱 → Actions → Run workflow — 지연 없음.
    텔레그램 "심리" 전송은 당일 15:40 수거 실행에서 처리 (즉시성 필요 시 Run workflow)

환경변수:
  TELEGRAM_BOT_TOKEN  : 봇 토큰
  TELEGRAM_CHAT_ID    : 본인 chat_id
  GEMINI_API_KEY      : Gemini API 키 (무료, 기존 리서치 에이전트 키 재사용 —
                        aistudio.google.com/apikey). AI 웹조사 5문항 담당
  ANTHROPIC_API_KEY   : (선택) Claude API 키. GEMINI_API_KEY 부재 시 폴백.
                        둘 다 없으면 AI 조사 5문항이 사용자 질문으로 전환 (9문항 응답)
  FRED_API_KEY        : (선택) 하이일드 스프레드. 없으면 자동 제외 후 가중 재배분

의존성: pip install yfinance requests
실행:  python sentiment_bot.py            # long-polling 상시 실행
       python sentiment_bot.py --auto     # 정량+AI조사만 1회 발송 (Actions cron용)
"""

import os
import re
import json
import time
import sys
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FRED_KEY = os.environ.get("FRED_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")        # 무료 (기존 리서치 에이전트 키 재사용)
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")  # 선택 (있으면 Gemini 부재 시 폴백)
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_DIR = os.environ.get("STATE_DIR", "state")
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "sentiment_state.json")   # 진행 중 설문
OFFSET_FILE = os.path.join(STATE_DIR, "tg_offset.json")        # 텔레그램 오프셋
ANSWERS_FILE = os.path.join(STATE_DIR, "last_answers.json")    # 최근 사용자 응답
CRASH_FILE = os.path.join(STATE_DIR, "last_crash_alert.json")  # 급락 알림 쿨다운

# ======================================================================
# 사용자 답변 4문항 — 1~5점 상세 앵커 (2026-07-14 세션에서 확정한 기준표)
# ======================================================================
USER_QUESTIONS = [
    {
        "name": "주변 주식 얘기 빈도", "weight": 6,
        "anchors": {
            1: "주식 얘기 실종. 꺼내면 분위기 싸해짐",
            2: "가끔 나오나 손실 한탄·후회담 위주",
            3: "평상시 수준. 관심층끼리만 언급",
            4: "모임마다 종목·수익 얘기가 자연스럽게 나옴",
            5: "어느 자리든 주식이 대화 중심",
        },
    },
    {
        "name": "초보 지인 진입·문의", "weight": 6,
        "anchors": {
            1: "'주식=패가망신' 분위기. 계좌 해지·탈출 선언",
            2: "신규 진입 소식 없음. 기존 투자자도 조용",
            3: "원래 하던 사람만 지속. 신규 문의 거의 없음",
            4: "안 하던 지인 1~2명 계좌 개설·종목 문의",
            5: "부모님·무관심층까지 진입. 추천 요청 쇄도",
        },
    },
    {
        "name": "'이번엔 다르다' 담론", "weight": 3,
        "anchors": {
            1: "'국장은 끝났다'가 지배적",
            2: "비관론 우세. 상승론자가 방어적",
            3: "강세론·약세론 팽팽한 논쟁",
            4: "'AI 시대라 다르다'·'눌림목' 낙관 논리 우세",
            5: "새 시대론이 상식화. 의심하면 바보 취급",
        },
    },
    {
        "name": "본인 심리", "weight": 5,  # 사용자 4문항 합계 20 (6+6+3+5)
        "anchors": {
            1: "계좌 보기 두려움. 전량 손절 충동",
            2: "불안 우세. 반등이 탈출 기회로 보임",
            3: "평온. 계획대로 대응, 감정 동요 없음",
            4: "'지금이 기회' 확신. 매수 검토 중",
            5: "안 사면 뒤처질 FOMO. 풀베팅 충동",
        },
    },
]
USER_W = sum(q["weight"] for q in USER_QUESTIONS)  # 20

# ======================================================================
# AI 웹조사 5문항 — Claude API + web search로 자동 채점
# lagging=True 항목은 출판·공모 사이클 후행 지표 → 항복 진단에 사용
# 신용융자·반대매매는 금투협 크롤링 대신 뉴스 경유가 안정적 (검증된 방식)
# ======================================================================
AI_QUESTIONS = [
    {
        "name": "유튜브·커뮤니티 분위기", "weight": 5, "lagging": False,
        "guide": ("한국 주식 커뮤니티·유튜브 투자심리를 조사하라. "
                  "1=곡소리·탈출인증·'내가 사면 더 떨어진다' 비관 도배, 2=비관 우세, "
                  "3=혼재, 4=낙관 우세, 5=수익 인증 릴레이·구독자 폭증"),
    },
    {
        "name": "언론 헤드라인 톤", "weight": 4, "lagging": False,
        "guide": ("한국 증시 관련 최근 언론 헤드라인 톤을 조사하라. "
                  "1='폭락·패닉·강제청산' 일색, 2=부정 우세, 3=중립 혼재, "
                  "4=긍정 우세, 5='역대 최고·O만 간다' 일색"),
    },
    {
        "name": "신용융자·반대매매·예탁금", "weight": 5, "lagging": False,
        "guide": ("금융투자협회 발표 기준 최근 신용거래융자 잔고 추이, 미수금 대비 "
                  "반대매매 비중, 투자자예탁금 증감을 뉴스에서 조사하라. "
                  "1=반대매매 폭증(비중 5%↑)·예탁금 급감·강제청산 뉴스, 2=신용잔고 감소 전환, "
                  "3=보합, 4=신용잔고 증가 추세·예탁금 유입, 5=신용잔고 사상 최대 경신 중"),
    },
    {
        "name": "서점·미디어 콘텐츠", "weight": 3, "lagging": True,
        "guide": ("한국 서점 경제경영 베스트셀러 성격을 조사하라. "
                  "1=경제위기론·현금확보 책 도배, 2=투자 콘텐츠 침체, 3=평상시, "
                  "4=투자 입문·'주식하는 국민' 류 책 상위권, 5='주식 부자되기' 열풍"),
    },
    {
        "name": "공모주 시장", "weight": 3, "lagging": True,
        "guide": ("한국 IPO·공모주 청약 최근 경쟁률과 분위기를 조사하라. "
                  "1=상장 철회 속출·공모가 하단, 2=냉랭, 3=선별적 흥행, "
                  "4=대체로 흥행·상단 확정 릴레이, 5=따상 속출·수천 대 1 경쟁률"),
    },
]
AI_W = sum(q["weight"] for q in AI_QUESTIONS)  # 20

AUTO_WEIGHT_TOTAL = 60  # 정량 8종 합계 가중 (글로벌 3 + 국내 5)

REGIMES = [
    (20, "🔵 극단적 공포 (Extreme Fear)", "역사적 분할매수 검토 구간. 계획된 매수만, 물타기 금지"),
    (40, "🟦 공포 (Fear)", "관심종목 정비·트리거 설정 후 대기. 두-이벤트 확인 원칙 유지"),
    (60, "⚪ 중립 (Neutral)", "포지션 유지, 리밸런싱 적기"),
    (80, "🟠 탐욕 (Greed)", "신규 매수 자제, 이익 실현 계획 수립"),
    (101, "🔴 극단적 탐욕 (Extreme Greed)", "적극적 현금 확보, 분할 매도"),
]


def regime(temp):
    for th, name, action in REGIMES:
        if temp < th:
            return name, action
    return REGIMES[-1][1], REGIMES[-1][2]


# ======================================================================
# 정량 지표 자동 수집 (1=공포 ~ 5=탐욕)
# ======================================================================
def score_vix():
    v = float(yf.Ticker("^VIX").history(period="5d")["Close"].iloc[-1])
    s = 1 if v >= 30 else 2 if v >= 25 else 3 if v >= 17 else 4 if v >= 12 else 5
    return s, f"VIX {v:.1f}"


def score_cnn_fng():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    v = float(r.json()["fear_and_greed"]["score"])
    s = 1 if v < 25 else 2 if v < 45 else 3 if v < 55 else 4 if v < 75 else 5
    return s, f"CNN F&G {v:.0f}"


def score_kospi_drawdown():
    h = yf.Ticker("^KS11").history(period="1y")["Close"]
    close, high = float(h.iloc[-1]), float(h.max())
    dd = close / high - 1.0
    s = 1 if dd <= -0.20 else 2 if dd <= -0.10 else 3 if dd <= -0.05 else 4 if dd <= -0.02 else 5
    return s, f"KOSPI {close:,.0f} (고점比 {dd*100:.1f}%)"


def score_usdkrw():
    h = yf.Ticker("KRW=X").history(period="1y")["Close"]
    close = float(h.iloc[-1])
    pct = float((h <= close).mean())
    s = 1 if pct >= 0.80 else 2 if pct >= 0.60 else 3 if pct >= 0.40 else 4 if pct >= 0.20 else 5
    if close >= 1450:  # v4: 절대 레벨 캡 — 백분위 착시 방지
        s = min(s, 2)
    return s, f"원달러 {close:,.0f} (1Y 백분위 {pct*100:.0f}%)"


def score_hy_spread():
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY 미설정")
    url = ("https://api.stlouisfed.org/fred/series/observations"
           f"?series_id=BAMLH0A0HYM2&api_key={FRED_KEY}&file_type=json"
           "&sort_order=desc&limit=260")
    obs = requests.get(url, timeout=15).json()["observations"]
    vals = [float(o["value"]) for o in obs if o["value"] != "."]
    cur = vals[0]
    pct = sum(1 for v in vals if v <= cur) / len(vals)
    s = 1 if pct >= 0.80 else 2 if pct >= 0.60 else 3 if pct >= 0.40 else 4 if pct >= 0.20 else 5
    return s, f"HY OAS {cur:.2f}%p (1Y 백분위 {pct*100:.0f}%)"


# ---------------------- 코스피 특화 지표 (v3 추가) ----------------------
def score_kospi_disparity():
    """120일선 이격도 — 한국 시장의 고전적 과열/침체 게이지.
    0.85 이하는 역사적으로 IMF·금융위기·코로나급 투매 구간."""
    h = yf.Ticker("^KS11").history(period="2y")["Close"]
    ma120 = float(h.rolling(120).mean().iloc[-1])
    disp = float(h.iloc[-1]) / ma120
    s = 1 if disp <= 0.85 else 2 if disp <= 0.93 else 3 if disp <= 1.05 else 4 if disp <= 1.12 else 5
    return s, f"120일 이격도 {disp*100:.1f}%"


META = {}  # 실행 중 부가 데이터 (피크아웃 판정 등)


def score_kospi_realized_vol():
    """20일 실현변동성 (연율화, VKOSPI 대용).
    VKOSPI는 KRX 전용 데이터라 크롤링이 불안정 → 실현변동성으로 근사.
    45% 이상은 2020.3, 2026.6(VKOSPI 95) 같은 패닉 국면에서만 출현.
    v4: 60일 피크 대비 -20% 하락 여부(피크아웃)를 META에 저장 — 극단 공포
    매수 게이트로 사용 (2008년 극단 공포 5개월 지속 교훈)."""
    import math
    h = yf.Ticker("^KS11").history(period="6mo")["Close"]
    ret = h.pct_change().dropna()
    rv_series = ret.rolling(20).std() * math.sqrt(252) * 100
    rv = float(rv_series.iloc[-1])
    peak60 = float(rv_series.tail(60).max())
    META["rv_now"], META["rv_peak60"] = rv, peak60
    META["rv_peakout"] = bool(peak60 > 0 and rv <= peak60 * 0.8)
    s = 1 if rv >= 45 else 2 if rv >= 30 else 3 if rv >= 20 else 4 if rv >= 15 else 5
    return s, f"KOSPI 실현변동성 {rv:.0f}% (연율)"


def score_lev_inv_ratio():
    """KODEX 레버리지(122630) vs 200선물인버스2X(252670) 거래대금 비율.
    개인의 방향성 베팅 게이지 — 레버리지 쏠림=탐욕, 인버스 쏠림=공포.
    1년 분포 내 백분위로 채점해 절대 수준 변화에 강건."""
    lev = yf.Ticker("122630.KS").history(period="1y")
    inv = yf.Ticker("252670.KS").history(period="1y")
    ratio = (lev["Volume"] * lev["Close"]) / (inv["Volume"] * inv["Close"])
    ratio = ratio.dropna()
    cur = float(ratio.tail(5).mean())          # 최근 5일 평균으로 노이즈 완화
    pct = float((ratio <= cur).mean())          # 높을수록 레버리지 쏠림=탐욕
    s = 1 if pct <= 0.20 else 2 if pct <= 0.40 else 3 if pct <= 0.60 else 4 if pct <= 0.80 else 5
    return s, f"레버리지/인버스 거래대금 {cur:.2f}배 (1Y 백분위 {pct*100:.0f}%)"


AUTO_INDICATORS = [
    # (표시명, 함수, 지역)
    ("VIX", score_vix, "글로벌"),
    ("CNN Fear&Greed", score_cnn_fng, "글로벌"),
    ("하이일드 스프레드", score_hy_spread, "글로벌"),
    ("KOSPI 고점대비", score_kospi_drawdown, "국내"),
    ("KOSPI 120일 이격도", score_kospi_disparity, "국내"),
    ("KOSPI 실현변동성", score_kospi_realized_vol, "국내"),
    ("레버리지/인버스 비율", score_lev_inv_ratio, "국내"),
    ("원달러 환율", score_usdkrw, "국내"),
]


CORE_KR = ("KOSPI 고점대비", "KOSPI 120일 이격도", "KOSPI 실현변동성")  # 급락 코어


def postprocess_auto(results, failed, meta=None):
    """채점 결과 후처리 (v4): 동학개미 보정 + 소계 계산. 백테스트에서도 호출 가능."""
    scores = {r["name"]: r["score"] for r in results}
    core = [scores[n] for n in CORE_KR if n in scores]
    core_avg = sum(core) / len(core) if core else None
    donghak = False
    # 동학개미 보정 (2020.3 교훈): 급락 코어와 참여 열기가 역행하면 열기 지표 캡 3
    if core_avg is not None and core_avg <= 1.5 and scores.get("레버리지/인버스 비율", 0) >= 4:
        for r in results:
            if r["name"] == "레버리지/인버스 비율":
                r["score"] = 3
                r["detail"] += " ⇒ 저가매수 군중 보정(캡3)"
        scores["레버리지/인버스 비율"] = 3
        donghak = True
    avg = sum(r["score"] for r in results) / len(results)
    out = {"items": results, "avg": avg, "weight": AUTO_WEIGHT_TOTAL, "failed": failed,
           "scores": scores, "core_kr": core_avg, "donghak": donghak,
           "meta": dict(meta) if meta else dict(META)}
    for region in ("글로벌", "국내"):
        sub = [r["score"] for r in results if r["region"] == region]
        out[region] = sum(sub) / len(sub) if sub else None
    return out


def collect_auto():
    results, failed = [], []
    for name, fn, region in AUTO_INDICATORS:
        try:
            s, detail = fn()
            results.append({"name": name, "score": s, "detail": detail, "region": region})
        except Exception as e:
            failed.append(f"{name} ({type(e).__name__})")
    if not results:
        raise RuntimeError("정량 지표 전체 수집 실패")
    return postprocess_auto(results, failed)


# ======================================================================
# AI 웹조사 채점 (Claude API + web search)
# ======================================================================
def _ai_query_gemini(prompt):
    """Gemini API (무료 티어) + Google 검색 그라운딩."""
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-2.5-flash:generateContent?key={GEMINI_KEY}")
    r = requests.post(url, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
    }, timeout=180)
    data = r.json()
    if "error" in data:  # 키 오류·쿼터 초과 등을 명시적으로 노출
        raise RuntimeError(f"Gemini API 오류: {data['error'].get('message', data['error'])}")
    parts = data["candidates"][0]["content"]["parts"]
    return "\n".join(p.get("text", "") for p in parts if "text" in p)


def _ai_query_claude(prompt):
    """Claude API + 웹서치 (ANTHROPIC_API_KEY 보유 시 폴백)."""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": "claude-sonnet-4-6",
              "max_tokens": 2000,
              "messages": [{"role": "user", "content": prompt}],
              "tools": [{"type": "web_search_20250305", "name": "web_search",
                         "max_uses": 8}]},
        timeout=180,
    )
    data = r.json()
    return "\n".join(b.get("text", "") for b in data.get("content", [])
                     if b.get("type") == "text")


def collect_ai():
    """5개 체감 항목을 AI가 웹검색으로 조사·채점 (Gemini 무료 우선, Claude 폴백).
    실패 시 None → 해당 항목이 사용자 질문으로 전환."""
    if not (GEMINI_KEY or ANTHROPIC_KEY):
        return None
    guides = "\n".join(f'- "{q["name"]}": {q["guide"]}' for q in AI_QUESTIONS)
    prompt = (
        "당신은 한국 주식시장 심리 분석가다. 오늘 기준으로 아래 5개 항목을 웹 검색으로 조사하고 "
        "각각 1~5점(1=극단적 공포, 5=극단적 탐욕)으로 채점하라.\n\n"
        f"{guides}\n\n"
        "규칙: 반드시 최근 1~2주 내 한국 뉴스·데이터를 근거로 하라. 각 항목 근거는 한 문장.\n"
        '응답은 JSON 배열만 출력하라. 마크다운 백틱·서문 금지. 형식: '
        '[{"name": "항목명", "score": 3, "rationale": "근거 한 줄"}]'
    )
    provider = "Gemini" if GEMINI_KEY else "Claude"
    try:
        text = _ai_query_gemini(prompt) if GEMINI_KEY else _ai_query_claude(prompt)
        m = re.search(r"\[.*\]", text.replace("```json", "").replace("```", ""), re.S)
        if not m:
            raise ValueError(f"응답에서 JSON 배열 미발견: {text[:200]!r}")
        parsed = json.loads(m.group(0))
        items = []
        for idx, q in enumerate(AI_QUESTIONS):
            # 이름 정확 일치 → 부분 일치 → 순서 기반 순으로 매칭 (모델별 표기 편차 흡수)
            p = next((x for x in parsed if x.get("name") == q["name"]), None)
            if p is None:
                key = q["name"][:4]
                p = next((x for x in parsed if key in str(x.get("name", ""))), None)
            if p is None and idx < len(parsed):
                p = parsed[idx]
            score = int(p["score"]) if p and "score" in p else None
            if score is None or not (1 <= score <= 5):
                raise ValueError(f"항목 채점 누락: {q['name']} / 응답: {p}")
            items.append({"name": q["name"], "score": score,
                          "weight": q["weight"], "lagging": q["lagging"],
                          "rationale": str(p.get("rationale", ""))[:120]})
        avg = sum(i["score"] * i["weight"] for i in items) / AI_W
        print(f"[AI 조사 성공] {provider}, 소계 {avg:.2f}/5")
        return {"items": items, "avg": avg, "weight": AI_W}
    except Exception as e:
        print(f"[AI 조사 실패 → 9문항 폴백] {provider}: {type(e).__name__}: {e}")
        return None


# ======================================================================
# 급락 유형 분류기 (v4) — 백테스트에서 역발상 성패를 가른 변수
#   이벤트 쇼크형(2020.3, 2026.3): V자 우위 / 청산형·혼합형(2008): 계단식
#   침식형(2000~2002): 심리 역발상 무효
# ======================================================================
def classify_downturn(auto, ai):
    s = auto.get("scores", {})
    dd, rv = s.get("KOSPI 고점대비"), s.get("KOSPI 실현변동성")
    if dd is None or rv is None or dd >= 3:
        return None  # 하락 국면 아님 또는 판별 불가
    if dd == 1 and rv >= 3:
        return ("🐌 *침식형 약세장 (2000~2002년형)*: 깊은 낙폭 + 낮은 변동성 = 패닉 없이 "
                "흘러내리는 구조적 약세. *심리 역발상 무효* — 밸류에이션·추세 기준으로 전환. "
                "닷컴 백테스트: 온도 26 '공포'에서 매수 시 1년+ 추가 신저가")
    credit = None
    if ai:
        for i in ai["items"]:
            if "신용" in i["name"]:
                credit = i["score"]
    if rv == 1 and credit == 1:
        return ("⚠️ *쇼크+레버리지 청산 혼합형 (2008 vs 2020 분기점)*: 반대매매 도미노 진행 중. "
                "V자(2020)와 계단식(2008)의 분기 조건은 정책 대응 속도와 펀더멘털 손상 여부. "
                "신용잔고 정상화·반대매매 비중 5%↓ 확인 전에는 계단식 리스크를 우선 가정")
    if rv == 1:
        return ("⚡ *이벤트 쇼크형 (2020.3 / 2026.3형)*: 급성 변동성 + 신용 경로 미붕괴. "
                "펀더멘털 무손상 확인 시 V자 반등 확률 우위 (2026.3 백테스트: 3개월 +84%)")
    if credit == 1:
        return ("🪜 *레버리지 청산형*: 반대매매 소진까지 계단식 하락 위험. "
                "신용 지표 정상화가 진입의 선행 조건")
    return None


# ======================================================================
# 항복(capitulation) 진단 — 2026-07-14 세션에서 도출한 로직
# ======================================================================
def capitulation_check(ai, user_scores, temp):
    """유통시장 심리는 공포인데 후행지표(서점/공모주)가 뜨거우면 '항복 미완' 판정."""
    if temp >= 40 or not ai:
        return None
    lagging = [i for i in ai["items"] if i["lagging"]]
    leading = [i["score"] for i in ai["items"] if not i["lagging"]]
    if user_scores:
        leading += user_scores[:2]  # 주변 대화·지인 진입
    lagging_hot = any(i["score"] >= 4 for i in lagging)
    leading_cold = leading and (sum(leading) / len(leading)) <= 2.0
    if lagging_hot and leading_cold:
        hot_names = ", ".join(i["name"] for i in lagging if i["score"] >= 4)
        return (f"⚠️ *항복 미완*: 유통시장 심리는 붕괴했으나 후행지표({hot_names})가 "
                "아직 뜨거움. 진짜 바닥(capitulation)에서는 공모주 청약까지 냉각됨. "
                "체크리스트: 반대매매 비중 5%↓ 안착 / 공모주 경쟁률 급랭·상장 철회 / "
                "신용잔고 정상화 / 개인 순매도 클라이맥스 후 거래량 반등")
    if not lagging_hot and leading_cold:
        return "✅ 항복 신호 진행: 후행지표까지 냉각. 극단 공포 구간 진입 여부 주시"
    return None


# ======================================================================
# 메시지 조립
# ======================================================================
def build_interim(auto, ai):
    lines = ["📊 *1) 정량 지표 자동 채점* (1=공포~5=탐욕)"]
    for region in ("글로벌", "국내"):
        sub = [r for r in auto["items"] if r["region"] == region]
        if not sub:
            continue
        lines.append(f"  [{region}]")
        for r in sub:
            lines.append(f"  · {r['name']}: *{r['score']}점* — {r['detail']}")
    if auto["failed"]:
        lines.append(f"  ⚠️ 수집 실패(제외): {', '.join(auto['failed'])}")
    gl, kr = auto.get("글로벌"), auto.get("국내")
    if gl is not None and kr is not None:
        lines.append(f"  → 글로벌 {gl:.2f} · 국내 {kr:.2f} · 종합 {auto['avg']:.2f}/5")
        div = kr - gl
        if div <= -1.0:
            lines.append(f"  🚨 *국내-글로벌 괴리 {div:+.2f}*: 한국 국지적 패닉. "
                         "글로벌 리스크오프가 아닌 국내 수급(레버리지 청산·ADR 등) 요인 점검 필요")
        elif div >= 1.0:
            lines.append(f"  🚨 *국내-글로벌 괴리 {div:+.2f}*: 한국 국지적 과열. "
                         "글로벌 대비 앞서간 만큼 되돌림 취약")
    else:
        lines.append(f"  → 정량 소계 {auto['avg']:.2f}/5")
    if ai:
        lines.append("\n🔍 *2) AI 웹조사 채점*")
        for i in ai["items"]:
            tag = " (후행)" if i["lagging"] else ""
            lines.append(f"  · {i['name']}{tag}: *{i['score']}점* — {i['rationale']}")
        lines.append(f"  → AI조사 소계 {ai['avg']:.2f}/5")
    else:
        lines.append(f"\n⚠️ AI 조사 불가 — 아래 질문 {len(USER_QUESTIONS) + len(AI_QUESTIONS)}개에 모두 답해주세요")
    return "\n".join(lines)


def build_questions(include_ai_fallback):
    qs = list(USER_QUESTIONS) + ([{"name": q["name"], "weight": q["weight"],
                                   "anchors": None, "guide": q["guide"]}
                                  for q in AI_QUESTIONS] if include_ai_fallback else [])
    n = len(qs)
    lines = [f"📝 *체감 설문 {n}문항* — 각 1~5점. 애매하면 `1or2` 처럼 범위 허용",
             "예: `2 1or2 1or2 3`" if n == 4 else "예: `2 1or2 2 3 1 1 1 4 4`"]
    for idx, q in enumerate(qs, 1):
        lines.append(f"\n*{idx}. {q['name']}*")
        if q.get("anchors"):
            for s in range(1, 6):
                lines.append(f"  {s}점: {q['anchors'][s]}")
        else:
            lines.append(f"  기준: {q['guide']}")
    return "\n".join(lines)


def parse_answers(text, n):
    """'2 1or2 1~2 3' → [2.0, 1.5, 1.5, 3.0]. 형식 불일치 시 None."""
    tokens = re.findall(r"[1-5]\s*(?:or|~|-)\s*[1-5]|[1-5]", text, re.I)
    if len(tokens) != n:
        return None
    vals = []
    for t in tokens:
        nums = [int(x) for x in re.findall(r"[1-5]", t)]
        vals.append(sum(nums) / len(nums))
    return vals


def build_final(auto, ai, answers):
    answers = list(answers)
    notes = []
    # 동학개미 보정 (v4): 급락 코어 극단에서 참여 열기 문항(주변 대화·지인 진입) 캡 3
    core = auto.get("core_kr")
    user_clipped = False
    if core is not None and core <= 1.5:
        for idx in (0, 1):
            if idx < len(answers) and answers[idx] > 3:
                answers[idx] = 3
                user_clipped = True
    if user_clipped or auto.get("donghak"):
        notes.append("👥 *저가매수 군중 감지(동학개미형)*: 급락 코어와 참여 열기가 역행 → "
                     "참여 항목을 3점으로 캡해 온도 왜곡 제거 (2020.3 백테스트: 미보정 시 "
                     "10년 최고 매수 기회를 '공포 22'로 오판). 군중 유입 자체는 바닥도 상투도 "
                     "아님 — 아래 유형 분류가 판단 기준")
    # 사용자 소계
    if ai:
        user_avg = sum(a * q["weight"] for a, q in zip(answers, USER_QUESTIONS)) / USER_W
        sub_avg = (ai["avg"] * AI_W + user_avg * USER_W) / (AI_W + USER_W)
    else:
        all_q = USER_QUESTIONS + AI_QUESTIONS
        total_w = sum(q["weight"] for q in all_q)
        sub_avg = sum(a * q["weight"] for a, q in zip(answers, all_q)) / total_w
    total_avg = (auto["avg"] * AUTO_WEIGHT_TOTAL + sub_avg * (AI_W + USER_W)) / 100
    temp = (total_avg - 1) / 4 * 100
    name, action = regime(temp)
    gap = sub_avg - auto["avg"]
    if gap > 0.5:
        if core is not None and core <= 1.5:
            gap_msg = "체감 > 데이터: 급락 코어 대비 참여 열기 잔존 — 후기 국면 아님, 동학개미형 항목 참조"
        else:
            gap_msg = "체감 > 데이터: 대중 참여가 데이터를 앞서는 후기 국면 신호"
    elif gap < -0.5:
        gap_msg = "데이터 > 체감: 시장이 먼저 움직임. 대중 미참여 국면"
    else:
        gap_msg = "체감·데이터 정렬 — 국면 판정 신뢰도 높음"
    msg = (
        f"🌡 *시장 심리 온도: {temp:.0f} / 100*\n"
        f"판정: {name}\n\n"
        f"정량(60): {auto['avg']:.2f}/5 · 체감(40): {sub_avg:.2f}/5\n"
        + (f"국내 {auto['국내']:.2f} vs 글로벌 {auto['글로벌']:.2f} "
           f"(괴리 {auto['국내']-auto['글로벌']:+.2f})\n"
           if auto.get('국내') is not None and auto.get('글로벌') is not None else "")
        + f"체감-정량 괴리: {gap:+.2f} — {gap_msg}\n\n"
        f"📌 {action}"
    )
    dt = classify_downturn(auto, ai)
    if dt:
        notes.append(dt)
    # 변동성 피크아웃 게이트 (v4, 2008 교훈): 극단 공포에서도 피크아웃 전엔 1차 분할만
    if temp < 20:
        m = auto.get("meta", {})
        po, now, peak = m.get("rv_peakout"), m.get("rv_now"), m.get("rv_peak60")
        if po is True:
            notes.append(f"📉 변동성 피크아웃 확인 (RV {now:.0f}% ← 피크 {peak:.0f}%) → "
                         "분할매수 2차 진행 조건 충족")
        elif po is False:
            notes.append(f"⏳ *변동성 피크아웃 미확인* (RV {now:.0f}% vs 60일 피크 {peak:.0f}%) → "
                         "극단 공포라도 1차 분할만. 2008년: 극단 공포가 5개월 지속, "
                         "미국 저점은 첫 극단 공포 신호 후 -30% 아래")
    cap = capitulation_check(ai, answers, temp)
    if cap:
        notes.append(cap)
    for n in notes:
        msg += f"\n\n{n}"
    msg += "\n\n_※ 심리 지표는 타이밍이 아닌 위험관리 도구. 극단 구간에서만 역발상 신호로 유효. 투자 자문 아님._"
    return msg


# ======================================================================
# 텔레그램 I/O
# ======================================================================
def send(chat_id, text):
    try:
        r = requests.post(f"{API}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                          timeout=15)
        ok = bool(r.json().get("ok"))
        if not ok:
            print(f"[send 실패] {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[send 예외] {e}")
        return False


def save_state(auto, ai):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"auto": auto, "ai": ai}, f, ensure_ascii=False)


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def drain_updates():
    """대기 시작 전 밀린 메시지를 소진해 오래된 텍스트가 답장으로 오인되는 것을 방지."""
    offset = _read_json(OFFSET_FILE, {}).get("offset", 0)
    try:
        r = requests.get(f"{API}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=30).json()
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
    except Exception:
        pass
    _write_json(OFFSET_FILE, {"offset": offset})
    return offset


def wait_for_answers(chat_id, auto, ai, minutes):
    """진단 발송 후 같은 실행 안에서 답장을 대기 (long-poll). 수신 시 최종 진단 발송.
    상시 리스너 없이 단일 Actions 실행으로 문답을 완결하는 v6 핵심 메커니즘."""
    n = 4 if ai else len(USER_QUESTIONS) + len(AI_QUESTIONS)
    offset = _read_json(OFFSET_FILE, {}).get("offset", 0)
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"offset": offset,
                                     "timeout": min(50, max(1, int(deadline - time.time())))},
                             timeout=60).json()
        except Exception:
            time.sleep(5)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            _write_json(OFFSET_FILE, {"offset": offset})
            msg = upd.get("message") or {}
            if CHAT_ID and str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
                continue
            answers = parse_answers(msg.get("text", "") or "", n)
            if answers:
                import datetime
                _write_json(ANSWERS_FILE,
                            {"date": datetime.date.today().isoformat(), "answers": answers})
                send(chat_id, build_final(auto, ai, answers))
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
                return True
    return False


def run_diagnosis(chat_id, header=None):
    """전체 진단 1회: 정량 + AI조사 발송 → 설문 전송 → 잠정 진단 → 답장 대기."""
    if header:
        send(chat_id, header)
    if not send(chat_id, "⏳ 정량 지표 수집 + AI 웹조사 중... (최대 2~3분)"):
        raise RuntimeError("텔레그램 발송 실패 — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 확인 "
                           "(봇에게 /start를 먼저 보냈는지도 확인). 헛대기 방지를 위해 즉시 중단")
    drain_updates()
    auto = collect_auto()
    ai = collect_ai()
    save_state(auto, ai)
    send(chat_id, build_interim(auto, ai))
    send(chat_id, build_questions(include_ai_fallback=(ai is None)))
    prev = _read_json(ANSWERS_FILE, None)
    n_needed = 4 if ai else len(USER_QUESTIONS) + len(AI_QUESTIONS)
    if prev and len(prev.get("answers", [])) == n_needed:
        send(chat_id,
             f"📎 *잠정 전체 진단* — 지난 응답({prev.get('date', '?')}) 재사용\n\n"
             + build_final(auto, ai, prev["answers"]))
    wait_min = int(os.environ.get("ANSWER_WAIT_MIN", "15"))
    if wait_min > 0:
        send(chat_id, f"⌛ 지금부터 *{wait_min}분간* 답장을 대기합니다. "
                      f"숫자 {n_needed}개 회신 시 즉시 최종 진단 발송 "
                      "(놓치면 다음 실행 때 갱신)")
        got = wait_for_answers(chat_id, auto, ai, wait_min)
        if not got:
            send(chat_id, "⌛ 대기 종료 — 잠정 진단으로 마감합니다. "
                          "체감 갱신은 다음 정기/긴급 실행 때 답장해 주세요")


CRASH_THRESHOLD = -0.10  # v7: 급락 판정 임계 (전일 종가 대비 -10%, 서킷브레이커급)


def crash_check(chat_id):
    """KOSPI 급락 자동 감지: 전일 종가 대비 -10% 이하 → 하루 1회 긴급 진단.
    -10%는 서킷브레이커급 이벤트 (2026.3.4 이란전쟁 -12.06% 등). 장 마감 후 1회만 검토."""
    try:
        h = yf.Ticker("^KS11").history(period="5d")["Close"]
        if len(h) < 2:
            return False
        chg = float(h.iloc[-1] / h.iloc[-2] - 1)
    except Exception:
        return False
    if chg > CRASH_THRESHOLD:
        return False
    import datetime
    today = datetime.date.today().isoformat()
    if _read_json(CRASH_FILE, {}).get("date") == today:
        return False  # 쿨다운: 하루 1회
    _write_json(CRASH_FILE, {"date": today, "chg": chg})
    run_diagnosis(chat_id, header=f"🚨 *급락 자동 감지*: KOSPI 전일比 {chg*100:.1f}% "
                                  f"(임계 {CRASH_THRESHOLD*100:.0f}%) → 긴급 진단 실행")
    return True


def handle_message(chat_id, text):
    text = text.strip()
    if text in ("심리", "/sentiment", "/심리", "시장심리", "긴급", "진단"):
        run_diagnosis(chat_id)
        return
    state = load_state()
    if state:
        n = 4 if state["ai"] else len(USER_QUESTIONS) + len(AI_QUESTIONS)
        answers = parse_answers(text, n)
        if answers:
            import datetime
            _write_json(ANSWERS_FILE,
                        {"date": datetime.date.today().isoformat(), "answers": answers})
            send(chat_id, build_final(state["auto"], state["ai"], answers))
            os.remove(STATE_FILE)
            return
        # 숫자가 섞였는데 개수가 안 맞으면 안내
        if re.search(r"[1-5]", text):
            send(chat_id, f"답변 {n}개가 필요합니다. 예: " +
                 ("`2 1or2 1or2 3`" if n == 4 else "`2 1or2 2 3 1 1 1 4 4`"))


def poll_loop():
    offset = 0
    print("sentiment bot v3 polling...")
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"offset": offset, "timeout": 50}, timeout=60).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")
                if not text:
                    continue
                if CHAT_ID and chat_id != str(CHAT_ID):
                    continue
                try:
                    handle_message(chat_id, text)
                except Exception as e:
                    send(chat_id, f"⚠️ 오류: {type(e).__name__}: {e}")
        except Exception as e:
            print("poll error:", e)
            time.sleep(10)


def process_pending(chat_id):
    """밀린 텔레그램 메시지 일괄 처리 (v7) — 일일 장 마감 실행에 무임승차.
    상시 리스너 없이 키워드 트리거·응답 갱신을 지원 (추가 Actions 사용량 0).
      · "심리"/"긴급"/"진단" → 전체 진단 실행 (하루 중 보낸 요청을 15:40에 수거)
      · 숫자 답변 → 미완 설문이 있으면 최종 진단 발송, 없으면 last_answers 갱신
    반환: 진단을 실행했으면 True (급락 체크 중복 발송 방지용)."""
    offset = _read_json(OFFSET_FILE, {}).get("offset", 0)
    try:
        r = requests.get(f"{API}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=30).json()
    except Exception:
        return False
    want_diag = False
    pending_answers = None
    for upd in r.get("result", []):
        offset = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if CHAT_ID and str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
            continue
        text = (msg.get("text") or "").strip()
        if text in ("심리", "/sentiment", "/심리", "시장심리", "긴급", "진단"):
            want_diag = True
            continue
        state = load_state()
        n = 4 if (not state or state.get("ai")) else len(USER_QUESTIONS) + len(AI_QUESTIONS)
        ans = parse_answers(text, n)
        if ans:
            pending_answers = ans  # 여러 개면 마지막 것 사용
    _write_json(OFFSET_FILE, {"offset": offset})
    if pending_answers:
        import datetime
        _write_json(ANSWERS_FILE,
                    {"date": datetime.date.today().isoformat(), "answers": pending_answers})
        state = load_state()
        if state:
            send(chat_id, build_final(state["auto"], state["ai"], pending_answers))
            os.remove(STATE_FILE)
        else:
            send(chat_id, "✅ 체감 응답 갱신 완료 — 다음 진단부터 반영됩니다")
    if want_diag:
        run_diagnosis(chat_id, header="📬 *요청 수거*: 오늘 보내신 진단 요청을 실행합니다")
        return True
    return False


def daily():
    """--daily: 평일 장 마감(15:40 KST) 1회 실행 — 메시지 수거 + 급락 체크."""
    ran = process_pending(CHAT_ID)
    if not ran:
        crash_check(CHAT_ID)


def check_once():
    """--check: 리스너 1회 (Actions 15분 주기용) — 신규 메시지 처리 + 급락 감지."""
    offset = _read_json(OFFSET_FILE, {}).get("offset", 0)
    r = requests.get(f"{API}/getUpdates",
                     params={"offset": offset, "timeout": 0}, timeout=30).json()
    for upd in r.get("result", []):
        offset = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")
        if not text:
            continue
        if CHAT_ID and chat_id != str(CHAT_ID):
            continue
        try:
            handle_message(chat_id, text)
        except Exception as e:
            send(chat_id, f"⚠️ 오류: {type(e).__name__}: {e}")
    _write_json(OFFSET_FILE, {"offset": offset})
    if CHAT_ID:
        crash_check(CHAT_ID)


def weekly():
    """--weekly: 주간 정기 진단 (화요일 08:30 KST cron)."""
    run_diagnosis(CHAT_ID, header="📅 *주간 정기 심리 진단* (화요일 08:30 KST)")


def auto_once():
    """--auto: 정량 + AI조사만 1회 발송 (GitHub Actions 주간 cron 스텝용)"""
    auto = collect_auto()
    ai = collect_ai()
    temp_partial = ((auto["avg"] * AUTO_WEIGHT_TOTAL + (ai["avg"] * AI_W if ai else 0))
                    / (AUTO_WEIGHT_TOTAL + (AI_W if ai else 0)) - 1) / 4 * 100
    name, _ = regime(temp_partial)
    send(CHAT_ID, build_interim(auto, ai) +
         f"\n\n부분 판정(사용자 체감 제외): {name} (온도 {temp_partial:.0f})\n"
         "전체 진단은 `심리` 전송")


if __name__ == "__main__":
    if not BOT_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN 환경변수를 설정하세요.")
    if "--weekly" in sys.argv:
        weekly()
    elif "--daily" in sys.argv:
        daily()                # 일일 장 마감: 메시지 수거 + 급락 체크 (조건 미충족 시 무발송 종료)
    elif "--crash" in sys.argv:
        crash_check(CHAT_ID)   # 급락 체크만
    elif "--check" in sys.argv:
        check_once()
    elif "--auto" in sys.argv:
        auto_once()
    else:
        poll_loop()
