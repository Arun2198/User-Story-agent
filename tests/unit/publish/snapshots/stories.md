# User stories

Run: run-fixed-0001 | Workspace: acme | Stories: 3

## Epic: Disputes (DSP)

### Feature: Raise a dispute

#### DSP-0001: Raise a card dispute

**As a Customer, I want to dispute a card payment within 60 days, so that I get a temporary credit while the bank investigates.**

Priority: must | Estimate: 5 | Confidence: 0.90

Acceptance criteria:
1. Given a card payment made 10 days ago, when the customer raises a dispute, then a temporary credit is posted
2. Given a card payment made 90 days ago, when the customer raises a dispute, then the dispute is refused with the reason

Source:
- Scenario: "expects a temporary credit while the bank investigates"
- Question: How long does the customer have to raise a dispute? Answer: 60 days

Requirements: REQ-001, REQ-002

#### DSP-0002: Keep an audit trail of disputes

**As a Compliance officer, I want every dispute action recorded, so that I can answer an audit.**

Priority: should | Estimate: 3 | Confidence: 0.60

Acceptance criteria:
1. Given a dispute is raised, when its status changes, then the change is recorded with who and when

Non-functional requirements:
- Audit records are kept for 7 years

Assumptions:
- Records are kept for the bank's standard retention period

Open questions:
- Which retention period applies?

Source:
- Confirmed from saved memory (M-abc1234567): No daily claim limit
- Decision on audit: Agent judgment, approved by the user

Requirements: REQ-003

## Epic: Visibility (VIS)

#### VIS-0001: Let the relationship manager see a case

**As a Relationship manager, I want to see the dispute case, so that I can support the customer.**

Priority: could | Confidence: 0.80

Source:
- Scenario: "The relationship manager can see the case"
- Reviewer note: Keep it short

Requirements: REQ-001

## Requirements

| ID | Requirement | Basis |
|---|---|---|
| REQ-001 | Customer expects a temporary credit during the investigation | stated or confirmed |
| REQ-002 | Dispute window is 60 days | stated or confirmed |
| REQ-003 | Disputes are kept in an audit trail | assumed (your judgment) |

## Open questions

- DSP-0002: Which retention period applies?

Not published (rejected): VIS-0002
