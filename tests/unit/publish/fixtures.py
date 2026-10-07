"""A small, hand-built finished run for the publisher tests."""

from story_agent.schema import (
    AcceptanceCriterion,
    Answer,
    AnswerKind,
    CriterionKind,
    Priority,
    Provenance,
    ProvenanceType,
    Question,
    QuestionRound,
    Requirement,
    RunState,
    Scenario,
    Story,
    StoryStatus,
)

SCENARIO = (
    "A customer disputes a card payment and expects a temporary credit while the bank "
    "investigates. The relationship manager can see the case."
)
NOTES = "Keep it short."


def excerpt(text: str) -> Provenance:
    return Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref=text, element="story")


def answer_ref(qid: str, element: str) -> Provenance:
    return Provenance(type=ProvenanceType.CLARIFICATION_ANSWER, ref=qid, element=element)


def finished_state() -> RunState:
    """Three published stories in two epics, one rejected story, answers of every kind."""
    credit = "expects a temporary credit while the bank investigates"
    rm = "The relationship manager can see the case"
    question = Question(
        id="Q1-dispute_handling",
        category="dispute_handling",
        question="How long does the customer have to raise a dispute?",
        why_it_matters="It sets the dispute window rule.",
        options=["30 days", "60 days"],
    )
    limits = Question(
        id="Q1-limits_velocity",
        category="limits_velocity",
        question="Is there a daily claim limit?",
        why_it_matters="It shapes the claim rules.",
        options=["Yes", "No"],
    )
    answers = [
        Answer(question_id=question.id, kind=AnswerKind.OPTION, value="60 days"),
        Answer(
            question_id=limits.id,
            kind=AnswerKind.MEMORY_CONFIRMED,
            value="No daily claim limit",
            memory_id="M-abc1234567",
        ),
        Answer(
            question_id="Q-final-audit",
            kind=AnswerKind.JUDGMENT,
            value="Agent judgment, approved by the user",
        ),
    ]
    requirements = [
        Requirement(
            id="REQ-001",
            text="Customer expects a temporary credit during the investigation",
            category="dispute_handling",
            provenance=[excerpt(credit)],
        ),
        Requirement(
            id="REQ-002",
            text="Dispute window is 60 days",
            category="dispute_handling",
            provenance=[answer_ref(question.id, "item")],
        ),
        Requirement(
            id="REQ-003",
            text="Disputes are kept in an audit trail",
            category="audit",
            assumed=True,
            provenance=[answer_ref("Q-final-audit", "item")],
        ),
    ]
    first = Story(
        id="DSP-0001",
        epic="Disputes",
        feature="Raise a dispute",
        title="Raise a card dispute",
        persona="Customer",
        want="to dispute a card payment within 60 days",
        benefit="I get a temporary credit while the bank investigates",
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                given="a card payment made 10 days ago",
                when="the customer raises a dispute",
                then="a temporary credit is posted",
            ),
            AcceptanceCriterion(
                id="AC-2",
                given="a card payment made 90 days ago",
                when="the customer raises a dispute",
                then="the dispute is refused with the reason",
                kind=CriterionKind.ERROR,
            ),
        ],
        priority=Priority.MUST,
        estimate=5,
        requirement_ids=["REQ-001", "REQ-002"],
        clarification_refs=[question.id],
        provenance=[
            Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref=credit, element="persona"),
            Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref=credit, element="benefit"),
            answer_ref(question.id, "want"),
        ],
        confidence=0.9,
        status=StoryStatus.APPROVED,
    )
    second = Story(
        id="DSP-0002",
        epic="Disputes",
        title="Keep an audit trail of disputes",
        persona="Compliance officer",
        want="every dispute action recorded",
        benefit="I can answer an audit",
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                given="a dispute is raised",
                when="its status changes",
                then="the change is recorded with who and when",
            )
        ],
        priority=Priority.SHOULD,
        estimate=3,
        nfrs=["Audit records are kept for 7 years"],
        assumptions=["Records are kept for the bank's standard retention period"],
        open_questions=["Which retention period applies?"],
        requirement_ids=["REQ-003"],
        clarification_refs=["Q-final-audit"],
        provenance=[
            Provenance(type=ProvenanceType.MEMORY_CONFIRMED, ref="M-abc1234567", element="persona"),
            answer_ref("Q-final-audit", "want"),
            answer_ref("Q-final-audit", "benefit"),
        ],
        confidence=0.6,
        status=StoryStatus.EDITED,
    )
    third = Story(
        id="VIS-0001",
        epic="Visibility",
        title="Let the relationship manager see a case",
        persona="Relationship manager",
        want="to see the dispute case",
        benefit="I can support the customer",
        priority=Priority.COULD,
        requirement_ids=["REQ-001"],
        provenance=[
            Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref=rm, element="persona"),
            Provenance(type=ProvenanceType.SCENARIO_EXCERPT, ref=rm, element="want"),
            Provenance(type=ProvenanceType.USER_NOTES, ref="Keep it short", element="benefit"),
        ],
        confidence=0.8,
        status=StoryStatus.APPROVED,
    )
    rejected = third.model_copy(
        update={"id": "VIS-0002", "title": "Drop me", "status": StoryStatus.REJECTED}
    )
    return RunState(
        run_id="run-fixed-0001",
        scenario=Scenario(text=SCENARIO, notes=NOTES, workspace="acme"),
        redacted_text=SCENARIO,
        redacted_notes=NOTES,
        rounds=[QuestionRound(number=1, questions=[question, limits])],
        answers=answers,
        requirements=requirements,
        stories=[first, second, third, rejected],
        go_ahead=True,
        go_ahead_by="user",
    )
