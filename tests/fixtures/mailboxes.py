"""Realistic mailbox fixtures for triage acceptance.

Two mailboxes with deliberately different distributions, because a rule tuned
around one sample is not intelligence. Each message carries a `truth` label
used only for scoring — it is never shown to the model.

The adversarial half is the point. Every "important" message here wears the
costume of the marketing mail beside it: a bulk sender, a templated subject, a
list-unsubscribe header, a no-reply address. A classifier that reads those
signals as a verdict will sweep up a payment receipt and a security alert, and
that is exactly the failure this corpus exists to catch.
"""

from __future__ import annotations

from app.backend.mail import Message

BULK_HEADERS = {"List-Unsubscribe": "<https://example.test/u>", "Precedence": "bulk"}


def _message(
    identifier: str,
    sender: str,
    subject: str,
    snippet: str,
    truth: str,
    *,
    bulk: bool = False,
    body: str = "",
) -> tuple[Message, str]:
    return (
        Message(
            identifier=identifier,
            sender=sender,
            subject=subject,
            snippet=snippet,
            headers=dict(BULK_HEADERS) if bulk else {},
            body=body,
        ),
        truth,
    )


# --------------------------------------------------------------- mailbox one
# A student's mailbox: university, a bank, shopping, and a lot of marketing.

_STUDENT = [
    # --- genuinely low value -------------------------------------------------
    _message(
        "s1", "offers@fashionhouse.test", "70% OFF everything — 48 hours only!",
        "Our biggest sale of the season ends Sunday. Shop now and save.",
        "low_value", bulk=True,
    ),
    _message(
        "s2", "deals@fashionhouse.test", "70% OFF everything — final hours",
        "Last chance. Sale ends midnight.", "low_value", bulk=True,
    ),
    _message(
        "s3", "news@gadgetweekly.test", "This week in gadgets: foldables, again",
        "Plus: the best budget earbuds we tested this month.",
        "low_value", bulk=True,
    ),
    _message(
        "s4", "no-reply@socialapp.test", "See what you missed this week",
        "12 people posted while you were away. Come back and catch up.",
        "low_value", bulk=True,
    ),
    _message(
        "s5", "promotions@foodpanda.test", "Hungry? 40% off your next 3 orders",
        "Use code EAT40 at checkout. Minimum spend applies.",
        "low_value", bulk=True,
    ),
    # --- adversarial: looks exactly like the above, must not be touched ------
    _message(
        "s6", "no-reply@daraz.test", "Your order #DZ-88421 has been shipped",
        "Your parcel is on its way and should arrive Thursday. Track it here.",
        "important", bulk=True,
        body="Order DZ-88421. Item: USB-C charger. Delivery estimate Thursday.",
    ),
    _message(
        "s7", "alerts@brac-bank.test", "Unusual sign-in to your account",
        "We noticed a login from a new device. If this was not you, act now.",
        "important", bulk=True,
        body="A sign-in from Chrome on Windows was recorded at 02:14.",
    ),
    _message(
        "s8", "billing@cloudhost.test", "Your invoice for August is ready",
        "Invoice #4471 for BDT 1,250 is attached. Payment due 5 September.",
        "important", bulk=True,
    ),
    _message(
        "s9", "noreply@university.test", "Semester registration closes Friday",
        "Students who have not registered by Friday will be charged a late fee.",
        "important", bulk=True,
    ),
    _message(
        "s10", "no-reply@github.test", "Verify your new email address",
        "Confirm this address to finish adding it to your account.",
        "important", bulk=True,
    ),
    _message(
        "s11", "careers@techfirm.test", "Update on your application",
        "Thanks for interviewing with us. We would like to arrange a second call.",
        "important", bulk=True,
    ),
    # --- plainly personal ----------------------------------------------------
    _message(
        "s12", "rahim.chowdhury@gmail.test", "notes from today",
        "Hey — here are the notes you missed. Call me if anything is unclear.",
        "important",
    ),
    _message(
        "s13", "supervisor@university.test", "Re: thesis draft chapter 2",
        "I read the draft. Section 2.3 needs more evidence before we submit.",
        "important",
    ),
]

# --------------------------------------------------------------- mailbox two
# A freelancer's mailbox: a different shape entirely. Mostly transactional,
# fewer adverts, and newsletters the person clearly engages with.

_FREELANCER = [
    _message(
        "f1", "noreply@invoiceapp.test", "Invoice #221 was paid",
        "Acme Ltd paid BDT 42,000. Funds arrive in 2 working days.",
        "important", bulk=True,
    ),
    _message(
        "f2", "no-reply@stripe.test", "Your payout is on the way",
        "A payout of BDT 18,300 has been initiated to your bank account.",
        "important", bulk=True,
    ),
    _message(
        "f3", "client@acme.test", "Re: revised scope for the landing page",
        "Looks good. Can we add one more section before Friday?",
        "important",
    ),
    _message(
        "f4", "hello@designletter.test", "Issue 214 — grids that actually work",
        "This week: a practical look at baseline grids, plus three teardowns.",
        "low_value", bulk=True,
    ),
    _message(
        "f5", "marketing@stockphotos.test", "Your free trial is ending — upgrade now",
        "Upgrade in the next 3 days and get 30% off your first year.",
        "low_value", bulk=True,
    ),
    _message(
        "f6", "marketing@stockphotos.test", "Your free trial is ending — last call",
        "Final reminder: upgrade today and keep your downloads.",
        "low_value", bulk=True,
    ),
    _message(
        "f7", "noreply@domains.test", "Your domain renews in 14 days",
        "acme-portfolio.test renews automatically on 29 August for BDT 1,400.",
        "important", bulk=True,
    ),
    _message(
        "f8", "no-reply@taxauthority.test", "Filing deadline reminder",
        "Your annual return is due within 21 days. Late filing carries a penalty.",
        "important", bulk=True,
    ),
]


def student_mailbox() -> tuple[list[Message], dict[str, str]]:
    return [item for item, _ in _STUDENT], {
        item.identifier: truth for item, truth in _STUDENT
    }


def freelancer_mailbox() -> tuple[list[Message], dict[str, str]]:
    return [item for item, _ in _FREELANCER], {
        item.identifier: truth for item, truth in _FREELANCER
    }


def score(
    verdicts: dict[str, object], truth: dict[str, str]
) -> dict[str, object]:
    """Precision, recall, and the number that actually matters.

    A false positive here is a receipt or a security alert filed away as junk.
    It is weighted differently from a missed advert because it is a different
    kind of mistake, and the report keeps them apart rather than blending them
    into one score.
    """

    swept: list[str] = []
    for identifier, verdict in verdicts.items():
        if getattr(verdict, "actionable", False):
            swept.append(identifier)

    false_positives = [item for item in swept if truth.get(item) == "important"]
    true_positives = [item for item in swept if truth.get(item) == "low_value"]
    all_low = [key for key, value in truth.items() if value == "low_value"]
    missed = [item for item in all_low if item not in swept]
    return {
        "swept": len(swept),
        "true_positives": len(true_positives),
        "false_positives": len(false_positives),
        "false_positive_ids": false_positives,
        "missed": len(missed),
        "precision": (len(true_positives) / len(swept)) if swept else 1.0,
        "recall": (len(true_positives) / len(all_low)) if all_low else 1.0,
    }
