"""Garmin Connect for Hermes Agent, through Garmin's official Personal API (unofficial plugin)."""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

TOOLSET = "garmin"
SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "garmin"

MORNING_BRIEF_PROMPT = (
    "Write the user's morning readiness brief from their Garmin data, following the garmin skill. "
    "Call garmin_today, then garmin_trends with days=28. In at most 8 short lines: how they slept, "
    "overnight HRV and resting heart rate against their own baseline, Body Battery at wake, any trend "
    "flags, and one practical suggestion for today's training load ({focus}). Use the user's language. "
    "Say it is not medical advice only if you mention a health concern. If the Garmin tools are "
    "unavailable or the key was rejected, reply with one line saying so and how to fix it."
)


def register(ctx) -> None:
    # No network or attribute reads beyond get_config here: register() also runs in validators.
    from .commands import handle
    from .context import DailyContext
    from .runtime import RUNTIME, key_configured
    from .tools import TOOLS

    RUNTIME.bind(ctx.get_config)
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=key_configured, requires_env=["GARMIN_PERSONAL_API_KEY"], emoji=emoji)
    ctx.register_hook("pre_llm_call", DailyContext(RUNTIME))
    ctx.register_command("garmin", handler=handle, args_hint="[status|today|help]",
                         description="Garmin Connect: status, today's overview, help")
    ctx.register_skill("garmin", SKILL_DIR / "SKILL.md",
                       description="Use and interpret Garmin sleep, HRV, Body Battery, stress, training intensity and fitness age data")
    ctx.register_automation_blueprint(
        "morning-readiness",
        title="Morning readiness brief",
        description="Every morning: last night's sleep, HRV and resting heart rate against your own baseline, "
                    "Body Battery and a suggestion for today's training, from your Garmin watch.",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template=MORNING_BRIEF_PROMPT,
        category="daily",
        slots=[
            {"name": "time", "type": "time", "label": "What time?", "default": "07:30",
             "help": "24h local time; leave time for the watch to sync after you wake up"},
            {"name": "recurrence", "type": "weekdays", "label": "Repeat on", "default": "everyday",
             "options": ("everyday", "weekdays", "weekends")},
            {"name": "focus", "type": "text", "label": "Training context",
             "default": "general fitness", "help": "for example: marathon build, recovery week, strength"},
            {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
             "options": ("origin", "local", "telegram", "discord", "email"), "strict": False},
        ],
        skills=("garmin-connect:garmin",),
        tags=("daily", "health", "garmin"),
    )
    ctx.on_unload(RUNTIME.close)
