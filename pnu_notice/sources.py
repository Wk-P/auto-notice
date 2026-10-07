from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    display_name: str
    board_id: str
    page_url: str
    rss_url: str


SOURCES: tuple[Source, ...] = (
    Source(
        "cse_undergraduate",
        "PNU CSE Undergraduate Notices",
        "计算机本科生公告",
        "2055",
        "https://cse.pusan.ac.kr/cse/14221/subview.do",
        "https://cse.pusan.ac.kr/bbs/cse/2055/rssList.do?row=50",
    ),
    Source(
        "cse_graduate",
        "PNU CSE Graduate Notices",
        "计算机大学院公告",
        "2058",
        "https://cse.pusan.ac.kr/cse/14227/subview.do",
        "https://cse.pusan.ac.kr/bbs/cse/2058/rssList.do?row=50",
    ),
    Source(
        "international_student",
        "PNU International Student Notices",
        "国际处留学生公告",
        "2081",
        "https://international.pusan.ac.kr/international/15224/subview.do",
        "https://international.pusan.ac.kr/bbs/international/2081/rssList.do?row=50",
    ),
)

SOURCE_BY_KEY = {source.key: source for source in SOURCES}
