# Skills go here

Each skill is a subdirectory implementing the `Skill` protocol in
`src/felt_core/plugin.py` — `name`, `description`, `parameters_schema`,
and an async `handle()`. Dropping a folder in adds a skill; deleting one
removes it. No core code changes needed either way.

Nothing's here yet — this is the scaffold. See the sibling
`clockmost-ai-assistant.md` / `assistant-skills.md` runbooks for the full
list of planned skills and their status.
