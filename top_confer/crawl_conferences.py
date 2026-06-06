# -*- coding: utf-8 -*-
"""
탑 컨퍼런스 일정 크롤러 (mlciv.com / ai-deadlines)
==================================================

https://mlciv.com/ai-deadlines/ 에 올라온 AI 탑 컨퍼런스들의 일정(예정 + past events)을
크롤링해서 JSON 파일로 저장한다.

데이터 출처
-----------
이 사이트는 페이지에서 사용하는 원본 데이터를 정식 JSON 엔드포인트로 그대로 공개한다.
    - 전체:   https://mlciv.com/ai-deadlines/conferences.json
HTML을 한 줄씩 파싱하는 것보다 이 엔드포인트가 훨씬 안정적이라 1순위로 사용하고,
만약 엔드포인트가 죽었을 경우를 대비해 HTML 페이지를 직접 파싱하는 폴백(fallback)도 둔다.

추출/정리하는 항목
------------------
    - conference        : 컨퍼런스 약칭 (예: CVPR, NeurIPS)
    - full_name         : 컨퍼런스 정식 명칭
    - year              : 개최 연도
    - publisher         : 기관(출판사)명  ※ 원본 데이터에 없어 큐레이션 매핑으로 채움
    - conference_dates  : 컨퍼런스가 열리는 날짜 (원문/시작/종료)
    - location          : 개최 장소
    - subjects          : 분야 (코드 -> 풀네임으로 변환)
    - paper_deadline    : 논문 마감일
    - abstract_deadline : 초록 마감일
    - timezone, h_index, website, note

사용법
------
    python crawl_conferences.py
    python crawl_conferences.py --out my_output.json --raw      # 원본 그대로도 함께 저장
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any

import requests

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------

BASE = "https://mlciv.com/ai-deadlines"
JSON_URL = f"{BASE}/conferences.json"          # 1순위: 정식 데이터 엔드포인트
HTML_URL = f"{BASE}/?sub=ML,CV,CG,NLP,RO,SP,DM,AP,KR,HCI,EDU"  # 폴백: HTML 페이지

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# 분야 코드 -> 사람이 읽는 이름 (ai-deadlines 표준 분류)
SUBJECT_NAMES = {
    "ML": "Machine Learning",
    "CV": "Computer Vision",
    "CG": "Computer Graphics",
    "NLP": "Natural Language Processing",
    "RO": "Robotics",
    "SP": "Signal Processing",
    "DM": "Data Mining",
    "AP": "Applications",
    "KR": "Knowledge Representation & Reasoning",
    "HCI": "Human-Computer Interaction",
    "EDU": "Education / Learning Analytics",
}

# 기관(출판사) 매핑.
#   - 원본 데이터에는 출판사/주관기관 정보가 없다.
#   - 컨퍼런스 약칭(title)을 기준으로 잘 알려진 탑컨퍼런스의 주관/출판 기관을 큐레이션했다.
#   - 매핑에 없으면 full_name 에서 'ACM'/'IEEE' 등을 추론하고, 그래도 모르면 None.
PUBLISHER_BY_TITLE = {
    # ---- IEEE (CVF 공동 포함) ----
    "CVPR": "IEEE / CVF", "ICCV": "IEEE / CVF", "WACV": "IEEE / CVF",
    "3DV": "IEEE", "ICASSP": "IEEE", "ICRA": "IEEE", "IROS": "IEEE / RSJ",
    "ICDM": "IEEE", "ICDE": "IEEE", "ICDE-1": "IEEE", "ICDE-2": "IEEE",
    "ICMLA": "IEEE", "ICMLCN": "IEEE",
    # ---- ACM ----
    "CHI": "ACM", "CSCW": "ACM", "KDD": "ACM", "KDD-1": "ACM", "KDD-2": "ACM",
    "SIGIR": "ACM", "SIGIR-AP": "ACM",
    "SIGMOD": "ACM", "SIGMOD-1": "ACM", "SIGMOD-2": "ACM",
    "SIGMOD-3": "ACM", "SIGMOD-4": "ACM",
    "SIGGRAPH": "ACM", "SIGGRAPH Asia": "ACM", "MM": "ACM",
    "CIKM": "ACM", "WSDM": "ACM", "RecSys": "ACM", "WWW": "ACM",
    "UMAP": "ACM", "LAK": "ACM", "Learing at Scale": "ACM", "AAMAS": "ACM / IFAAMAS",
    # ---- ACL (Association for Computational Linguistics) ----
    "ACL": "ACL", "EMNLP": "ACL", "NAACL": "ACL", "EACL": "ACL",
    "AACL": "ACL", "AACL-IJCNLP": "ACL", "IJCNLP-AACL": "ACL",
    "COLING": "ICCL / ACL", "CoNLL": "ACL / SIGNLL", "SIGDIAL": "ACL / ISCA",
    "INLG": "ACL / SIGGEN", "STARSEM": "ACL / SIGLEX", "BEA": "ACL",
    "ARR": "ACL", "UncertaiNLP": "ACL", "IWSDS": "ACL",
    # ---- ELRA ----
    "LREC": "ELRA", "LREC-COLING": "ELRA / ICCL",
    # ---- AAAI / IJCAI ----
    "AAAI": "AAAI", "IJCAI": "IJCAI", "IJCAI-ECAI": "IJCAI / EurAI",
    # ---- Springer (LNCS 등) ----
    "ECCV": "Springer", "ECML PKDD": "Springer", "ECML-PKDD": "Springer",
    "ECML/PKDD": "Springer", "ECIR": "Springer",
    # ---- PMLR (Proceedings of Machine Learning Research) ----
    "ICML": "PMLR", "AISTATS": "PMLR", "ACML": "PMLR", "UAI": "PMLR",
    # ---- 자체/재단(OpenReview, Curran 등) ----
    "NeurIPS": "NeurIPS Foundation",
    "NeurIPS [Dataset and Benchmarks Track]": "NeurIPS Foundation",
    "ICLR": "ICLR (OpenReview)", "COLM": "COLM", "LoG": "LoG (OpenReview)",
    "MLSYS": "MLSys Foundation",
    # ---- 기타 학회/재단 ----
    "RSS": "RSS Foundation", "InterSpeech": "ISCA", "BMVC": "BMVA",
    "ECIR ": "Springer", "AIED": "Springer / IAIED",
    "EDM": "IEDMS", "EDM ": "IEDMS",
}


# ----------------------------------------------------------------------------
# 크롤링
# ----------------------------------------------------------------------------

def fetch_from_json(url: str = JSON_URL) -> list[dict[str, Any]]:
    """정식 JSON 엔드포인트에서 컨퍼런스 데이터를 가져온다 (1순위)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError("예상과 다른 JSON 형식입니다 (list 가 아님).")
    return data


def fetch_from_html(url: str = HTML_URL) -> list[dict[str, Any]]:
    """
    폴백: HTML 페이지를 직접 파싱한다.

    이 사이트는 각 컨퍼런스를 data-* 속성을 가진 카드(div)로 렌더링한다.
    JSON 엔드포인트가 동작하지 않을 때만 사용한다.
    """
    from bs4 import BeautifulSoup  # 폴백 경로에서만 필요

    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    confs: list[dict[str, Any]] = []
    # 각 컨퍼런스 카드는 보통 conf 관련 class 또는 data 속성을 가진다.
    for card in soup.select("[data-title], .ConfItem, .conf, li.ConfItem"):
        title = card.get("data-title") or card.get("data-id")
        if not title:
            # 제목 텍스트를 직접 찾아본다.
            head = card.find(["h3", "h4", "a"])
            title = head.get_text(strip=True) if head else None
        if not title:
            continue
        confs.append(
            {
                "title": title,
                "year": _safe_int(card.get("data-year")),
                "full_name": card.get("data-fullname"),
                "place": card.get("data-place"),
                "date": card.get("data-date"),
                "start": card.get("data-start"),
                "end": card.get("data-end"),
                "deadline": card.get("data-deadline"),
                "link": card.get("data-link"),
                "sub": (card.get("data-sub") or "").split(",") if card.get("data-sub") else [],
            }
        )
    if not confs:
        raise RuntimeError(
            "HTML 폴백 파싱 실패: 페이지 구조가 바뀌었을 수 있습니다. "
            "셀렉터를 확인하세요."
        )
    return confs


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------
# 정리 / 변환
# ----------------------------------------------------------------------------

def resolve_publisher(title: str | None, full_name: str | None) -> str | None:
    """컨퍼런스의 기관(출판사)명을 결정한다."""
    if title and title in PUBLISHER_BY_TITLE:
        return PUBLISHER_BY_TITLE[title]
    # title 의 공백/연도 변형 처리
    if title:
        base = title.strip()
        if base in PUBLISHER_BY_TITLE:
            return PUBLISHER_BY_TITLE[base]
    # full_name 휴리스틱
    fn = (full_name or "").upper()
    for key in ("IEEE", "ACM", "SPRINGER", "USENIX", "AAAI"):
        if key in fn:
            return key.title() if key not in ("IEEE", "ACM", "AAAI", "USENIX") else key
    return None


def normalize(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """원본 엔트리들을 요청한 핵심 필드 중심으로 정리한다."""
    out: list[dict[str, Any]] = []
    for c in raw:
        subs = c.get("sub") or []
        subjects = [SUBJECT_NAMES.get(s, s) for s in subs]
        out.append(
            {
                "conference": c.get("title"),
                "full_name": c.get("full_name"),
                "year": c.get("year"),
                "publisher": resolve_publisher(c.get("title"), c.get("full_name")),
                "conference_dates": {
                    "text": c.get("date"),       # 사람이 읽는 원문 (예: "April 6-9, 2027")
                    "start": c.get("start"),     # ISO (예: "2027-04-06")
                    "end": c.get("end"),
                },
                "location": c.get("place"),
                "subjects": subjects,
                "subject_codes": subs,
                "paper_deadline": c.get("deadline"),
                "abstract_deadline": c.get("abstract_deadline"),
                "timezone": c.get("timezone"),
                "h_index": c.get("hindex"),
                "website": c.get("link"),
                "note": c.get("note"),
            }
        )
    # 연도(내림차순), 이름(오름차순)으로 정렬
    out.sort(key=lambda x: (-(x["year"] or 0), str(x["conference"] or "")))
    return out


# ----------------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="탑 컨퍼런스 일정 크롤러 (mlciv ai-deadlines)")
    parser.add_argument("--out", default="conferences.json", help="출력 JSON 파일 경로")
    parser.add_argument("--raw", action="store_true", help="원본 데이터도 conferences_raw.json 으로 저장")
    parser.add_argument("--html", action="store_true", help="JSON 엔드포인트 대신 HTML 폴백을 강제로 사용")
    parser.add_argument("--min-year", type=int, default=2024, help="포함할 최소 연도 (기본 2024)")
    parser.add_argument("--max-year", type=int, default=2027, help="포함할 최대 연도 (기본 2027)")
    args = parser.parse_args()

    # 1) 데이터 가져오기
    raw: list[dict[str, Any]]
    if args.html:
        print("[*] HTML 폴백 파싱 시도...")
        raw = fetch_from_html()
        source = HTML_URL
    else:
        try:
            print(f"[*] JSON 엔드포인트에서 가져오는 중: {JSON_URL}")
            raw = fetch_from_json()
            source = JSON_URL
        except Exception as exc:  # noqa: BLE001
            print(f"[!] JSON 엔드포인트 실패 ({exc}). HTML 폴백으로 전환합니다.")
            raw = fetch_from_html()
            source = HTML_URL

    print(f"[*] {len(raw)}개 엔트리 수집 (past events 포함)")

    # 2) 정리 + 연도 필터 (기본: 2024 ~ 2027)
    conferences = normalize(raw)
    before = len(conferences)
    conferences = [
        c for c in conferences
        if c["year"] is not None and args.min_year <= c["year"] <= args.max_year
    ]
    print(
        f"[*] 연도 필터 {args.min_year}~{args.max_year} 적용: "
        f"{before} -> {len(conferences)}개"
    )
    years = [c["year"] for c in conferences if c["year"]]
    payload = {
        "metadata": {
            "source": source,
            "crawled_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "total_entries": len(conferences),
            "unique_conferences": len({c["conference"] for c in conferences}),
            "year_range": [min(years), max(years)] if years else None,
            "note": (
                "publisher(기관/출판사) 필드는 원본 데이터에 없어 컨퍼런스 약칭 기준 "
                "큐레이션 매핑으로 채운 값입니다. 매핑에 없으면 null."
            ),
        },
        "conferences": conferences,
    }

    # 3) 저장
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[+] 저장 완료: {args.out}")

    if args.raw:
        with open("conferences_raw.json", "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        print("[+] 원본 저장 완료: conferences_raw.json")

    # 4) 요약 출력
    missing_pub = sum(1 for c in conferences if not c["publisher"])
    print(
        f"[i] 고유 컨퍼런스 {payload['metadata']['unique_conferences']}개, "
        f"연도 {payload['metadata']['year_range']}, "
        f"publisher 미지정 {missing_pub}개"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
