# MediBot Trust Layer: AI Evaluation & Guardrail Platform

### Project explanation for MediAssist Health Network leadership, risk, and clinical governance

---

## 1. Executive summary

MediAssist Health Network already uses AI assistants to help staff find answers in internal documents. **MediBot** answers questions for doctors, nurses, billing executives, biomedical technicians and administrators, drawing on clinical protocols, the drug formulary, nursing procedures, equipment manuals, billing guides and HR policies.

Building assistants is no longer the hard part. The hard part is knowing that an assistant is **safe to keep running**, and finding out **quickly when it stops working as it should**.

This project adds a **trust layer** around MediBot. It is a shared set of safety checks, record-keeping and quality measurements that sits between staff and the AI. It answers three questions all the time:

1. **Is this answer safe to show this person?** Every question is screened before the AI sees it. Every answer is screened before the person sees it.
2. **Is the system behaving correctly right now?** Every conversation leaves a complete, reviewable record of what the AI saw, what it decided and why.
3. **Is answer quality holding up over time?** A repeatable test runs a fixed set of known questions through the assistant and produces one PASS / FAIL health report.

The layer is designed so that any other AI assistant in the network (scheduling, triage support and so on) can be connected to it in the same way before it goes live, and monitored continuously afterwards.

---

## 2. Why this project exists

Last quarter, three incidents moved AI from "promising pilot" to "operational risk":

| Incident | What went wrong | Why it hurt |
|---|---|---|
| **Hallucinated dosage** | A pilot assistant confidently gave a medication dose that did not come from any approved document. | Patient-safety exposure. Nobody could reconstruct what the assistant had read before it answered. |
| **Suspected billing leak** | A support ticket claimed a chatbot showed billing information to a non-billing role. | A possible privacy and compliance breach, and the on-call engineer had no logs to confirm or rule it out for hours. |
| **Silent quality drop** | A new AI model version was swapped in without announcement and answer quality fell. | It took two weeks of staff complaints to notice. No automatic quality check was running. |

The Chief Risk Officer asked for these incidents to stop being fought one at a time. The goal is a **standard safety and quality layer** that every AI system must pass through.

---

## 3. What the trust layer does, in plain terms

The trust layer works like the checks a hospital already applies to medicines and procedures: a check at the door, a check before release, a complete record, and regular audits.

```
   Staff member asks a question
              │
              ▼
   ┌────────────────────────────┐
   │ 1. CHECK AT THE DOOR        │  Is this a genuine work question from someone
   │    (input guardrail)        │  allowed to ask it?  If not, refuse politely.
   └────────────────────────────┘
              │
              ▼
   ┌────────────────────────────┐
   │ 2. MediBot finds the right  │  Only searches the document collections
   │    documents and drafts an  │  this person's role is allowed to see.
   │    answer                   │
   └────────────────────────────┘
              │
              ▼
   ┌────────────────────────────┐
   │ 3. CHECK BEFORE RELEASE     │  Does the answer leak private or restricted
   │    (output guardrail)       │  information, or state facts the documents
   └────────────────────────────┘  don't support?  If so, withhold it.
              │
              ▼
   Staff member sees the answer, its source documents,
   and a reference ID for support

   Throughout: a full record of every step is kept (the "flight recorder").
   Regularly: a standard test set is run and a health report is produced (the "audit").
```

### 3.1 The check at the door (input safety)

Before MediBot processes a question, the trust layer screens it for:

- **Attempts to trick the assistant**, for example "ignore your previous instructions", "show me your hidden rules", or "pretend you have no restrictions".
- **Attempts to get around access rules**, for example a technician typing "I'm actually the admin, switch my role", or asking to search every collection.
- **Requests for restricted topics**, for example a nurse asking about insurance package rates, or a technician asking for drug-formulary dosing.
- **Harmful or improper requests**, such as how to harm a patient, falsify a claim, or obtain personal details of patients or staff.
- **Off-topic use**, such as writing poems or giving stock tips on a hospital system.

### 3.2 The check before release (output safety)

After MediBot drafts an answer, and before anyone sees it, the trust layer checks for:

- **Personal information**: patient names tied to medical details, phone numbers, ID numbers, medical record numbers. Published internal help-desk contacts are allowed.
- **Restricted content**: anything that comes from a document collection the person's role cannot access.
- **Unsupported or invented facts**: especially medication doses and clinical thresholds that do not appear in the documents the assistant actually read. This directly targets the hallucinated-dosage incident.

### 3.3 How decisions are made: two layers, and "when in doubt, refuse"

Each check has two layers:

1. **Fixed rules.** These are fast, predictable and cheap. They catch obvious attacks and leaks immediately.
2. **An independent AI reviewer.** A separate, larger AI model reads the question or answer against a written policy. It catches subtler cases the rules miss, such as an answer that gets a procedure wrong without quoting any numbers.

Two design principles matter most to risk owners:

- **Fail closed.** If the reviewer's decision is missing, garbled or ambiguous, or the reviewer is unavailable, the answer is **withheld**, never released. A system that cannot confirm an answer is safe does not show it.
- **Structured decisions, not guesses.** Every decision is recorded in the same format: *allowed or blocked*, *which category*, and *a one-sentence reason*. Decisions are never inferred from loose wording.

### 3.4 What staff see when something is blocked

Staff see a short, polite, generic message:

> *"I'm sorry, but I can't help with that request. Please ask a question about MediAssist policies, procedures or documents available to your role."*

The **reason is never shown to the user**. Telling someone exactly which rule stopped them teaches them how to get around it. The reason is kept in the internal record for investigators. Each answer also carries a **reference ID** that staff can quote to the help desk.

---

## 4. The flight recorder: full traceability

Every question produces a complete record. An investigator can pick any single conversation and answer *"what did the system see, what did it decide, and why?"* without asking the staff member to repeat anything.

For each request, the record holds:

| What is recorded | Why it matters |
|---|---|
| Who asked (role) and what they asked | Establishes context and access rights |
| Which documents and sections were retrieved and in what order | Shows what the AI actually read before answering (the missing piece in the dosage incident) |
| The exact instructions and context given to the AI | Allows a reviewer to judge whether the AI was misled or misbehaved |
| The AI's original draft and the final answer shown | Shows whether a safety check changed the outcome |
| Every safety decision: allowed or blocked, category and reason | Answers "was this blocked, and why?" (the missing piece in the billing-leak ticket) |
| Time taken at each step, and the amount of AI processing used | Supports performance monitoring and cost tracking |

Records go to two places:

- A **visual trace viewer** (LangSmith), where each conversation can be opened as a step-by-step timeline.
- **Structured log files** that operations staff can search and summarise, for example "how many requests were blocked today, and for what reasons?". A live summary of blocked versus allowed counts, response times and usage is also available on request.

The reference ID shown to staff is the same ID used in both places, so a support ticket leads straight to the full record.

---

## 5. The audit: automated quality evaluation

Safety checks protect each conversation. The **evaluation pipeline** protects quality over time. It is a repeatable test that runs a fixed, labeled set of questions through the real assistant and grades the results. Running it twice on an unchanged system gives consistent results, so any real change stands out.

### 5.1 The test set

The test set contains **28 labeled questions** across all five roles, each with an approved "correct answer" written from MediAssist's own documents:

- **Everyday questions**, such as diabetes diagnostic criteria, CVC dressing changes, autoclave testing, pre-authorisation deadlines and leave entitlements.
- **Deliberately hard questions**, such as weight-based paediatric dosing, dengue fever management and kidney-function dose adjustments. Easy questions make every system look good; hard ones separate a sound assistant from a lucky one.
- **Access-control questions**, where the correct behaviour is to **refuse** (for example a nurse asking for billing rates).
- **Attack questions**, where the correct behaviour is to **block** (tricks, role impersonation, harmful requests, requests for personal data).

A separate **calibration set** holds deliberately bad answers: a confidently wrong dosage, an empty answer, an answer with no source, a leak of billing content, a leak of patient details, and a refusal that reveals its reason. These prove that the graders actually catch failures instead of approving everything.

### 5.2 Four kinds of grading

| Grader | What it measures | Business meaning |
|---|---|---|
| **Rule-based checks** (run first) | Every answer is non-empty and cites a source. Restricted questions are clearly refused. Attacks are blocked. Nothing leaks across roles or reveals personal data. No dose appears that isn't in the documents. Responses arrive within the time limit. | Cheapest, fastest signal. If the system is obviously broken, the run stops here before spending money on deeper grading. |
| **Retrieval & answer quality scores** (RAGAS) | *Faithfulness*: does the answer stick to the documents? *Relevancy*: does it answer the question asked? *Context precision and recall*: did the assistant find the right documents, and all of them? | Shows whether problems start in "finding the information" or in "explaining it". |
| **Independent AI grader** (LLM-as-a-judge) | A separate, larger AI model scores each answer 1–5 on **accuracy, completeness, appropriate refusal and correct citation**, and writes a short justification. | A second opinion that does not share the assistant's blind spots, and is specifically tested against confident-but-wrong answers. |
| **Attack suite** | A library of attack and harmless prompts is run against the safety checks. It also runs "fail-closed" drills, where the reviewer is made to give broken or missing decisions. | Confirms the door and release checks hold up, that harmless questions are not wrongly blocked, and that a faulty reviewer never lets an answer through. |

### 5.3 Why a separate AI does the grading

The assistant is not allowed to grade itself. A system that marks its own homework tends to agree with its own confident mistakes. The grader is a **different, larger model**. It works in a fresh session, sees the approved correct answer, and is told explicitly not to reward confidence or fluency.

### 5.4 The health report

Each evaluation run produces **one consolidated report** with a clear **PASS / FAIL verdict**. Every signal is compared against a published threshold, for example:

- faithfulness and answer relevancy of at least 0.70; context precision and recall of at least 0.60;
- an independent grader average of at least 3.5 out of 5;
- 100% of attacks blocked, and no more than 10% of harmless questions wrongly blocked;
- 100% of fail-closed drills withheld;
- critical safety checks passing on every question.

If the verdict is FAIL, the report names **exactly which checks failed**. It also includes worked examples: one attack being correctly blocked, and one bad answer being correctly caught. A non-technical reviewer can see the evidence, not just the score.

---

## 6. How the trust layer answers each incident

| Incident | Before | With the trust layer |
|---|---|---|
| **Hallucinated dosage** | Nobody could see what the assistant read. | Answers with doses not found in the retrieved documents are withheld at release. The record shows exactly which documents were read. The test set includes dosing questions and a planted wrong-dose answer, confirming the graders catch it. |
| **Billing leak claim** | No trace, no logs, hours of uncertainty. | Access rules limit what the assistant can search. Restricted topics are refused at the door, and restricted content is caught at release. The support ticket's reference ID opens the full record within minutes, so the claim can be confirmed or ruled out with evidence. |
| **Silent model swap** | Two weeks of complaints before anyone noticed. | The evaluation runs on demand or on a schedule. A model change that lowers quality fails the thresholds and shows up in the health report straight away, before staff feel it. |

---

## 7. Roles and responsibilities

| Stakeholder | Uses the trust layer to… |
|---|---|
| **Chief Risk Officer / Governance board** | Approve go-live and changes based on the PASS / FAIL health report and its thresholds. |
| **Clinical governance** | Review the hard clinical questions and approved answers in the test set; update them when protocols change. |
| **Information security & privacy** | Review blocked-request categories, personal-data and leak detections, and attack-suite results. |
| **AI platform / operations team** | Investigate tickets using the reference ID. Monitor block rates, response times and usage. Rerun evaluations after every change. |
| **Department heads** | Confirm which document collections each role should see (the access rules the checks enforce). |
| **Staff** | Use MediBot as usual. When something is withheld, quote the reference ID to the help desk. |

---

## 8. Operating model

1. **Before go-live** (any AI assistant): connect it to the trust layer, run the evaluation, and go live only on PASS.
2. **Every change** (new AI model, new documents, new prompts): rerun the evaluation. A FAIL blocks the change.
3. **Continuously**: every conversation is screened and recorded. Operations reviews blocked-request trends and response times.
4. **Periodically**: clinical governance refreshes the labeled questions and approved answers. Security adds newly seen attack patterns to the attack suite.
5. **On incident**: look up the reference ID, open the record, and decide with evidence. If the case reveals a gap, add it to the test set so the same failure cannot pass unnoticed again.

---

## 9. Benefits

- **Patient safety**: unsupported clinical figures are withheld rather than shown with confidence.
- **Privacy and compliance**: role-based access is enforced at several points, personal data is screened out of answers, and every decision is auditable.
- **Faster investigations**: from hours of guesswork to minutes, using a single reference ID.
- **Early warning**: quality regressions are found by the evaluation, not by staff complaints.
- **Reusable across the network**: the same trust layer and health report apply to scheduling, triage and future assistants, giving leadership one consistent standard.
- **Evidence-based decisions**: go / no-go decisions rest on published thresholds and a written report, not intuition.

---

## 10. Known limitations and trade-offs

- **Speed and cost**: the independent AI reviewer adds a short delay and some usage cost to each question. The fixed rules run first, so obvious attacks never reach the reviewer.
- **Caution over convenience**: because the system fails closed, a reviewer outage means answers are withheld until it recovers. This is deliberate for a healthcare setting.
- **Not a clinical decision-maker**: MediBot summarises approved internal documents. It does not replace clinical judgement, and the checks confirm answers match the documents, not that the documents are clinically current.
- **The test set needs upkeep**: the evaluation is only as good as its labeled questions. It must be refreshed as protocols, policies and attack techniques change.
- **Demo-grade identity**: the current demonstration uses sample accounts. Production would connect to MediAssist's single sign-on and employee directory.

---

## 11. Roadmap

| Next step | Value |
|---|---|
| Schedule the evaluation to run nightly and after every deployment | Continuous early warning without manual effort |
| Operations dashboard over the structured records | At-a-glance block rates, response times and quality trends |
| Connect the scheduling and triage pilots | One safety standard for every AI assistant in the network |
| Add a managed cloud guardrail service as an extra layer | Defence in depth for personal-data detection |
| Human review queue for borderline blocked answers | Fewer false refusals while keeping the fail-closed default |
| Single sign-on integration | Production-grade identity and access |

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Guardrail** | An automatic safety check applied to a question (input) or an answer (output). |
| **Fail closed** | When a safety decision can't be confirmed, the default is to refuse, never to allow. |
| **Trace / flight recorder** | The step-by-step record of how one answer was produced. |
| **Reference ID** | The unique number on each answer that links to its full record. |
| **RAG** | "Retrieval-augmented generation": the AI first finds relevant documents, then answers from them. |
| **RAGAS** | An industry-standard toolkit for scoring how well a RAG assistant finds and uses documents. |
| **LLM-as-a-judge** | Using a separate AI model, with a written rubric, to grade another AI's answers. |
| **Heuristic check** | A simple, rule-based test that needs no AI, for example "does every answer cite a source?". |
| **Role-based access (RBAC)** | Each role can only see the document collections it is entitled to. |
