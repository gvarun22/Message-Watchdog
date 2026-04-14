"""
Core data models shared across all modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Message:
    """
    Normalised representation of a message from any source.
    Sources (Telegram, WhatsApp, etc.) convert their native event type
    into this common format before passing it to the engine.
    """
    source_type: str            # "telegram", "whatsapp", …
    chat_id: str                # stable group identifier
    chat_name: str              # human-readable group name (used in alert body)
    sender_id: str
    sender_name: Optional[str]
    text: Optional[str]
    has_media: bool
    media_type: Optional[str]   # "photo", "video", "document", "sticker", …
    timestamp: datetime
    raw: Any = field(repr=False)  # original source event — not compared or hashed

    def format_for_log(self, *, indent: str = "  ") -> str:
        """
        One-line human-readable representation used in alert bodies and LLM prompts.
        Shared by the classifier prompt builder and the engine alert formatter.
        """
        time_str = self.timestamp.strftime("%H:%M:%S")
        sender = self.sender_name or self.sender_id
        if self.text and self.has_media:
            content = f"{self.text}  [sent {self.media_type or 'media'}]"
        elif self.text:
            content = self.text
        elif self.has_media:
            content = f"[sent {self.media_type or 'media'}]"
        else:
            content = "[message]"
        return f"{indent}[{time_str}] {sender}: {content}"


@dataclass
class ActivityContext:
    """
    Message-rate snapshot produced by ActivityTracker and fed to the LLM prompt.
    The spike_factor is the single most useful signal: a 10x rate spike in a
    normally-quiet group is highly informative even if individual messages are ambiguous.
    """
    current_rate: float    # messages per minute over short window (e.g. 60 s)
    baseline_rate: float   # messages per minute over long window (e.g. 1 hour)
    spike_factor: float    # current_rate / max(baseline_rate, 0.01)
    window_seconds: int    # short window size, for display in prompt


@dataclass
class ClassificationResult:
    """
    Structured result returned by LLMClassifier.classify().
    """
    triggered: bool
    confidence: float          # 0.0 – 1.0
    reason: str                # 1-2 sentence LLM explanation
    key_signals: list[str]     # specific message snippets that influenced the decision
    model_used: str
    tokens_used: int
    latency_ms: int


@dataclass
class SurgeGateConfig:
    """
    Configuration for the LLM trip switch.

    The surge gate is a cheap pre-filter that blocks LLM calls during quiet
    periods — the LLM only runs when at least one of three conditions is true:
      1. Activity spike   : message rate >= min_spike_factor × baseline
      2. Absolute minimum : message rate >= always_classify_rate (msg/min)
      3. Keyword signal   : any message in the batch contains a broad H1B pattern

    This reduces LLM token usage by ~95% in a noisy group where most messages
    are unrelated to the watched condition.
    """
    enabled: bool = True
    min_spike_factor: float = 2.5       # activate on N× rate spike above baseline
    always_classify_rate: float = 3.0   # always run LLM if rate >= this (msg/min)
    keyword_patterns: list[str] = field(default_factory=lambda: [
        r"h\s*[-]?\s*1\s*b",           # h1b, h-1b, h 1 b, h1-b, …
    ])


@dataclass
class WatchdogConfig:
    """
    One watchdog rule, as parsed from a single entry under `watchdogs:` in config.yaml.
    """
    name: str
    source_type: str             # "telegram"
    source_group: str            # group URL, @username, or integer ID (as string)
    condition: str               # natural-language condition — the only thing the user needs to change
    confidence_threshold: float  # minimum confidence to fire alert (default 0.75)
    alert_channels: list[str]    # e.g. ["phone_call", "telegram_self", "email"]
    cooldown_seconds: int        # seconds between consecutive alerts (default 300)
    batch_window_seconds: int    # flush buffer after this many seconds of quiet (default 60)
    batch_burst_cap: int         # flush immediately when buffer reaches this size (default 30)
    dry_run: bool                # log alert content but do not actually send
    surge_gate: SurgeGateConfig = field(default_factory=SurgeGateConfig)
