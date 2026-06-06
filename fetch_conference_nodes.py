# -*- coding: utf-8 -*-
"""
컨퍼런스 논문 -> OpenAlex KG 노드 수집기 (DBLP → OpenAlex 브리지)
================================================================

`confer_results/conferences.json` 의 컨퍼런스 논문을 받아오되, 출력 포맷을
`KG_data/PaperRecommendSys_KG/nodes/preview_top5.json` 와 **동일한 OpenAlex 노드
스키마**(abstract / source / authorships / primary_topic / referenced_works /
related_works ...)로 저장한다.

왜 브리지인가
-------------
- 타겟 포맷의 핵심 필드(referenced_works, related_works, primary_topic, abstract)는
  오직 OpenAlex 에만 있다.
- 그런데 OpenAlex 는 2023+ 컨퍼런스 논문을 venue(source)에 연결하지 않아
  "컨퍼런스명 → venue → 논문" 경로로는 최근 논문을 못 찾는다.
- 반면 그 논문들의 레코드 자체는 DOI 로 OpenAlex 안에 존재한다.
  → 그래서 **DBLP 로 컨퍼런스 논문 목록 + DOI 를 확보**하고, 그 DOI(또는 제목)로
    **OpenAlex Work 를 조회**해 타겟 포맷으로 변환한다.

파이프라인
----------
    1) DBLP: venue:<약칭>: year:<연도>: 로 해당 컨퍼런스 논문 목록(+DOI/ee) 수집
    2) DOI 가 있는 논문 → OpenAlex /works?filter=doi:a|b|... (50개씩 배치) 조회
    3) DOI 가 없는 논문(예: OpenReview 계열) → OpenAlex search 로 제목+연도 매칭(폴백)
    4) openalex_format.to_node() 로 변환 → 컨퍼런스별 JSON(노드 배열) 저장

출력 파일은 preview_top5.json 과 동일하게 "노드 객체의 평면 배열" 이다.
메타/통계는 _summary.json 에 별도 저장.

인증
----
OpenAlex 키는 env OPENALEX_API_KEY 또는 --api-key 로 전달(하드코딩 금지).

사용 예
-------
    export OPENALEX_API_KEY=...
    python fetch_conference_nodes.py --only CVPR --year 2024 --max-per-conf 20
    python fetch_conference_nodes.py --only ICLR --year 2024 --max-per-conf 20   # DOI없음→제목폴백
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import requests

from openalex_format import OPENALEX_NODE_SELECT, to_node
# 기존 모듈 재사용 (모듈 임포트 시 main()은 실행되지 않음)
from fetch_dblp_papers import clean_acronym, fetch_conf as dblp_fetch_conf, simplify_hit as dblp_simplify
from fetch_openalex_papers import OPENALEX, get_json, make_session


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def chunks(seq: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def normalize_title(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def extract_doi(paper: dict[str, Any]) -> str | None:
    """DBLP 레코드에서 정규화된 bare DOI(소문자) 추출. doi 필드 우선, 없으면 ee 에서 파싱."""
    doi = paper.get("doi")
    if doi:
        return doi.lower().replace("https://doi.org/", "").replace("http://dx.doi.org/", "")
    ee = paper.get("ee") or ""
    m = re.search(r"doi\.org/(10\.\S+)", ee, re.I)
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------------------
# OpenAlex 조회
# ---------------------------------------------------------------------------

def openalex_by_dois(session: requests.Session, dois: list[str], sleep: float) -> dict[str, dict[str, Any]]:
    """bare DOI 리스트 -> {doi: work}. 50개씩 배치 OR 조회."""
    found: dict[str, dict[str, Any]] = {}
    for chunk in chunks(dois, 50):
        data = get_json(session, f"{OPENALEX}/works", {
            "filter": "doi:" + "|".join(chunk),
            "select": OPENALEX_NODE_SELECT,
            "per-page": 50,
        }, sleep=sleep)
        for w in data.get("results", []):
            d = (w.get("doi") or "").lower().replace("https://doi.org/", "")
            if d:
                found[d] = w
    return found


def openalex_by_title(session: requests.Session, title: str, year: int | None,
                      sleep: float) -> dict[str, Any] | None:
    """
    제목으로 OpenAlex Work 검색(폴백). 정규화 제목이 정확히 일치할 때만 채택.

    주의: publication_year 로 거르지 않는다. ICLR/NeurIPS 등은 OpenAlex 에
    arXiv 프리프린트(전년도) 레코드로 들어있는 경우가 많아 연도 필터를 걸면
    매칭을 놓친다. 대신 연도가 가까운 후보를 우선 채택한다.
    """
    data = get_json(session, f"{OPENALEX}/works",
                    {"search": title, "select": OPENALEX_NODE_SELECT, "per-page": 10}, sleep=sleep)
    target = normalize_title(title)
    candidates = [w for w in data.get("results", []) if normalize_title(w.get("display_name")) == target]
    if not candidates:
        return None
    if year is not None:
        candidates.sort(key=lambda w: abs((w.get("publication_year") or 0) - year))
    return candidates[0]


# ---------------------------------------------------------------------------
# 한 컨퍼런스 처리
# ---------------------------------------------------------------------------

def collect_nodes(dblp_session: requests.Session, oa_session: requests.Session,
                  acronym: str, year: int | None, *, max_per_conf: int,
                  dblp_sleep: float, oa_sleep: float, title_fallback: bool,
                  first_author_only: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # 1) DBLP 논문 목록
    used_q, hits = dblp_fetch_conf(dblp_session, acronym, year,
                                   max_results=max_per_conf, sleep=dblp_sleep)
    papers = [dblp_simplify(h) for h in hits]

    # 2) DOI / no-DOI 분리
    with_doi: list[tuple[dict[str, Any], str]] = []
    no_doi: list[dict[str, Any]] = []
    for p in papers:
        d = extract_doi(p)
        (with_doi.append((p, d)) if d else no_doi.append(p))

    nodes: list[dict[str, Any]] = []

    # 2a) DOI 배치 조회
    doi_list = [d for _, d in with_doi]
    doi_map = openalex_by_dois(oa_session, doi_list, oa_sleep) if doi_list else {}
    matched_doi = 0
    for _, d in with_doi:
        w = doi_map.get(d)
        if w:
            nodes.append(to_node(w, first_author_only=first_author_only))
            matched_doi += 1

    # 3) 제목 폴백 (DOI 없는 논문)
    matched_title = 0
    if title_fallback:
        for p in no_doi:
            w = openalex_by_title(oa_session, p.get("title") or "", year, oa_sleep)
            if w:
                nodes.append(to_node(w, first_author_only=first_author_only))
                matched_title += 1

    stats = {
        "conference": acronym, "year": year, "dblp_query": used_q,
        "dblp_papers": len(papers),
        "with_doi": len(with_doi), "matched_by_doi": matched_doi,
        "without_doi": len(no_doi),
        "matched_by_title": matched_title if title_fallback else None,
        "nodes": len(nodes),
        "unmatched": len(papers) - len(nodes),
    }
    return nodes, stats


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def load_conferences(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["conferences"] if isinstance(data, dict) else data


def main() -> int:
    p = argparse.ArgumentParser(description="컨퍼런스 논문 -> OpenAlex KG 노드 (DBLP→OpenAlex 브리지)")
    p.add_argument("--input", default="confer_results/conferences.json")
    p.add_argument("--out-dir", default="confer_results/nodes")
    p.add_argument("--api-key", default=os.environ.get("OPENALEX_API_KEY"))
    p.add_argument("--mailto", default=os.environ.get("OPENALEX_MAILTO"))
    p.add_argument("--max-per-conf", type=int, default=200, help="컨퍼런스당 최대 논문 수")
    p.add_argument("--limit", type=int, default=None, help="처음 N개 엔트리만(테스트)")
    p.add_argument("--only", default=None, help="쉼표 구분 약칭만")
    p.add_argument("--year", type=int, default=None, help="이 연도 엔트리만")
    p.add_argument("--first-author-only", action="store_true",
                   help="authorships 를 제1저자만(preview_top5.json 처럼). 기본은 전체 저자")
    p.add_argument("--no-title-fallback", action="store_true",
                   help="DOI 없는 논문의 제목검색 폴백 비활성화")
    p.add_argument("--dblp-sleep", type=float, default=1.6, help="DBLP 요청 간격(초)")
    p.add_argument("--oa-sleep", type=float, default=0.1, help="OpenAlex 요청 간격(초)")
    args = p.parse_args()

    confs = load_conferences(Path(args.input))
    if args.only:
        want = {a.strip().upper() for a in args.only.split(",")}
        confs = [c for c in confs if clean_acronym(c["conference"]).upper() in want]
    if args.year is not None:
        confs = [c for c in confs if c.get("year") == args.year]
    if args.limit is not None:
        confs = confs[:args.limit]
    if not confs:
        print("[!] 처리할 컨퍼런스가 없습니다.")
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    oa_session = make_session(args.api_key, args.mailto)
    dblp_session = requests.Session()
    dblp_session.headers.update({"User-Agent": "top-confer-kg/1.0 (academic use)"})
    auth = "api_key" if args.api_key else ("mailto" if args.mailto else "anonymous")
    print(f"[*] 입력 {len(confs)}개 | DBLP→OpenAlex | 인증={auth} | 컨퍼런스당 최대 {args.max_per_conf}편\n")

    summary: list[dict[str, Any]] = []
    for i, c in enumerate(confs, 1):
        acr = c["conference"]
        year = c.get("year")
        print(f"[{i}/{len(confs)}] {clean_acronym(acr)} {year} ...", end=" ", flush=True)
        try:
            nodes, stats = collect_nodes(
                dblp_session, oa_session, acr, year,
                max_per_conf=args.max_per_conf, dblp_sleep=args.dblp_sleep,
                oa_sleep=args.oa_sleep, title_fallback=not args.no_title_fallback,
                first_author_only=args.first_author_only,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"실패: {exc}")
            summary.append({"conference": acr, "year": year, "status": "error", "detail": str(exc)})
            continue

        print(f"DBLP {stats['dblp_papers']}편 → OpenAlex 노드 {stats['nodes']}개 "
              f"(DOI매칭 {stats['matched_by_doi']}, 제목매칭 {stats['matched_by_title']})")

        # preview_top5.json 와 동일한 "노드 배열" 형태로 저장
        fname = f"{clean_acronym(acr).replace('/', '-').replace(' ', '_').replace('*', 'star')}_{year}.json"
        (out_dir / fname).write_text(json.dumps(nodes, ensure_ascii=False, indent=2), encoding="utf-8")
        stats["file"] = fname
        stats["status"] = "ok" if nodes else "no_nodes"
        summary.append(stats)

    (out_dir / "_summary.json").write_text(
        json.dumps({"total": len(summary), "results": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    tot = sum(s.get("nodes", 0) for s in summary)
    print(f"\n[+] 완료: 총 노드 {tot}개")
    print(f"[+] 저장: {out_dir}/ (컨퍼런스별 노드배열 JSON + _summary.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
