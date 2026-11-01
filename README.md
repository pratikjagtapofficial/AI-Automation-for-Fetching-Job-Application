# AI-Automation-for-Fetching-Job-Application

## LinkedIn draft bot

A separate Telegram bot, independent of the job pipeline — its own token,
offset file, and database (`data/social.db`). Send it any text and it
drafts a LinkedIn post from it; nothing posts until you tap **Post** on
the card that comes back. See [SPEC.md](SPEC.md) "Phase 6" for setup.

```
python -m src.social_bot            # run until interrupted
python -m src.social_bot --once     # drain pending updates and exit
```