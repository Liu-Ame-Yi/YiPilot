"""Strict documents and independent review gates for the specification workflow."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1, max_length=8000)]
Texts = Annotated[list[Text], Field(min_length=1, max_length=100)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Criterion(Contract):
    id: Text
    behavior: Text
    acceptance: Text
    sources: Texts


class Fact(Contract):
    statement: Text
    evidence: Texts


class Question(Contract):
    id: Text
    question: Text


class SRS(Contract):
    scope: Text
    non_goals: Texts
    constraints: Texts
    criteria: Annotated[list[Criterion], Field(min_length=1, max_length=100)]
    code_facts: list[Fact]
    assumptions: list[Text]
    open_questions: list[Question]


class TestMapping(Contract):
    criterion_id: Text
    test: Text


class SDD(Contract):
    srs_sha256: Text
    modules: Texts
    interfaces: Texts
    data: Texts
    steps: Texts
    tests: Annotated[list[TestMapping], Field(min_length=1, max_length=100)]
    code_facts: list[Fact]
    assumptions: list[Text]
    open_questions: list[Question]


class Issue(Contract):
    id: Text
    kind: Literal["revise", "clarify", "fatal"]
    description: Text
    evidence: Texts


class Review(Contract):
    document_sha256: Text
    verdict: Literal["accept", "revise", "clarify", "stop"]
    summary: Text
    evidence: Texts
    issues: list[Issue]


def validate_document(document: SRS | SDD, sources: set[str], code: set[str],
                      srs: SRS | None = None, srs_sha256: str | None = None) -> None:
    for fact in document.code_facts:
        if not set(fact.evidence) <= code:
            raise ValueError("code facts must cite supplied code evidence")
    ids = [q.id for q in document.open_questions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate question IDs")
    if isinstance(document, SRS):
        ids = [criterion.id for criterion in document.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate criterion IDs")
        if any(not set(item.sources) <= sources for item in document.criteria):
            raise ValueError("criteria must trace to original requirements or answers")
    else:
        if srs is None or document.srs_sha256 != srs_sha256:
            raise ValueError("design must reference the accepted SRS version")
        if {item.criterion_id for item in document.tests} != {item.id for item in srs.criteria}:
            raise ValueError("test mapping must cover exactly the accepted criteria")


def validate_review(review: Review, document: SRS | SDD, digest: str,
                    evidence: set[str], required: set[str]) -> None:
    if review.document_sha256 != digest:
        raise ValueError("review references another document version")
    if not required <= set(review.evidence) or not set(review.evidence) <= evidence:
        raise ValueError("review must check its required upstream evidence")
    if any(not set(issue.evidence) <= evidence for issue in review.issues):
        raise ValueError("review cites unknown evidence")
    ids = [issue.id for issue in review.issues]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate issue IDs")
    kinds = {issue.kind for issue in review.issues}
    expected = ("stop" if "fatal" in kinds else "clarify" if "clarify" in kinds
                else "revise" if kinds else "accept")
    if review.verdict != expected:
        raise ValueError("verdict contradicts issues")
    if document.open_questions and review.verdict == "accept":
        raise ValueError("unresolved questions cannot pass review")
    if document.open_questions and not kinds & {"clarify", "fatal"}:
        raise ValueError("unresolved questions require clarification")
