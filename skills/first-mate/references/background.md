# First mate — background

- **Dialogs stay with the first mate** on the user's word in any tab: they are the user's choice, and a
  hop through the epic lead adds nothing.
- **Mark complete** writes `COMPLETE.json`, which hides the epic on the board; the board's Reopen
  undoes it.
- **Lead refresh**: re-registering (`adw-status lead`) re-routes the lead's open and unlogged
  decisions to the new session. A lead with segment agents running in-tab would lose them on
  `/clear` — hence the `[refreshable]` requirement. The board emits the `refresh` line only when the
  lead is idle, declared `waiting … [refreshable]`, has had no activity since, has nothing running
  in-tab, and its context is past `ADW_LEAD_REFRESH_AT` (default 150k tokens).
- **`/continue`**: the decision card explains the decision, so the needs-you chat line doesn't.
- **Context hygiene**: you run for days. Files are your memory; the conversation is disposable.
