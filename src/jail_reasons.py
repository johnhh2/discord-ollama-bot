"""Past-tense reason strings shown back in jail-related embeds.

Captured at jail time (display name baked in) so a later rename doesn't
break the displayed reason. The display layer in economy_cog.py omits
the Reason line entirely when the stored value is None or empty —
legacy jails (set before this column existed) read back as NULL.
"""


def format_steal_reason(victim_name: str) -> str:
    return f"Tried to steal from {victim_name}"


def format_mug_reason(victim_name: str, amount: int) -> str:
    return f"Mugged {victim_name} for {amount:,} coins"


def format_bankheist_reason(target_name: str) -> str:
    """Stub for the upcoming bankheist cog. Not currently called."""
    return f"Participated in a bankheist targeting {target_name}"
