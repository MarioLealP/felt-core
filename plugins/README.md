# Skills go here

Each skill is a subdirectory implementing the `Skill` protocol in
`src/felt_core/plugin.py` — `name`, `description`, `parameters_schema`,
and an async `handle()`. Dropping a folder in adds a skill; deleting one
removes it. No core code changes needed either way.

See the sibling `clockmost-ai-assistant.md` / `assistant-skills.md`
runbooks for the full list of planned skills and their status.

`tags.json` maps a skill name to its default animation reaction tag.
