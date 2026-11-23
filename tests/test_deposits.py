from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deposits import ParsedDeposit, parse_deposit, score_candidate  # noqa: E402
from app.utils import normalize_name, parse_amount  # noqa: E402

SAMPLES = [
    (
        "KB국민은행",
        "12/22 15:04 123456-78-901234 입금 10,000원 홍길동 잔액 1,234,567원",
        10000,
        "홍길동",
    ),
    ("신한은행", "110-123-456789\n50,000원 입금\n김철수", 50000, "김철수"),
    ("카카오뱅크", "3333-01-1234567\n30,000원 입금\n이영희님", 30000, "이영희"),
    ("토스", "홍길동님에게서 20,000원이 입금되었어요", 20000, "홍길동"),
    ("우리은행", "1002-123-456789 입금 15,000원 (박민수) 잔액 500,000원", 15000, "박민수"),
    ("NH농협은행", "302-1234-5678-91 12/22 14:00 입금 7,000원 최지우", 7000, "최지우"),
    ("하나은행", "하나 123-456789-12345 입금자명: 정다은 12,000원 입금", 12000, "정다은"),
    ("카카오뱅크", "입금 5,000원 W7K2QA", 5000, ""),
    ("국민은행", "12/22 09:11 123456-78-901234 출금 10,000원 홍길동 잔액 100,000원", None, None),
    ("신한카드", "12/22 15:00 스타벅스 6,500원 승인", None, None),
    ("우리은행", "잔액 10,000원", None, None),
    ("기업은행", "12/22 10:00 010-1234-5678 문자 도착", None, None),
]

FAILS = []

for title, body, expected_amount, expected_name in SAMPLES:
    parsed = parse_deposit(title, body)
    if expected_amount is None:
        if parsed is not None:
            FAILS.append(f"should be ignored: {title} / {body} -> {parsed}")
        continue
    if parsed is None:
        FAILS.append(f"should parse: {title} / {body}")
        continue
    if parsed.amount != expected_amount:
        FAILS.append(f"amount {parsed.amount} != {expected_amount}: {body}")
    if expected_name and normalize_name(parsed.depositor) != normalize_name(expected_name):
        FAILS.append(f"depositor {parsed.depositor!r} != {expected_name!r}: {body}")
    if "W7K2QA" in body and parsed.code != "W7K2QA":
        FAILS.append(f"code {parsed.code!r} != 'W7K2QA': {body}")

print("failures:", len(FAILS))
for failure in FAILS:
    print(" -", failure)


def test_samples_all_pass():
    assert not FAILS, "\n".join(FAILS)


def test_amount_and_name_scoring():
    pending = {"code": "W7K2QA", "depositor": "홍길동"}
    parsed = ParsedDeposit(10000, [10000], "홍길동", None, "입금 10,000원 홍길동", "국민")
    assert score_candidate(parsed, pending) == 100

    code_parsed = ParsedDeposit(10000, [10000], "", "W7K2QA", "입금 10,000원 W7K2QA", "")
    assert score_candidate(code_parsed, pending) == 300

    other = ParsedDeposit(10000, [10000], "김철수", None, "입금 10,000원 김철수", "")
    assert score_candidate(other, pending) == 0


def test_parse_amount():
    assert parse_amount("10,000원") == 10000
    assert parse_amount("1만원") == 10000
    assert parse_amount("5000") == 5000
    assert parse_amount("-3000") == -3000
    assert parse_amount("abc") is None
