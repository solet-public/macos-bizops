"""Watch phrase detection for agent guardrails.

WatchPhraseChecker provides simple phrase detection for backends without
native hooks. Used by Codex plugin to check JSON events for dangerous
content. Claude Code plugin should use SDK's native PreToolUse hooks instead.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WatchPhraseAlert:
    """Alert generated when a watch phrase is detected.

    Attributes:
        phrase: The phrase that triggered the alert
        context: Surrounding text where phrase was found (truncated)
        severity: Alert severity - "warn" or "terminate"
    """

    phrase: str
    context: str
    severity: str  # "warn" | "terminate"


class WatchPhraseChecker:
    """Simple phrase detection for backends without native hooks.

    Used by Codex plugin. Claude Code plugin uses SDK hooks instead.

    Example:
        checker = WatchPhraseChecker(
            phrases=["rm -rf", "DROP TABLE"],
            dangerous_phrases=["rm -rf /"],
        )
        alert = checker.check("Running: rm -rf /tmp/build")
        if alert and alert.severity == "terminate":
            process.terminate()
    """

    def __init__(
        self,
        phrases: list[str],
        dangerous_phrases: list[str] | None = None,
    ) -> None:
        """Initialize checker with watch phrases.

        Args:
            phrases: List of phrases to detect
            dangerous_phrases: Subset of phrases that trigger termination
        """
        self._phrases = [p.lower() for p in phrases]
        self._dangerous = frozenset(p.lower() for p in (dangerous_phrases or []))

    def check(self, content: str) -> WatchPhraseAlert | None:
        """Check content for watch phrases.

        Args:
            content: Text content to check

        Returns:
            WatchPhraseAlert if phrase found, None otherwise
        """
        content_lower = content.lower()
        for phrase in self._phrases:
            if phrase in content_lower:
                severity = "terminate" if phrase in self._dangerous else "warn"
                return WatchPhraseAlert(
                    phrase=phrase,
                    context=content[:200],
                    severity=severity,
                )
        return None
