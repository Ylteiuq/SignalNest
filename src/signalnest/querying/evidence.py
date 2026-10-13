"""Conservative deadline citation checks; production facts/policy are unchanged."""

from html import escape

from signalnest.contracts import NoticeContent
from signalnest.notifications.contracts import NoticeFacts
from signalnest.notifications.facts import extract_facts
from signalnest.querying.contracts import Quote

_BREAKS = "。；;\n"  # The v8 extractor's witnessed temporal clause boundaries.
_MAX_STATEMENT = 500
_MAX_PROBES = 8
_MAX_PROOFS = 4


def _bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Recover the complete clause: extractor excerpts may stop at 120 characters."""
    left = max(text.rfind(mark, 0, start) for mark in _BREAKS) + 1
    ends = [index for mark in _BREAKS if (index := text.find(mark, end)) != -1]
    return left, min(ends, default=len(text))


class DeadlineEvidence:
    """Reuse the existing pure extractor on an isolated statement and chosen quote.

    Keeping the non-temporal title allows witnessed TA submission clauses to keep
    their subject. No other date-bearing clause can supply the quote's deadline.
    This is evidence validation, not a second date parser or a new notice version.
    """

    def __init__(self, content: NoticeContent, facts: NoticeFacts):
        self._sources = {"title": content.title, "body_text": content.body_text}
        self._date = content.published_date
        self._deadline = facts.deadline_at
        self._lower = facts.deadline_lower_at
        title = content.title
        for proof in facts.time_evidence:
            if proof.field == "title":
                left, right = _bounds(content.title, proof.start, proof.end)
                # Preserve offsets for overlapping proofs and clause separators.
                title = title[:left] + " " * (right - left) + title[right:]
        self._title = title.strip() or "通知"
        self.proofs: list[dict] = []
        if self._deadline is None:
            return
        seen = set()
        for proof in facts.time_evidence[:_MAX_PROBES]:
            if proof.field not in {"title", "body_text"}:
                continue
            text = getattr(content, proof.field)
            left, right = _bounds(text, proof.start, proof.end)
            statement = text[left:right]
            key = proof.field, left, right
            if key in seen or len(statement) > _MAX_STATEMENT:
                continue
            seen.add(key)
            if self._matches(proof.field, statement):
                self.proofs.append(
                    {"field": proof.field, "start": left, "end": right, "excerpt": statement}
                )
                if len(self.proofs) == _MAX_PROOFS:
                    break

    def _matches(self, field: str, fragment: str) -> bool:
        # Build only an in-memory extraction input; never persist or hash it.
        probe = NoticeContent(
            title=fragment if field == "title" else self._title,
            published_date=self._date,
            body_html="<p>" + escape(fragment) + "</p>",
            body_text=fragment if field == "body_text" else "",
        )
        extracted = extract_facts(probe)
        return (
            extracted.deadline_at == self._deadline and extracted.deadline_lower_at == self._lower
        )

    def supports(self, quote: Quote) -> bool:
        # The whole statement must first support the boundary: removing an
        # administrative subject from a substring must not change its scope.
        text = self._sources[quote.field]
        offset = text.find(quote.text)
        if offset == -1:
            return False
        for _ in range(_MAX_PROBES):
            if not any(
                quote.field == proof["field"]
                and proof["start"] <= offset
                and offset + len(quote.text) <= proof["end"]
                for proof in self.proofs
            ):
                return False
            offset = text.find(quote.text, offset + 1)
            if offset == -1:
                return self._matches(quote.field, quote.text)
        # Quote has no offsets: many/ambiguous occurrences cannot prove its scope.
        return False
