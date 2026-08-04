"""Curated post-training data for Base Steak's routing-only adapter."""

from __future__ import annotations

from .base_steak_identity_dataset import IdentityExample


ROUTE_CODES = {
    "RESPOND": "A",
    "RESEARCH": "B",
    "IMAGE": "C",
    "AGENT": "D",
}

ROUTE_SYSTEM = """Choose what kind of execution the latest user request needs.
Reply with exactly one code and nothing else:
A - answer from stable knowledge or supplied conversation/data
B - read and validate current public web information
C - create or revise a generated image
D - operate the local computer, terminal, files, browser, Discord, Gmail, or another connected service
Classify the requested outcome, not isolated keywords. Never perform the request here."""


_SEEDS = {
    "RESPOND": (
        "Hello, how are you?",
        "Explain LoRA in simple terms.",
        "What is 17 multiplied by 23?",
        "Write a Python function that reverses a list.",
        "Translate this sentence into Bengali.",
        "Summarize the text I pasted above.",
        "Compare a process and a thread.",
        "Explain how web research works without searching.",
        "Write code that opens a file, but do not run it.",
        "Give me three names for a fictional cafe.",
        "Improve the grammar of this paragraph.",
        "Design a normalized database schema for a bookstore.",
        "Why is floating-point addition not associative?",
        "Draft the words of a polite email for me to copy.",
        "What did the document I attached say?",
        "Continue the calculation from our conversation.",
        "What is your model name and who trained you?",
        "Explain why source validation matters.",
        "Describe a good folder structure without creating it.",
        "Give pseudocode for a browser automation loop.",
        "Rewrite this title to sound more professional.",
        "Teach me the ACID properties of a transaction.",
        "Brainstorm a cinematic street scene in words only.",
        "Explain the term agent mode.",
        "Reply only with valid JSON whose ok field is true.",
        "Count the characters in the phrase Base Steak 2.0.",
        "What are the tradeoffs of a 262K context window?",
        "Review this code snippet for bugs.",
        "Make a checklist I can follow manually.",
        "Thank you for the help.",
    ),
    "RESEARCH": (
        "Research current SSD prices from several stores.",
        "Find ten valid Discord invite links and verify each one.",
        "What is the latest stable CUDA release today?",
        "Cross-check this public claim with independent sources.",
        "Find the current CEO and cite the official announcement.",
        "Search YouTube and public forums for recent owner reports.",
        "Compare today's prices for three 2 TB NVMe drives.",
        "Which of these public links still works right now?",
        "Look up the newest Windows release notes.",
        "Find an open restaurant near Dhanmondi this evening.",
        "Research recent changes to Python 3.14.",
        "Verify this Discord invitation is not expired.",
        "Find current laptop prices in Bangladesh and cite the shops.",
        "Summarize breaking AI news from primary sources.",
        "Retrieve the current documentation for this API endpoint.",
        "Compare the official report with independent technical coverage.",
        "Find public Instagram posts about this week's product launch.",
        "Check the current weather forecast from reliable sources.",
        "What changed in the library's most recent version?",
        "Find the official page and two independent confirmations.",
        "Research whether this download is currently available.",
        "Give me direct citations for the latest security advisory.",
        "Validate every source before reporting the current stock price.",
        "Search public social media for recent event reactions.",
        "Compare the current schedules published by two sites.",
        "Find today's exchange rate and cite the data provider.",
        "Research the present legal requirement in my jurisdiction.",
        "Check whether the announced release date has changed.",
        "Find currently active small Discord communities for this topic.",
        "Investigate a disputed benchmark and preserve disagreements.",
    ),
    "IMAGE": (
        "Generate an image of a rain-soaked neon street.",
        "Create a premium logo and icon for Boilabin.",
        "Draw the environment you just described.",
        "Make a poster for the fictional cafe we discussed.",
        "Turn this conversation's road scene into concept art.",
        "Create a transparent mascot illustration.",
        "Generate a photorealistic mountain sunrise.",
        "Design a mobile-app splash image.",
        "Revise the previous image with warmer lighting.",
        "Remove the background from the generated character.",
        "Make three visual variants of that logo.",
        "Render this room as an isometric illustration.",
        "Create a cinematic thumbnail for my video.",
        "Generate a clean product mockup from this brief.",
        "Draw a map-style illustration of the imagined town.",
        "Create an avatar based on the character in our conversation.",
        "Make the last image more minimal and less saturated.",
        "Generate a widescreen wallpaper of the scene.",
        "Create a UI concept image, not implementation code.",
        "Illustrate the database architecture as a polished diagram.",
        "Make an album-cover image with no text.",
        "Generate a realistic red sports car at night.",
        "Create an infographic from the facts we discussed.",
        "Draw a friendly robot holding a steak-shaped badge.",
        "Produce a square social-media graphic for the launch.",
        "Edit the attached image to remove the person.",
        "Use the previous answer as the prompt for an image.",
        "Create a line-art icon set for the app.",
        "Generate a storyboard frame of the opening scene.",
        "Visualize this fictional landscape as matte painting.",
    ),
    "AGENT": (
        "Open Notepad on my computer.",
        "Launch Discord.",
        "Open this URL in my browser.",
        "Run git status in the terminal.",
        "Create a folder named Project Atlas on my desktop.",
        "Move these files into the archive folder.",
        "Put the selected file in the Recycle Bin.",
        "Optimize my Windows PC for performance.",
        "Disable Windows animations using the correct system settings.",
        "Label my Gmail messages about Project Atlas.",
        "Draft this message inside Gmail but do not send it.",
        "Send the approved email to the address I named.",
        "Open Discord and navigate to the server invite.",
        "Type this approved message in Discord without posting it.",
        "Post the message I explicitly approved to the current Discord channel.",
        "Take a screenshot of the primary display.",
        "Focus the Calculator window.",
        "Use PowerShell to print the current working directory.",
        "Find all log files in this folder and show me before deleting anything.",
        "Rename the selected local file to final-report.pdf.",
        "Open YouTube in the browser and play the named video.",
        "Fill this web form with the details I provided.",
        "Download the file to my Downloads folder.",
        "Organize 120 matching mailbox messages into their own label.",
        "Open Settings and switch to the performance page.",
        "Copy this value to the clipboard.",
        "Read the visible browser page and save its title locally.",
        "Create a calendar event in my connected account.",
        "Run the test suite in this project and report failures.",
        "Install the prepared local package after the validation gates pass.",
    ),
}

_WRAPPERS = (
    "{prompt}",
    "Please do this: {prompt}",
    "For this turn, {prompt}",
    "I need you to handle this carefully: {prompt}",
    "Continue from our conversation and {lower}",
)


def training_examples() -> list[IdentityExample]:
    examples: list[IdentityExample] = []
    for label, prompts in _SEEDS.items():
        for prompt_index, prompt in enumerate(prompts, start=1):
            lower = prompt[0].lower() + prompt[1:]
            for wrapper_index, wrapper in enumerate(_WRAPPERS, start=1):
                value = wrapper.format(prompt=prompt, lower=lower)
                examples.append(
                    IdentityExample(
                        id=f"route-{label.casefold()}-{prompt_index:03d}-{wrapper_index}",
                        category=f"route_{label.casefold()}",
                        messages=(("system", ROUTE_SYSTEM), ("user", value)),
                        response=ROUTE_CODES[label],
                    )
                )
    return examples


_HOLDOUT = (
    ("RESPOND", "Without browsing, explain what a hash table does."),
    ("RESPOND", "Do not open anything; tell me how Windows startup apps work."),
    ("RESPOND", "Write the content of an email, but leave my Gmail account untouched."),
    ("RESPOND", "Why might a global LoRA damage unrelated answers?"),
    ("RESPOND", "Correct the grammar in the sentence I supplied."),
    ("RESPOND", "What is your canonical identity?"),
    ("RESPOND", "Give me code for image generation, not an actual picture."),
    ("RESPOND", "Explain current as a programming concept, not today's news."),
    ("RESPOND", "Summarize our plan in five bullets."),
    ("RESPOND", "Calculate 31 times 19 after the identity discussion."),
    ("RESPOND", "Describe what a terminal command would do without running it."),
    ("RESPOND", "Brainstorm a logo concept using words only."),
    ("RESPOND", "Explain Discord server roles from stable knowledge."),
    ("RESPOND", "Return a compact JSON example with an ok field."),
    ("RESPOND", "Thank the new teammate in two friendly sentences."),
    ("RESEARCH", "Determine which public invite URLs are active and reject expired ones."),
    ("RESEARCH", "Find the present release and validate it against two publishers."),
    ("RESEARCH", "Browse recent videos and forum posts, then compare their claims."),
    ("RESEARCH", "What does the current official advisory say as of today?"),
    ("RESEARCH", "Look up live product availability before recommending a seller."),
    ("RESEARCH", "Research the latest model announcement and cite exact pages."),
    ("RESEARCH", "Check whether this public website now redirects somewhere valid."),
    ("RESEARCH", "Find today's match result from authoritative sports sources."),
    ("RESEARCH", "Verify a disputed public statistic across independent organizations."),
    ("RESEARCH", "Search current restaurant hours before suggesting where to go."),
    ("RESEARCH", "Find recent public posts from YouTube, Instagram, and forums."),
    ("RESEARCH", "Compare this month's prices and keep unsupported claims out."),
    ("RESEARCH", "Retrieve the newest API documentation and release notes."),
    ("RESEARCH", "Who currently holds this public office? Include citations."),
    ("RESEARCH", "Investigate whether the announced event was postponed."),
    ("IMAGE", "Show me our imagined Dhaka street as a cinematic picture."),
    ("IMAGE", "Give the previous scene a visual form."),
    ("IMAGE", "Produce a transparent app icon from the brand brief."),
    ("IMAGE", "Redraw the existing result with a blue-hour palette."),
    ("IMAGE", "Turn the attached sketch into a polished illustration."),
    ("IMAGE", "Create a 16:9 thumbnail without adding words."),
    ("IMAGE", "Make a visual comparison chart as an image."),
    ("IMAGE", "Generate the character we described earlier."),
    ("IMAGE", "Remove the car from the last generated picture."),
    ("IMAGE", "Create three image variations of this environment."),
    ("IMAGE", "Render a photoreal product shot from the specification."),
    ("IMAGE", "Design a poster image for the fictional event."),
    ("IMAGE", "Visualize the system architecture in a clean diagram."),
    ("IMAGE", "Make the current image brighter and less cluttered."),
    ("IMAGE", "Create a square avatar based on our conversation."),
    ("AGENT", "Navigate to Discord and stop before posting the prepared text."),
    ("AGENT", "Use the terminal to inspect disk space on this PC."),
    ("AGENT", "Open Gmail and prepare the approved draft in my account."),
    ("AGENT", "Organize the matching messages, then verify the label count."),
    ("AGENT", "Change the local Windows performance setting I requested."),
    ("AGENT", "Launch the installed application named Spotify."),
    ("AGENT", "Create the directory and move only the files I listed."),
    ("AGENT", "Run this repository's tests in PowerShell."),
    ("AGENT", "Open the current page in the browser and click the Downloads link."),
    ("AGENT", "Capture the primary screen and inspect the visible window."),
    ("AGENT", "Add the event to my connected calendar."),
    ("AGENT", "Copy the generated path to my clipboard."),
    ("AGENT", "Recycle the exact file after showing me its resolved path."),
    ("AGENT", "Type the message into the active chat box but do not press Send."),
    ("AGENT", "Install the sealed candidate and update the desktop shortcut."),
)


def holdout_examples() -> list[IdentityExample]:
    return [
        IdentityExample(
            id=f"route-holdout-{index:03d}",
            category=f"route_{label.casefold()}_holdout",
            messages=(("system", ROUTE_SYSTEM), ("user", prompt)),
            response=ROUTE_CODES[label],
        )
        for index, (label, prompt) in enumerate(_HOLDOUT, start=1)
    ]


__all__ = ["ROUTE_CODES", "ROUTE_SYSTEM", "holdout_examples", "training_examples"]
