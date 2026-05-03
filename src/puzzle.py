"""Puzzle-generation prompts and response parsing.

The actual `!puzzle` command is in src/cogs/utility_cog.py — this module
holds the LLM-prompt templates and the JSON-with-regex-fallback extractor
so the cog body stays focused on Discord orchestration.
"""
import json
import re


PUZZLE_RIDDLE_PROMPT = (
    "You are a riddle generator. Your ONLY output must be a single raw JSON object — no markdown, no code fences, no prose before or after.\n"
    "\n"
    "Generate a creative riddle that satisfies ALL of these rules:\n"
    "\n"
    "WHAT MAKES A GOOD RIDDLE:\n"
    "  • The riddle should describe the answer indirectly — through what it does, how it behaves, or how it feels — NOT by literally listing its physical properties\n"
    "  • The answer should feel surprising and satisfying in hindsight: 'oh, of course!' — not 'well obviously, it said exactly what it is'\n"
    "  • Use unexpected angles, personification, or contradiction to obscure the answer\n"
    "\n"
    "HARD RULES:\n"
    "  • Do NOT generate math problems, arithmetic, number puzzles, trivia questions, or factual quiz questions\n"
    "  • Do NOT use these banned overused answers: echo, mirror, shadow, silence, time, fire, wind, darkness, light, water, breath, death, balloon\n"
    "  • Do NOT write a riddle that reads like a checklist of the answer's traits (shape + property + action = answer). That is not a riddle, it is a description.\n"
    "  • Every statement in the riddle must be UNIVERSALLY TRUE of the answer — no exceptions. Do not invent false constraints (e.g. 'I have no lock' for a door) to make the answer harder to guess. If a clue is only sometimes true, or sometimes false, remove it.\n"
    "  • A good riddle makes the solver think of something UNRELATED before the answer clicks. If someone could guess the answer from the second sentence alone, rewrite it.\n"
    "  • The answer must be a single common English word (no phrases, no numbers, no abbreviations)\n"
    "  • The answer must be unambiguous — there should be only one reasonable word that fits\n"
    "\n"
    "Output EXACTLY this JSON and nothing else:\n"
    "  {\"riddle\": \"<the riddle text>\", \"answer\": \"<single lowercase word>\"}\n"
    "\n"
    "Output ONLY the JSON object. Any text outside the JSON will break the parser."
)


PUZZLE_CODING_PROMPT = (
    "You are a coding puzzle generator. Your ONLY output must be a single raw JSON object — no markdown, no code fences, no prose before or after.\n"
    "\n"
    "STEP 1 — Write a self-contained code snippet that satisfies ALL of these:\n"
    "  • Uses only the standard library (no third-party imports)\n"
    "  • No input(), no random, no time-dependent values — output must be fully deterministic\n"
    "  • No unhandled exceptions of ANY kind — no AttributeError, TypeError, ZeroDivisionError, NameError, IndexError, KeyError, RecursionError, or any other exception that would terminate the program without being caught\n"
    "  • No infinite loops or unbounded recursion\n"
    "  • Must produce exactly ONE line of stdout output — the entire output must fit on a single line with no newline characters\n"
    "  • The puzzle's difficulty should come from surprising but VALID behavior — not from errors\n"
    "\n"
    "STEP 2 — Simulate a Python/JS/C interpreter in your head. Execute every line in order:\n"
    "  a) Track the value of every variable after each assignment\n"
    "  b) For every function call, trace what it returns\n"
    "  c) For every exception that could be raised — even inside try/except blocks — verify it is caught and handled\n"
    "  d) List only the lines that call print() (or printf/console.log). Write down exactly what each prints.\n"
    "  e) Ask: 'Is there any line that could raise an UNCAUGHT exception?' If yes → go back to STEP 1 and rewrite.\n"
    "  f) Ask: 'Is the stdout list from step (d) non-empty?' If empty → go back to STEP 1 and rewrite.\n"
    "  g) Ask: 'Does the stdout list from step (d) contain more than one line of output?' If yes → go back to STEP 1 and rewrite the snippet so it only prints once.\n"
    "\n"
    "STEP 3 — Output EXACTLY this JSON and nothing else:\n"
    "  {\"language\": \"<Python|JavaScript|C>\", "
    "\"code\": \"<snippet as a plain string — no backticks, no markdown, no code fences>\", "
    "\"answer\": \"<exact stdout>\"}\n"
    "\n"
    "CRITICAL: The 'code' field must be a valid JSON string value.\n"
    "  • Do NOT wrap it in backticks or markdown fences (no ```python, no ``` at all)\n"
    "  • Embed newlines as \\n and tabs as \\t\n"
    "  • Any double-quote character inside the code MUST be escaped as \\\". Example: s = \\\"hello\\\" not s = \"hello\"\n"
    "  • Prefer single quotes for string literals in the code where the language allows it (Python, JS) to avoid escaping\n"
    "\n"
    "Rules for the answer field:\n"
    "  • Copy character-for-character from your stdout list in STEP 2d\n"
    "  • Multiple printed lines are joined with a literal \\n in the JSON string\n"
    "  • No trailing newline (Python's print() newline is not part of the output string)\n"
    "  • For C: use standard Linux printf/puts behavior\n"
    "\n"
    "Output ONLY the JSON object. Any text outside the JSON will break the parser."
)


PUZZLE_DIFFICULTY_GUIDANCE = {
    "easy":   "Use Python or JavaScript only. Use a trivial snippet (e.g. basic arithmetic, string concat, simple loop). The output should be obvious to a beginner.",
    "medium": "Use Python or JavaScript only. Use a moderately tricky snippet involving type coercion, simple recursion, or list operations.",
    "hard":   "Use Python, JavaScript, or C. Use a tricky snippet involving closures, scoping, reference semantics, or unexpected operator behavior.",
    "extreme": "Use Python, JavaScript, or C. This must be brutally hard. The difficulty MUST come from actual algorithmic complexity or non-trivial computation — NOT from simple floating point quirks, basic type theory, or single-line edge cases. Required: the snippet must involve at least one of (a) a non-trivial algorithm (e.g. recursive descent, dynamic programming, bitwise computation, manual numeric base conversion, custom sort/reduce), (b) complex multi-step string manipulation or construction (e.g. encoding, interleaving, repeated transformations), or (c) a computation that requires tracing several steps of state mutation through data structures. The solver must actually work through the logic — not just recall a language quirk. The code must run to completion and print exactly one line.",
}


def build_coding_prompt(difficulty: str) -> str:
    """Coding-puzzle system prompt with the difficulty rider appended."""
    guidance = PUZZLE_DIFFICULTY_GUIDANCE[difficulty]
    return PUZZLE_CODING_PROMPT + f"\n\nDIFFICULTY: {difficulty}. {guidance}"


def extract_puzzle_fields(text: str) -> dict | None:
    """Parse the JSON object from a coding-puzzle LLM response.

    Tries `json.loads` first. If the model emitted invalid JSON (e.g.
    unescaped backslashes inside the `code` field), falls back to a
    per-field regex extractor that pulls out language/code/answer
    individually. Returns None if both attempts fail.
    """
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        return None
    blob = json_match.group()
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        pass
    lang_m = re.search(r'"language"\s*:\s*"([^"]+)"', blob)
    ans_m  = re.search(r'"answer"\s*:\s*"([^"]*)"', blob)
    code_m = re.search(r'"code"\s*:\s*"(.*?)"\s*(?:,\s*"answer"|,\s*"language"|\})', blob, re.DOTALL)
    if lang_m and ans_m and code_m:
        return {
            "language": lang_m.group(1),
            "code":     code_m.group(1),
            "answer":   ans_m.group(1),
        }
    return None


def normalize_code(code_raw: str) -> str:
    """Strip markdown code fences and unescape \\n/\\t from a JSON code string."""
    code = re.sub(r'^```[a-zA-Z]*\n?', '', code_raw.strip())
    code = re.sub(r'\n?```$', '', code)
    return code.replace("\\n", "\n").replace("\\t", "\t")
