"""Query intent classification (RAG-002).

A rule-based classifier with a fixed label set — the spec explicitly allows "small/cheap model
with fixed label set + rule-based overrides" (§16.2), and rules are deterministic, offline, and
logged. An LLM classifier can later slot in behind the same `classify_intent` signature as the
cheap-model path (RAG-050); the rules then act as high-precision overrides.

Order matters: injection/out-of-scope and advice are checked first so a prompt-injection attempt
or a "should I buy?" never falls through to a retrieval intent.
"""

from __future__ import annotations

import re

from app.rag.types import Intent, IntentResult

# Investment-advice solicitation (OUT_OF_SCOPE_ADVICE, §16.2): we retrieve evidence, never advise.
_ADVICE = re.compile(
    r"\b(should i (buy|sell|hold)|is it a (buy|sell)|buy or sell|price target|"
    r"will .* (go up|go down|moon|crash)|worth buying|good investment)\b",
    re.IGNORECASE,
)
# Forward-looking prediction (OUT_OF_SCOPE, §16.2): we report what filings say, never forecast the
# future. Narrow by design — "will ... next year/quarter" — so it does not catch a question about
# management's *stated* outlook ("what did management say about the revenue outlook").
_FORECAST = re.compile(
    r"\bwill\b.*\b(next (fiscal )?year|next quarter|coming year)\b", re.IGNORECASE
)
# Obvious prompt-injection / off-domain (OUT_OF_SCOPE). Retrieved-text injection is handled in
# app/rag/injection.py; this catches it in the *question* itself.
_INJECTION = re.compile(
    r"\b(ignore (all |your |the |previous |prior )*(instructions|prompts?)|"
    r"disregard .* (instructions|rules)|system prompt|you are now|reveal your|jailbreak|"
    r"new instructions|respond only with|override your|"
    r"ignore the (citation|source)s?( requirement)?|invent (a |the )?(number|figure)s?)\b",
    re.IGNORECASE,
)
# Requests no filing or metric can answer: creative writing and live conditions (OUT_OF_SCOPE).
# "Weather" alone stays in scope ("did weather hurt sales?"); only asking for it is off-domain.
_OFF_DOMAIN = re.compile(
    r"\b((write|compose|tell)( me)? (a|an) (poem|song|story|joke|haiku|limerick|rap)|"
    r"what('s| is) the (weather|temperature))\b",
    re.IGNORECASE,
)

# Retrieval intents, most specific first. Each entry: (intent, pattern, rule-name).
_RULES: list[tuple[Intent, re.Pattern[str], str]] = [
    (
        Intent.FILING_DIFF,
        re.compile(
            r"\b(what changed|changed .* (10-?k|10-?q|filing)|diff(erence)? "
            r"between .* (filing|10-?k|10-?q))\b",
            re.IGNORECASE,
        ),
        "diff",
    ),
    (
        Intent.COMPARISON,
        re.compile(
            r"\b(compare|versus|vs\.?|compared to|against (its )?(peers|"
            r"competitors))\b",
            re.IGNORECASE,
        ),
        "comparison",
    ),
    (
        Intent.FINANCIAL_EXPLANATION,
        re.compile(
            r"\b(why|how (did|has|have)|what (drove|caused|"
            r"led to)|reason(s)? for|explain)\b",
            re.IGNORECASE,
        ),
        "why-how",
    ),
    (
        Intent.RISK_ANALYSIS,
        re.compile(
            r"\b(risk|headwind|threat|exposure|uncertaint|challeng|"
            r"concern)\w*",
            re.IGNORECASE,
        ),
        "risk",
    ),
    (
        Intent.MANAGEMENT_COMMENTARY,
        re.compile(
            r"\b(management|ceo|cfo|guidance|outlook|"
            r"(are|is) .* saying|commentary|on the (earnings )?"
            r"call)\b",
            re.IGNORECASE,
        ),
        "management",
    ),
    (
        Intent.DOCUMENT_SUMMARY,
        re.compile(
            r"\b(summar(ize|y)|overview of the|tl;?dr|key (points|"
            r"takeaways))\b",
            re.IGNORECASE,
        ),
        "summary",
    ),
]

# Pure metric lookups: a known metric word + a value-seeking verb, and NOT a why/how question.
# No bare "price": stock-price questions are caught before this (ADR-0054), and "average price
# paid per share" (buybacks) belongs to the filings.
_METRIC_TERMS = re.compile(
    r"\b(revenue|sales|net income|earnings|eps|gross margin|operating margin|net margin|"
    r"ebitda|free cash flow|fcf|operating income|debt|cash|current ratio|roe|roa|"
    r"market cap|p/?e|margins?|gross profit|inventory turnover)\b",
    re.IGNORECASE,
)
_METRIC_ASK = re.compile(
    r"\b(what('s| is| was| are| were)?|how much|how many|value of|tell me the|"
    r"give me the|report the|figure|amount)\b",
    re.IGNORECASE,
)
_WHY_HOW = re.compile(r"\b(why|how (did|has|have)|drove|caused|explain)\b", re.IGNORECASE)
# "What did management say about margins?" asks for commentary, not the stored figure.
_SAID = re.compile(r"\b(say|said|says|saying|comment\w*|discuss\w*|management)\b", re.IGNORECASE)


def classify_intent(question: str) -> IntentResult:
    """Classify `question` into one `Intent` with a confidence and the rule that fired."""
    q = question.strip()
    if _INJECTION.search(q):
        return IntentResult(Intent.OUT_OF_SCOPE, 0.99, "injection")
    if _OFF_DOMAIN.search(q):
        return IntentResult(Intent.OUT_OF_SCOPE, 0.9, "off-domain")
    if _ADVICE.search(q):
        return IntentResult(Intent.OUT_OF_SCOPE_ADVICE, 0.95, "advice")
    if _FORECAST.search(q):
        return IntentResult(Intent.OUT_OF_SCOPE, 0.9, "forecast")

    # Pure metric lookup wins over the generic patterns, but never over a why/how explanation.
    metric_ask = _METRIC_TERMS.search(q) and _METRIC_ASK.search(q)
    if metric_ask and not _WHY_HOW.search(q) and not _SAID.search(q):
        return IntentResult(Intent.FINANCIAL_METRIC, 0.9, "metric-lookup")

    for intent, pattern, rule in _RULES:
        if pattern.search(q):
            return IntentResult(intent, 0.8, rule)

    # No rule fired: default to document retrieval rather than refusing (abstain is RAG-018).
    return IntentResult(Intent.DOCUMENT_SUMMARY, 0.3, "default")
