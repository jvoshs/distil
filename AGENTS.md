# Instructions for AI coding agents

You're helping someone set up or change Distil. Read README.md and docs/SETUP.md first.

## Rules

- **Never ask the user to paste a key, token or password into the chat.** Use commands that prompt for it:
  `gh secret set NAME -R OWNER/REPO` and `npx wrangler secret put NAME`. The user types the value there.
- **Never write keys into files**, and never commit `worker/wrangler.toml`, `settings.env` or anything from
  the user's notes repo to this repo.
- **Ask before each step that creates something** (a repo, a database, a Worker, a token) or costs money.
  Everything here should stay on free tiers. Never turn on billing.
- **The notes repo must be private.** Check with `gh repo view OWNER/NOTES --json visibility`.
- **Don't add fetchers for specific websites to this repo.** If the user wants one, it goes in their private
  notes repo at `private/fetcher.py`, and they're responsible for it.
- Use secrets, not repository variables, for settings in the workflow. Variables show in public logs.

## Setup steps

1. **Check tools:** `gh --version`, `gh auth status`, `node --version`, `python3 --version`.
2. **Repos:** confirm the user has forked this repo. Create the notes repo:
   `gh repo create NOTES_NAME --private --add-readme`.
3. **Keys the user gets in a browser.** Give them the links and wait:
   - Gemini: https://aistudio.google.com (Get API key)
   - Groq: https://console.groq.com (API Keys)
   - Cloudflare account ID and a Workers AI API token: https://dash.cloudflare.com
   - Optional Notion connection: https://app.notion.com/developers/connections (make a new connection and
     copy its API token)
   - A fine-grained GitHub token at https://github.com/settings/personal-access-tokens/new with access to
     the fork and the notes repo, and **Contents: Read and write** only.
4. **Inbox Worker**, from `worker/`:
   - `cp wrangler.example.toml wrangler.toml`, then fill in `GITHUB_REPO`, `WIDGET_KEY` (generate a random
     string with `openssl rand -hex 12`), `APP_NAME`, `ITEM_NOUN` and `WIDGET_TZ`.
   - `npx wrangler login`
   - `npx wrangler d1 create distil`, then put the `database_id` in wrangler.toml.
   - `npx wrangler d1 execute distil --remote --file=schema.sql`
   - Generate an inbox token with `openssl rand -hex 24`. Show it to the user once, so they can use it in their
     Shortcut. Then have them run `npx wrangler secret put INBOX_TOKEN` and paste it there.
   - `npx wrangler secret put GITHUB_TOKEN` (the fine-grained token)
   - `npx wrangler deploy`. Note the URL it prints.
5. **GitHub secrets on the fork.** The user runs each of these and types the value when asked:
   `NOTES_REPO` (`owner/notes-repo`), `NOTES_REPO_TOKEN`, `INBOX_TOKEN`, `GEMINI_API_KEY`, `GROQ_API_KEY`,
   `CF_ACCOUNT_ID`, `CF_API_TOKEN`, and optionally `NOTION_TOKEN` and `FETCHER_ENV`.
   Command: `gh secret set NAME -R OWNER/FORK`.
6. **settings.env** in the notes repo with `INBOX_URL=` set to the Worker URL. Add other settings from
   docs/SETUP.md if the user wants them. Commit and push it to the **notes** repo.
7. **Notion (optional):** help the user make the database with the properties in docs/SETUP.md, share it with
   the connection, and put `NOTION_DATA_SOURCE_ID` in settings.env.
8. **Phone share menu:** ask whether they use iPhone or Android, then walk them through section 7 of
   docs/SETUP.md. You can't do this part for them.
9. **Enable Actions** on the fork if needed: `gh workflow enable process.yml -R OWNER/FORK`.
10. **Test:** add a direct link to a short public video file through the inbox page, or run
    `gh workflow run process.yml -R OWNER/FORK` and `gh run watch -R OWNER/FORK`. Check the log shows
    `Saved <hidden>.md` and no links or titles.

## Writing a fetcher plugin

If the user asks for help with a fetcher:

1. Ask what content they save and where it comes from.
2. Suggest options, best first: an official API, an official data export, an oEmbed or public embed link,
   a direct media link, then other tools. Explain the trade-offs: reliability, cost, rate limits, and
   whether it fits the site's terms.
3. Never use the user's own logins, cookies or session tokens for a site. That can get their account banned.
4. Write the plugin to the **notes repo** at `private/fetcher.py`, following examples/fetcher_example.py.
   Put any packages it needs in `private/requirements.txt`, and any keys in the `FETCHER_ENV` secret.
5. Never add it to this repo.

## Changing the code

- The engine is `pipeline/notes_pipeline.py`. It only uses the Python standard library, plus ffmpeg.
- Keep `normalise_url()` in the engine and `normaliseLink()` in `worker/src/index.js` in step. They must make
  the same link from the same input.
- Anything that logs user content must go through quiet mode. Call `hide()` on titles, names and IDs, and
  don't log links unless they pass through `redact()`.
- Test locally with `NOTES_DIR=./test-notes python pipeline/notes_pipeline.py --url "https://..." --print`.
