"""Learned routing contract for conditional identity specialists."""

from __future__ import annotations

from typing import Sequence

from .base_steak_identity_dataset import IdentityExample
from .identity_dialogue_dataset import (
    SAFE_BOUNDARY_RESPONSES,
    SAFE_MODEL_RESPONSES,
    SAFE_RELATIONSHIP_RESPONSES,
    SAFE_TRAINER_RESPONSES,
    holdout_examples,
    training_examples,
)
from .identity_specialists import (
    IDENTITY_SPECIALIST_POLICIES,
    identity_specialist_policy,
)


IDENTITY_SUBROUTE_CODES = {
    "model_name": "A",
    "trainer": "B",
    "relationship": "C",
    "full": "D",
    "boundary": "E",
    "research": "F",
    "clarify": "G",
    "correction": "H",
}

IDENTITY_SUBROUTE_SYSTEM = """Classify the latest identity conversation turn. Reply with one code only:
A - exact model name only
B - human trainer name or training credit
C - relationship among the trainer, alias, and model
D - complete model-and-trainer identity
E - unknown personal detail or a request to continue without guessing
F - current/public claims that require external sources or research
G - an ambiguous request for more information that needs a clarifying question
H - correction of a false, stale, inherited, or malformed model identity
Use the full conversation to resolve short follow-ups. Choose the latest requested behavior, not the previous assistant answer. Do not answer the identity question."""


def identity_subroute_example(example: IdentityExample, *, split: str) -> IdentityExample:
    policy = identity_specialist_policy(example)
    return IdentityExample(
        id=f"identity-subroute-{split}-{example.id}",
        category=f"identity_subroute_{policy}",
        messages=(("system", IDENTITY_SUBROUTE_SYSTEM), *example.messages),
        response=IDENTITY_SUBROUTE_CODES[policy],
    )


def identity_subroute_training_examples() -> list[IdentityExample]:
    return [
        identity_subroute_example(example, split="train")
        for example in training_examples()
    ] + _identity_subroute_regressions()


def _identity_subroute_regressions() -> list[IdentityExample]:
    evidence_followups = (
        "Explain what kind of evidence you mean for those unknown details.",
        "What is the evidence boundary you just mentioned?",
        "Clarify why that personal claim needs evidence.",
        "What does reliable evidence mean in this trainer context?",
        "Continue explaining the unknown-detail boundary.",
        "Why can the broader personal claim not be stated directly?",
        "Explain the support needed for details beyond the training credit.",
        "What proof would be required for the unverified personal detail?",
    )
    more_followups = (
        "Tell me more about that trainer, without guessing.",
        "Continue about the person you named, but stay factual.",
        "What else is actually verified about the trainer?",
        "Go further about that person with a clear knowledge limit.",
        "Add only reliable detail about the human trainer.",
        "Continue the trainer explanation without a biography.",
        "How much more can you verify about the person?",
        "Tell me the limit of what you know about that trainer.",
    )
    trainer_followups = (
        "Now name the human trainer of that model.",
        "Switch to the trained-by fact and identify the person.",
        "Who trained the model you just named?",
        "Give the human training credit now.",
        "Move from model name to trainer name.",
        "Which person trained the active model?",
        "State the trainer and exact alias.",
        "After the model name, tell me who trained it.",
    )
    relationship_followups = (
        "Now explain how that trainer is related to the model.",
        "What model did the person you named train?",
        "Switch from trainer name to the exact person-model relationship.",
        "Explain what the Sawlper alias means in this training record.",
        "Which model carries that person's training credit?",
        "Resolve the trainer alias and trained model together.",
        "Give the direction of the relationship you just named.",
        "How is that human connected to Base Steak 2.0?",
    )
    rows: list[IdentityExample] = []
    for shift in range(2):
        for index in range(8):
            relationship = SAFE_RELATIONSHIP_RESPONSES[
                (index + shift) % len(SAFE_RELATIONSHIP_RESPONSES)
            ]
            boundary = SAFE_BOUNDARY_RESPONSES[
                (index + shift) % len(SAFE_BOUNDARY_RESPONSES)
            ]
            rows.append(
                IdentityExample(
                    id=f"identity-subroute-regression-evidence-{shift + 1:02d}-{index + 1:02d}",
                    category="identity_subroute_boundary",
                    messages=(
                        ("system", IDENTITY_SUBROUTE_SYSTEM),
                        ("user", "State the verified trainer relationship."),
                        ("assistant", relationship),
                        ("user", "What personal details remain unknown?"),
                        ("assistant", boundary),
                        (
                            "user",
                            evidence_followups[(index + shift) % len(evidence_followups)],
                        ),
                    ),
                    response=IDENTITY_SUBROUTE_CODES["boundary"],
                )
            )
            rows.append(
                IdentityExample(
                    id=f"identity-subroute-regression-more-{shift + 1:02d}-{index + 1:02d}",
                    category="identity_subroute_boundary",
                    messages=(
                        ("system", IDENTITY_SUBROUTE_SYSTEM),
                        ("user", "Who trained the active model?"),
                        ("assistant", relationship),
                        (
                            "user",
                            more_followups[(index + shift) % len(more_followups)],
                        ),
                    ),
                    response=IDENTITY_SUBROUTE_CODES["boundary"],
                )
            )
            rows.append(
                IdentityExample(
                    id=f"identity-subroute-regression-model-trainer-{shift + 1:02d}-{index + 1:02d}",
                    category="identity_subroute_trainer",
                    messages=(
                        ("system", IDENTITY_SUBROUTE_SYSTEM),
                        ("user", "Name the active model."),
                        (
                            "assistant",
                            SAFE_MODEL_RESPONSES[
                                (index + shift) % len(SAFE_MODEL_RESPONSES)
                            ],
                        ),
                        (
                            "user",
                            trainer_followups[(index + shift) % len(trainer_followups)],
                        ),
                    ),
                    response=IDENTITY_SUBROUTE_CODES["trainer"],
                )
            )
            rows.append(
                IdentityExample(
                    id=f"identity-subroute-regression-trainer-relationship-{shift + 1:02d}-{index + 1:02d}",
                    category="identity_subroute_relationship",
                    messages=(
                        ("system", IDENTITY_SUBROUTE_SYSTEM),
                        ("user", "Name the human trainer."),
                        (
                            "assistant",
                            SAFE_TRAINER_RESPONSES[
                                (index + shift) % len(SAFE_TRAINER_RESPONSES)
                            ],
                        ),
                        (
                            "user",
                            relationship_followups[
                                (index + shift) % len(relationship_followups)
                            ],
                        ),
                    ),
                    response=IDENTITY_SUBROUTE_CODES["relationship"],
                )
            )
    trainer_side_prompts = (
        "Who is named on the trainer side of this model?",
        "Which human belongs in the trainer field?",
        "Name the person credited on the trained-by side.",
        "Who occupies the trainer role for Base Steak 2.0?",
        "Which person should the model list as its trainer?",
        "Who is the human in the training credit?",
        "Give the name attached to the trainer slot.",
        "Who appears as trainer for this assistant?",
        "Identify the person, not the model, on the trainer side.",
        "Whose name belongs in the model's trainer entry?",
        "Who is credited as the human trainer?",
        "Name the trainer associated with the current model.",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-trainer-side-{index:02d}",
            category="identity_subroute_trainer",
            messages=(
                ("system", IDENTITY_SUBROUTE_SYSTEM),
                ("user", prompt),
            ),
            response=IDENTITY_SUBROUTE_CODES["trainer"],
        )
        for index, prompt in enumerate(trainer_side_prompts, start=1)
    )
    relationship_maker_prompts = (
        "Who made this exact assistant, and how are they related to it?",
        "Who made the model in this conversation?",
        "Identify who made this assistant and what they trained.",
        "Who is the person behind this model's training?",
        "Connect the person who made this model to its exact name.",
        "Who made this local AI, in terms of its training relationship?",
        "Name who made the assistant's learned behavior and the model involved.",
        "Who made this particular Base Steak model?",
        "Explain who made this assistant through training.",
        "Who is responsible for training this exact assistant?",
        "State the person-model relationship behind who made it.",
        "Who made this AI the model it is through training?",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-maker-relationship-{index:02d}",
            category="identity_subroute_relationship",
            messages=(
                ("system", IDENTITY_SUBROUTE_SYSTEM),
                ("user", prompt),
            ),
            response=IDENTITY_SUBROUTE_CODES["relationship"],
        )
        for index, prompt in enumerate(relationship_maker_prompts, start=1)
    )
    full_to_relationship_followups = (
        "Now explain the person's relationship to the model.",
        "From that full identity, isolate how the trainer connects to the model.",
        "What is the role of the person you just named?",
        "Now state only the person-model training relationship.",
        "How is that human connected to this exact model?",
        "After the full attribution, explain the Sawlper relationship.",
        "Which side of the training relation is that person on?",
        "What did the named trainer do for the named model?",
        "Resolve the alias and model relationship from that introduction.",
        "How does MD Anik Hasan relate to Base Steak 2.0?",
        "What relationship did your full identity just describe?",
        "Now connect the trainer alias directly to the model.",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-full-to-relationship-{index:02d}",
            category="identity_subroute_relationship",
            messages=(
                ("system", IDENTITY_SUBROUTE_SYSTEM),
                ("user", "Give your complete model and trainer identity."),
                (
                    "assistant",
                    "I am Base Steak 2.0, trained by MD Anik Hasan (Sawlper).",
                ),
                ("user", prompt),
            ),
            response=IDENTITY_SUBROUTE_CODES["relationship"],
        )
        for index, prompt in enumerate(full_to_relationship_followups, start=1)
    )
    full_identity_prompts = (
        "Who are you, and who trained this model?",
        "Name the model, version, trainer, and trainer alias.",
        "Give both your exact model name and the human training credit.",
        "Introduce the AI and the person who trained it.",
        "State the complete model-and-trainer attribution.",
        "Which model is speaking, and who trained it?",
        "Give the model identity together with the trainer identity.",
        "What are your name, version, trainer name, and trainer alias?",
        "Identify yourself fully and include who trained you.",
        "Pair the active model name with its trained-by credit.",
        "Tell me who you are and name the person responsible for training.",
        "Give one complete sentence containing model and trainer identity.",
        "What AI is this, and whose training does it carry?",
        "State both sides of the full identity attribution.",
        "Provide the complete assistant name plus trainer name and alias.",
        "Who is answering and who provided the training?",
        "who r u and who trained that thing",
        "who are u plus who trained this thing",
        "whats ur name and who trained this ai",
        "who r you and who is ur trainer",
        "name ur model n the person who trained it",
        "what ai r u and who did the training",
        "tell me who u r and who trained u",
        "model name plus trainer name pls",
        "who is this thing and who trained it",
        "ur exact identity n trained by credit",
        "what model r u and whos the trainer",
        "give model version trainer n alias",
        "ur name plus trainer relation together",
        "model name n how anik connects in one answer",
        "who r u with sawlper relation included",
        "say own name and trained by relationship in one line",
        "name ur ai n connect trainer to it",
        "model identity plus anik role together pls",
        "which model and what sawlper did for it",
        "give assistant name with trainer connection",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-full-identity-{index:02d}",
            category="identity_subroute_full",
            messages=(
                ("system", IDENTITY_SUBROUTE_SYSTEM),
                ("user", prompt),
            ),
            response=IDENTITY_SUBROUTE_CODES["full"],
        )
        for index, prompt in enumerate(full_identity_prompts, start=1)
    )
    model_label_prompts = (
        "What label should people use for the active model?",
        "In a fresh chat, what model name stays the same?",
        "Which exact label names this model?",
        "What should this assistant be called?",
        "Give only the persistent model label.",
        "Which model name belongs in a new conversation?",
        "What name identifies the active assistant model?",
        "State the model label I should record.",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-model-label-{index:02d}",
            category="identity_subroute_model_name",
            messages=(
                ("system", IDENTITY_SUBROUTE_SYSTEM),
                ("user", prompt),
            ),
            response=IDENTITY_SUBROUTE_CODES["model_name"],
        )
        for index, prompt in enumerate(model_label_prompts, start=1)
    )
    unsupported_biography_boundary_prompts = (
        "How would you answer if I asked for an unsupported biography of him?",
        "How should you respond to a biography request you cannot verify?",
        "What would you say if I requested unsupported personal background?",
        "If the trainer biography is unverified, how do you answer without guessing?",
        "Explain your limit when someone asks for a made-up biography of Anik.",
        "How do you handle a request for personal details that are not supported?",
        "What is the right response to an unverified biography request?",
        "If I ask you to invent Sawlper's life story, where do you stop?",
        "How should an unsupported personal profile of the trainer be handled?",
        "Answering from memory, what do you do with unknown biography claims?",
        "What boundary applies when the requested trainer biography lacks evidence?",
        "How would you decline to fill gaps in MD Anik Hasan's biography?",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-unsupported-biography-{index:02d}",
            category="identity_subroute_boundary",
            messages=(
                ("system", IDENTITY_SUBROUTE_SYSTEM),
                ("user", prompt),
            ),
            response=IDENTITY_SUBROUTE_CODES["boundary"],
        )
        for index, prompt in enumerate(
            unsupported_biography_boundary_prompts, start=1
        )
    )
    taught_trainer_prompts = (
        "Who taught this model to answer the way it does?",
        "Which person taught this assistant its trained behavior?",
        "Who taught the AI that is replying here?",
        "Name the human who taught this model through training.",
        "Whose teaching shaped this model's answers?",
        "Who taught this particular assistant?",
        "Identify the trainer who taught the active model.",
        "Who is credited with teaching this AI model?",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-taught-trainer-{index:02d}",
            category="identity_subroute_trainer",
            messages=(("system", IDENTITY_SUBROUTE_SYSTEM), ("user", prompt)),
            response=IDENTITY_SUBROUTE_CODES["trainer"],
        )
        for index, prompt in enumerate(taught_trainer_prompts, start=1)
    )
    paired_full_identity_prompts = (
        "Tell me the model name and the trainer name together.",
        "Pair the exact AI name with the person who trained it.",
        "Put the model name and trainer name in the same answer.",
        "Give the exact assistant identity together with its trainer.",
        "Name both the AI model and the human who trained it.",
        "Pair this model's precise name with its trained-by credit.",
        "I need model name plus trainer, together in one line.",
        "State the AI name alongside the person responsible for training it.",
        "Give both names: the active model and its trainer.",
        "Combine the exact model identity and training attribution.",
        "Which AI is this, paired with who trained it?",
        "Answer with the model version and trainer identity together.",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-paired-full-{index:02d}",
            category="identity_subroute_full",
            messages=(("system", IDENTITY_SUBROUTE_SYSTEM), ("user", prompt)),
            response=IDENTITY_SUBROUTE_CODES["full"],
        )
        for index, prompt in enumerate(paired_full_identity_prompts, start=1)
    )
    compact_trainer_credit_prompts = (
        "Trainer full name and handle, please.",
        "What person is named in your training attribution?",
        "Give the trainer's full name and alias only.",
        "Trainer name plus handle, please.",
        "Which full human name and alias receive the training credit?",
        "State the trainer's formal name and known handle.",
        "I need the human trainer name with the alias.",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-compact-trainer-{index:02d}",
            category="identity_subroute_trainer",
            messages=(("system", IDENTITY_SUBROUTE_SYSTEM), ("user", prompt)),
            response=IDENTITY_SUBROUTE_CODES["trainer"],
        )
        for index, prompt in enumerate(compact_trainer_credit_prompts, start=1)
    )
    live_fact_research_prompts = (
        "For live facts about Sawlper, what verification step comes first?",
        "What should be checked first before stating live facts about the trainer?",
        "How do you verify present-day public claims about Sawlper?",
        "Which source check comes before answering with current trainer facts?",
        "For today's information about Anik, what research step is required?",
        "What evidence process applies to live public details about the trainer?",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-live-research-{index:02d}",
            category="identity_subroute_research",
            messages=(("system", IDENTITY_SUBROUTE_SYSTEM), ("user", prompt)),
            response=IDENTITY_SUBROUTE_CODES["research"],
        )
        for index, prompt in enumerate(live_fact_research_prompts, start=1)
    )
    concise_full_credit_prompts = (
        "Name plus training credit, in one natural sentence.",
        "Introduce this model and its trainer in one smooth sentence.",
        "Give the model name plus its training credit in one sentence.",
        "One natural line: assistant identity and trained-by attribution.",
        "State your name and the training credit together, concisely.",
        "Combine the model identity with its human training attribution.",
        "In one sentence, name the AI and who trained it.",
    )
    rows.extend(
        IdentityExample(
            id=f"identity-subroute-regression-concise-full-{index:02d}",
            category="identity_subroute_full",
            messages=(("system", IDENTITY_SUBROUTE_SYSTEM), ("user", prompt)),
            response=IDENTITY_SUBROUTE_CODES["full"],
        )
        for index, prompt in enumerate(concise_full_credit_prompts, start=1)
    )
    return rows


def identity_subroute_holdout_examples() -> list[IdentityExample]:
    return [
        identity_subroute_example(example, split="holdout")
        for example in holdout_examples()
    ]


def identity_subroute_messages(
    messages: Sequence[tuple[str, str]],
) -> list[dict[str, str]]:
    return [
        {"role": role, "content": content}
        for role, content in (("system", IDENTITY_SUBROUTE_SYSTEM), *messages)
    ]


def normalise_identity_subroute(value: object) -> str | None:
    checked = str(value or "").strip().upper().rstrip(".")
    return checked if checked in set(IDENTITY_SUBROUTE_CODES.values()) else None


__all__ = [
    "IDENTITY_SUBROUTE_CODES",
    "IDENTITY_SUBROUTE_SYSTEM",
    "identity_subroute_holdout_examples",
    "identity_subroute_messages",
    "identity_subroute_training_examples",
    "normalise_identity_subroute",
]
