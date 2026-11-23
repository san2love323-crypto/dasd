"""한국 은행/간편결제 입금 알림 파서 + 자동충전 매칭 엔진.

Pushbullet 으로 미러링된 안드로이드 알림에서 입금 금액/입금자명을 추출한 뒤,
사용자가 등록한 입금 요청(pending_deposits)과 대조해 잔액을 자동 충전합니다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import utils
from .db import Database, DatabaseError
from .plans import resolve_plan

log = logging.getLogger("vendrix.deposits")

DEPOSIT_WORDS = ("입금", "입금완료", "들어왔", "받았", "충전완료", "충전되었", "입금되었")
STRONG_DEPOSIT_WORDS = ("입금", "입금완료", "입금되었", "충전완료", "충전되었")
NEGATIVE_WORDS = ("출금", "승인취소", "결제취소", "잔액부족", "한도초과", "실패", "오류")

BANK_WORDS = (
    "국민", "kb", "신한", "우리", "하나", "농협", "nh", "기업", "ibk", "카카오뱅크", "카카오",
    "토스", "케이뱅크", "kbank", "수협", "신협", "우체국", "새마을금고", "부산은행", "대구은행",
    "경남은행", "광주은행", "전북은행", "제주은행", "sc제일", "씨티", "산업은행", "은행", "뱅크",
)

STOPWORDS = {
    "은행", "뱅크", "입금", "출금", "이체", "잔액", "계좌", "거래", "정상", "처리", "완료", "알림",
    "안내", "고객", "예금주", "자동", "체크", "카드", "결제", "승인", "충전", "상품", "금액",
    "국민", "신한", "우리", "하나", "농협", "기업", "카카오", "토스", "케이", "수협", "신협",
    "우체국", "새마을", "금고", "제일", "씨티", "산업", "오늘", "내일", "정상처리", "입금자명",
    "보낸분", "받는분", "송금", "이용", "내역", "문자", "메시지", "앱", "뱅킹", "인터넷",
}

NAME_RE = re.compile(r"^[가-힣A-Za-z][가-힣A-Za-z0-9*]{1,15}$")
CODE_RE = re.compile(r"\b(W[A-Z0-9]{5})\b")


@dataclass
class ParsedDeposit:
    amount: int
    amounts: list[int]
    depositor: str
    code: str | None
    text: str
    bank: str
    application: str = ""

    def as_note(self) -> str:
        return f"amount={self.amount} depositor={self.depositor or '-'} code={self.code or '-'}"


@dataclass
class MatchOutcome:
    status: str  # matched | review | unmatched | duplicate | ignored
    deposit: dict | None = None
    amount: int | None = None
    depositor: str = ""
    user_id: str | None = None
    guild_id: str | None = None
    balance: int | None = None
    candidates: list[dict] = field(default_factory=list)
    message: str = ""


def _clean(text: str) -> str:
    text = (text or "").replace("\u200b", " ")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _extract_amounts(text: str) -> list[tuple[int, int]]:
    """(위치, 금액) 목록. '잔액 1,234,567원' 구간은 제거한다."""
    cleaned = re.sub(r"잔액\s*[:：]?\s*[\d,]+\s*원?", " ", text)
    cleaned = re.sub(r"[\d,]+\s*원?\s*(?:잔액|남은금액)", " ", cleaned)
    out: list[tuple[int, int]] = []
    for match in re.finditer(r"(\d[\d,]{0,12})\s*원", cleaned):
        try:
            value = int(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if 0 < value <= 1_000_000_000:
            out.append((match.start(), value))
    return out


def _deposit_positions(text: str) -> list[int]:
    positions: list[int] = []
    for word in DEPOSIT_WORDS:
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + len(word)
    return positions or [0]


def _is_name(token: str) -> bool:
    token = token.strip("()[]{}<>.,:;'\"")
    if not token or token in STOPWORDS:
        return False
    if token.lower() in {word.lower() for word in BANK_WORDS}:
        return False
    if not NAME_RE.match(token):
        return False
    if token.isdigit():
        return False
    return True


def extract_depositor(text: str, amounts: list[int]) -> str:
    # 1) 명시적 입금자명
    for pattern in (
        r"입금자명\s*[:：]?\s*([가-힣A-Za-z0-9*]{2,20})",
        r"입금자\s*[:：]?\s*([가-힣A-Za-z0-9*]{2,20})",
    ):
        match = re.search(pattern, text)
        if match and _is_name(match.group(1)):
            return match.group(1)

    # 2) '홍길동님' 형태
    for match in re.finditer(r"([가-힣]{2,10})\s*님", text):
        candidate = match.group(1)
        if candidate not in {"고객", "예금주", "본인", "상대", "받는", "보내는"} and _is_name(candidate):
            return candidate

    # 3) 금액 앞뒤의 한글 이름
    candidates: list[tuple[int, str]] = []
    for amount in amounts:
        for token in (f"{amount:,}", str(amount)):
            idx = text.find(token)
            if idx < 0:
                continue
            before = re.findall(r"([가-힣]{2,10})\s*$", text[:idx].strip())
            after = re.findall(r"^\s*\(?([가-힣]{2,10})\)?", text[idx + len(token) :].strip())
            if before and _is_name(before[-1]):
                candidates.append((idx, before[-1]))
            if after and _is_name(after[0]) and after[0] not in {"입금", "출금", "잔액", "이체", "완료"}:
                candidates.append((idx, after[0]))
            break

    # 4) '입금 10,000원 홍길동' / '홍길동 10,000원 입금'
    for match in re.finditer(r"입금\s*(?:금액)?\s*[:：]?\s*[\d,]+\s*원\s*\(?([가-힣]{2,10})\)?", text):
        if _is_name(match.group(1)):
            candidates.append((match.start(), match.group(1)))
    for match in re.finditer(
        r"([가-힣]{2,10})\s*(?:님)?\s*(?:이|가|께서)?\s*[\d,]+\s*원\s*(?:을|를)?\s*입금", text
    ):
        if _is_name(match.group(1)):
            candidates.append((match.start(), match.group(1)))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    # 5) 마지막 수단: 입금 키워드 주변의 한글 토큰
    tokens = re.findall(r"[가-힣]{2,6}", text)
    for token in reversed(tokens):
        if _is_name(token) and not any(bank in token for bank in ("은행", "뱅크", "카드", "계좌")):
            return token
    return ""


def parse_deposit(title: str | None, body: str | None, application: str = "") -> ParsedDeposit | None:
    text = _clean(f"{title or ''} {body or ''}")
    if not text:
        return None

    has_deposit = any(word in text for word in STRONG_DEPOSIT_WORDS) or "입금" in text
    if not has_deposit:
        return None
    if any(word in text for word in NEGATIVE_WORDS) and not any(word in text for word in ("입금", "입금완료")):
        return None
    if "출금" in text and "입금" not in text:
        return None

    amount_hits = _extract_amounts(text)
    if not amount_hits:
        return None

    positions = _deposit_positions(text)
    amount_hits.sort(key=lambda hit: min(abs(hit[0] - pos) for pos in positions))
    amounts = [value for _, value in amount_hits]
    amount = amounts[0]
    if amount <= 0:
        return None

    depositor = extract_depositor(text, amounts)
    code_match = CODE_RE.search(text.upper())
    code = code_match.group(1) if code_match else None

    bank = ""
    for word in BANK_WORDS:
        if word in text.lower():
            bank = word
            break

    return ParsedDeposit(
        amount=amount,
        amounts=amounts,
        depositor=depositor,
        code=code,
        text=text,
        bank=bank,
        application=application,
    )


def score_candidate(parsed: ParsedDeposit, pending: dict) -> int:
    """입금 요청과 알림의 일치 점수. 0 이면 불일치."""
    score = 0
    if parsed.code and pending.get("code") and parsed.code.upper() == str(pending["code"]).upper():
        score += 300
    elif parsed.code and pending.get("code") and str(pending["code"]).upper() in parsed.text.upper():
        score += 250

    pending_name = utils.normalize_name(pending.get("depositor"))
    parsed_name = utils.normalize_name(parsed.depositor)
    if parsed_name and pending_name:
        if parsed_name == pending_name:
            score += 100
        elif len(parsed_name) >= 3 and len(pending_name) >= 3 and (
            parsed_name in pending_name or pending_name in parsed_name
        ):
            score += 70
        else:
            return 0
    return score


class DepositProcessor:
    def __init__(self, db: Database, store, notifier: Any | None = None):
        self.db = db
        self.store = store
        self.notifier = notifier
        self.log = log

    # -------------------------------------------------------------- filtering
    def is_relevant(self, push: dict) -> bool:
        if push.get("type") != "mirror":
            return False
        if push.get("dismissed"):
            return False
        package = (push.get("package_name") or "").strip()
        if package and package in self.store.blocked_packages():
            return False
        allowed = self.store.allowed_packages()
        if allowed and package not in allowed:
            return False
        return True

    # ----------------------------------------------------------------- process
    async def process_push(self, push: dict, source: str = "pushbullet") -> MatchOutcome:
        iden = (push.get("iden") or "").strip()
        if not iden:
            return MatchOutcome("ignored", message="iden 없음")
        if self.db.deposit_exists(iden):
            return MatchOutcome("duplicate")

        if not self.is_relevant(push):
            return MatchOutcome("ignored", message="관련 없는 알림")

        parsed = parse_deposit(push.get("title"), push.get("body"), push.get("application_name") or "")
        if parsed is None:
            return MatchOutcome("ignored", message="입금 알림 아님")

        record = self.db.record_deposit(
            push_iden=iden,
            source=source,
            application=push.get("application_name"),
            title=utils.truncate(push.get("title") or "", 200),
            body=utils.truncate(push.get("body") or "", 1600),
            amount=parsed.amount,
            depositor=parsed.depositor,
            status="unmatched",
            note=parsed.as_note(),
        )
        if record is None:
            return MatchOutcome("duplicate")

        outcome = await self._match(record, parsed)
        await self._notify(outcome)
        return outcome

    async def _match(self, record: dict, parsed: ParsedDeposit) -> MatchOutcome:
        candidates = self.db.pending_for_amount(parsed.amount)
        scored: list[tuple[int, dict]] = []
        for pending in candidates:
            score = score_candidate(parsed, pending)
            if score > 0:
                scored.append((score, pending))
            elif not parsed.depositor and not parsed.code:
                scored.append((5, pending))

        scored.sort(key=lambda item: (item[0], item[1]["created_at"]))
        outcome = MatchOutcome(
            status="unmatched",
            deposit=record,
            amount=parsed.amount,
            depositor=parsed.depositor,
            candidates=[item[1] for item in scored],
        )

        if not scored:
            outcome.message = "일치하는 입금 요청이 없습니다."
            return outcome

        best_score = scored[0][0]
        best = [item for item in scored if item[0] == best_score]
        if len(best) > 1:
            outcome.status = "review"
            outcome.message = f"동일 금액/이름의 입금 요청이 {len(best)}건이라 수동 확인이 필요합니다."
            self.db.mark_deposit(record["id"], "review", outcome.message)
            return outcome

        pending = best[0][1]
        self.db.attach_pending(record["id"], int(pending["id"]))
        record["pending_id"] = int(pending["id"])
        outcome.candidates = [pending]
        outcome.user_id = str(pending["discord_id"])
        outcome.guild_id = str(pending["guild_id"])

        # 이름/코드 매칭이 약한 경우(금액만 일치)는 자동 충전하지 않는다.
        if best_score < 60:
            outcome.status = "review"
            outcome.message = "금액만 일치하고 입금자명/코드가 확인되지 않았습니다."
            self.db.mark_deposit(record["id"], "review", outcome.message)
            return outcome

        guild = self.db.get_guild(pending["guild_id"])
        if not self.store.auto_charge_master():
            outcome.status = "review"
            outcome.message = "자동충전 마스터 스위치가 꺼져 있습니다."
        elif guild is None or not int(guild.get("auto_charge") or 0):
            outcome.status = "review"
            outcome.message = "서버의 자동충전이 꺼져 있습니다."
        elif self.store.license_enforce() and not self.db.license_valid(guild):
            outcome.status = "review"
            outcome.message = "서버 라이센스가 만료되어 자동충전이 보류되었습니다."
        else:
            plan = resolve_plan(self.db.plan_key_for_guild(guild))
            if not plan.auto_charge:
                outcome.status = "review"
                outcome.message = f"{plan.name} 플랜에서는 자동충전을 사용할 수 없습니다."

        if outcome.status == "review":
            self.db.mark_deposit(record["id"], "review", outcome.message)
            return outcome

        try:
            result = self.db.approve_deposit(
                record["id"], pending["guild_id"], pending["discord_id"], actor="auto-charge", note="자동충전"
            )
        except DatabaseError as exc:
            outcome.status = "review"
            outcome.message = f"자동 충전 실패: {exc}"
            self.db.mark_deposit(record["id"], "review", outcome.message)
            return outcome

        outcome.status = "matched"
        outcome.balance = result["deposit"].get("balance")
        outcome.amount = int(result["deposit"].get("amount") or parsed.amount)
        outcome.message = "자동 충전 완료"
        self.log.info("auto charged guild=%s user=%s amount=%s", outcome.guild_id, outcome.user_id, outcome.amount)
        return outcome

    # ---------------------------------------------------------------- notify
    async def _notify(self, outcome: MatchOutcome) -> None:
        if self.notifier is None:
            return
        try:
            if outcome.status == "matched":
                await self.notifier.deposit_matched(outcome)
            elif outcome.status == "review":
                await self.notifier.deposit_review(outcome)
            elif outcome.status == "unmatched":
                await self.notifier.deposit_unmatched(outcome)
        except Exception:  # 알림 실패가 충전 자체를 되돌리면 안 됨
            self.log.exception("deposit notification failed")

    async def approve_manually(self, deposit_id: int, guild_id: str, discord_id: str, actor: str) -> dict:
        result = self.db.approve_deposit(deposit_id, guild_id, discord_id, actor=actor, note="수동 승인")
        outcome = MatchOutcome(
            status="matched",
            deposit=result["deposit"],
            amount=int(result["deposit"].get("amount") or 0),
            user_id=str(discord_id),
            guild_id=str(guild_id),
            balance=result["deposit"].get("balance"),
            message="수동 승인",
        )
        await self._notify(outcome)
        return result
