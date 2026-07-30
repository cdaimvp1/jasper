# Jasper: A Working Overview

*JASPER — Just Another System for Planning, Execution, & Routing. The closest thing to a project-management system for work that doesn't have one — an overview for anyone deciding whether it's worth adopting*

---

## What Jasper Is

Jasper reads your work mail, the Teams chats tied to it, and your calendar, and turns that scattered stream into one managed picture of your actual work — grounded in your real data every time, never a guess. It doesn't just sort an inbox. It notices when three separate email threads and a Teams conversation are really the same deal, tracks that deal's state over time, summarizes what's happened on it in plain English, and tells you what needs your attention next and why. For a kind of work that doesn't have a real project-management tool behind it today, Jasper is quietly building the closest thing to one — out of the same mail and chat you were already getting, not a new system you have to feed by hand.

It drafts, it doesn't send. It suggests, it doesn't approve. It never auto-deletes a real document. Everything it tells you is either built from your own ingested mail and Teams history, or it says plainly that it doesn't have enough evidence to answer.

---

## The Four Engines Behind It

Four purpose-built pieces do the real work, and it's worth knowing them by name because you'll see them cited directly in how Jasper explains itself:

**Next Best Action** is the ranking behind the Morning Queue. Every open item gets a score built from real signals — how close a deadline is, how long a thread's gone quiet, any dollar value mentioned in it — combined with whatever Total Recall and Aristotle (below) already know about it. The math is fixed and simple, not a model "deciding" behind the scenes, so the ranking is consistent and explainable every time you look at it.

**Socrates** is what answers you when you type a plain question into Back Office. It only ever answers from precedent, existing synthesis, or evidence already linked to your work — no web search, no free-form guessing. When nothing in your own data clears the bar for a confident answer, it says so honestly instead of inventing something plausible-sounding.

**Total Recall** is Jasper's memory of your own corrections. When you confirm or correct how something got classified, that pattern gets more (or less) confident the next time a similar case comes up, and you'll see it cited directly as "precedent" in the reasoning behind a task — not silently applied, always shown.

**Aristotle** is a prerequisite/gate check you teach it: "don't treat a signature request as ready to sign until an approved purchase order has come through for the same deal." From then on, if it sees the first without the second, it flags it — always phrased as *"no confirmation seen yet,"* never as a claim that the approval didn't happen, since Jasper only knows what's actually arrived in your mail. You can add these rules from a dropdown in Settings, explain them in plain English in Back Office chat, or let Jasper propose one on its own after noticing a consistent pattern across multiple deals — but nothing self-activates. Every candidate sits pending until you personally confirm it.

---

## How It Threads Scattered Work Into One Picture

This is the part that's easy to undersell, because it's not one feature — it's what makes everything else possible.

**It recognizes when separate conversations are really one deal.** When two email threads or Teams messages share the same outside contact, the same outside company, or a matching subject with a shared external party, Jasper merges them into a single tracked project automatically — no separate system to maintain, no manual folder-filing. When the signal is weaker (same category, opened close together in time, but no shared external contact yet), it proposes the merge instead of forcing it, and you confirm.

**It summarizes what's actually happened, and where things stand.** Once a project or issue has enough real activity, Jasper writes a short plain-English narrative — who asked what, what's happened since, where it stands now — rather than leaving you to reconstruct that by re-reading a thread. This is one of the few places a real language model does the writing (see "What's Real vs. What's a Model" below); the summary is always shown as exactly that, a written narrative you can check against the evidence it cites, not a black-box conclusion.

**It pulls out the specific asks, decisions, and commitments in a message** — not just "this email is about the Acme renewal," but the actual asks, dates mentioned, decisions made, and commitments given in it. Because this runs against your own mailbox, those asks and commitments are the ones actually directed at you.

Put together, this is the practical answer to "where do I stand on X" without you doing the reconstruction yourself — the same job a project tracker would do, running on mail and chat you were never going to re-enter anywhere else.

---

## How It Helps Day to Day

**The Morning Queue.** Instead of scrolling an inbox trying to guess what's urgent, Jasper hands you a ranked task list, each item scored by Next Best Action with the reason attached in plain language — quiet too long, a deadline close by, real money on the table. A "Now what?" view surfaces the single most pressing item so a busy morning doesn't require reading the whole list first.

**Ask Jasper (in Back Office).** Type a plain question — no special syntax — and Socrates answers from precedent, synthesis, or linked evidence, honestly abstaining when it doesn't have enough.

**Meeting prep, flagged ahead of time.** When a calendar item tied to a live issue is coming up within two weeks, Jasper flags it as worth a pre-read rather than letting it arrive unprepared-for. Today that's a heads-up and a reason, not an auto-written document — see the roadmap section for where this is headed once skills are connected.

**Jump straight to the source.** A direct link that opens the exact Teams chat, opens the exact email in Outlook, or (for a recognized DocuSign/Adobe Sign/Ariba request) opens the vendor's own page — instead of you digging back through search results.

**Real draft replies, always for your review.** A real Outlook draft — addressed, quoting the original thread, ready in a normal Outlook window — using Outlook's own Reply/Reply-All. It never calls Send.

---

## Managing a Team of AI Workers, Not Just an Inbox

Back Office isn't only where you ask Jasper questions — it's also where you direct a small cohort of Claude-based workers (each with its own name and lane) by @mentioning them, the same way you'd message a colleague. Each worker reports its own current status, so you can see what it's actively doing rather than guessing. Worth being honest about the shape of this: a worker you address is a genuinely separate, semi-autonomous process, not an instant chatbot — it picks up your message on its own schedule, not necessarily the moment you send it.

---

## The Learning Loops — It Gets Better Specifically for How *You* Work

Four separate mechanisms all point the same direction: the longer you use Jasper, the more it fits your actual work, not a generic model of "how mail triage should work."

- **Total Recall** remembers every correction you've made and reapplies that judgment to the next similar case.
- **Aristotle** accumulates the process rules you've taught it, and proposes new candidates on its own once a pattern's held consistently across multiple real deals.
- **Next Best Action** folds Total Recall's precedent directly into its scoring, so a well-worn pattern visibly moves an item's priority, not just its explanation.
- **Personal Response Learning** (optional, off by default) studies which systems and task-verbs *you* reach for in your own messages — never anyone else's — and cites that back as a "you've done this before" note, the same restrained, cited-not-applied treatment Total Recall gets. Three separately toggleable sources (in-app chat, sent email, sent Teams), with a one-click reset.

None of these four ever silently rewrites anything on your behalf — they all surface as a citation or a proposal you can see and, where relevant, confirm.

---

## Where a Model Is Actually Involved

Everything in this document is built and working today — this section isn't about what's real versus theoretical (see the separate Roadmap section below for that distinction). It's about *how* each piece works: almost all of it is deterministic — fixed arithmetic and rules, not a model "deciding" — and exactly three places genuinely use a language model instead:

- **Synthesis** (the plain-English "what's happened" narrative) and **extraction** (asks/decisions/commitments pulled from a message) are written by a real Claude-based worker, not a template.
- **Chat-based rule teaching** (`#addrule` in Back Office) runs a local, on-device language model to try to structure your plain-English sentence into a formal rule — see the limitations section below for exactly how honest it is when it isn't confident.

Everywhere else — the Morning Queue ranking, Aristotle's rule-checking, Total Recall's matching, project auto-grouping, Personal Response Learning's pattern-mining — is fixed logic with no model involved, which is exactly why those parts are consistent and fully explainable every time.

---

## What Jasper Deliberately Does NOT Do

- **It never sends an email or takes an action on your behalf.** Every reply is a draft you review in a real Outlook window before anything happens to it.
- **It never approves anything.** Approval chains and prerequisite flags are shown for context; the decision is always yours.
- **It never auto-deletes a real document.** Retention settings apply only to operational data — logs, diagnostic history — never to supplier contracts or anything resembling a real business record, regardless of any setting.
- **It doesn't guess when it doesn't know.** If there isn't enough grounded evidence in your own data to answer a question confidently, Jasper says so rather than fabricating a plausible-sounding answer.

**Honest limitations, beyond the "won't":**

- **It's built around one person's mailbox and workflow today.** It isn't yet a multi-person or team-wide tool.
- **Chat-based rule teaching is new and best-effort.** When the local language-structuring step is confident, it fills the rule in for you to confirm; when it isn't, it says so honestly and logs your raw explanation for you to structure yourself, rather than pretending it understood something it didn't.
- **Personal Response Learning is optional and starts off.** Nothing about "sounding more like you" happens unless you explicitly turn it on, and only for the sources you enable.
- **Meeting prep is a flag today, not a finished document** — see the roadmap below for where that's headed.
- **Some visible features aren't connected yet.** A "My Work" view exists in the interface but is intentionally disabled — it needs access to systems (Ariba, SAP, Aravo, ServiceNow, ContractPodAI) this install doesn't have yet. The tab itself says so.

---

## On the Roadmap — Not Yet Researched, Designed, or Built

Everything above is real and working today. This section is deliberately kept separate, because none of it is: the plan is to connect Jasper to a real library of ~30 existing Claude-based skills (contract review, negotiation prep, financial analysis, and more), so that once Jasper has already gathered the relevant material — say, a supplier's returned work order, attachment already in hand — it can suggest running the specific skill that fits, and you simply say go. The pattern this would extend already exists today (Jasper already suggests actions and waits for your go-ahead elsewhere in the app), but the skills connection itself has not been built, wired in, or even designed yet. Treat it as direction, not a promise.

---

## Time Savings: A Reasonable Expectation, Not a Measured Result

There's no stopwatch study behind this section — no measured "X hours saved per week" number exists, and it would be dishonest to invent one. What follows is a plain, reasoned estimate of *where* time would plausibly be saved, based on what Jasper actually does:

- **Not manually reconstructing where a deal stands.** Instead of re-reading three scattered threads to remember what's happened, you start from a synthesized summary and a current state that's already been tracked.
- **Not manually scanning an inbox to figure out what's urgent.** You start from a ranked list with reasons already attached, instead of reading top to bottom.
- **Not switching between Outlook, Teams, and vendor portals to find the right thread.** Deep links and one-click actions cut out the searching-and-re-finding step that normally happens every time you want to act on something.
- **Not re-explaining context to yourself every time.** Total Recall's precedent and Aristotle's taught rules mean you're not starting from zero on a recurring type of item.
- **Not manually cross-checking whether a prerequisite was actually satisfied.** A taught rule does that check automatically and flags it.

How much this adds up to will vary a lot by mail volume and how efficient your existing workflow already is. The honest framing: Jasper removes several specific, recurring, low-judgment steps — searching, re-finding, reconstructing, re-checking — and lets you spend that time on the actual decision instead. Treat any specific number as a guess until it's actually been measured against real before/after usage.
