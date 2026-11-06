# Setting up Share to Notes

About 15 minutes or less. As of November 2026, everything here is free and none of these services need a card
for their free tier. Plans change, so check each provider's pricing page before you sign up.

You'll make:

- **This repo** (fork it, or create your own copy). The workflow runs here.
- **A private notes repo.** Your notes, your fetcher plugin and your categories live here.
- **A Cloudflare Worker** for the inbox.
- Free API keys for **Gemini** and **Groq**, plus a **Cloudflare** API token as backup.
- Optionally, a **Notion** database.

## 1. The private notes repo

1. On GitHub, create a new **private** repo, for example `my-notes`. Add a README so it isn't empty.
2. Optional files in it:
   - `private/fetcher.py`: your fetcher plugin. Start from `examples/fetcher_example.py`.
   - `private/requirements.txt`: extra Python packages your plugin needs.
   - `categories.json`: your own categories, for example
     ```json
     {
       "Money": {"icon": "💰", "hint": "budgeting, saving, investing"},
       "Cooking": {"icon": "🍳", "hint": "recipes and meal prep"},
       "Other": {"icon": "📌", "hint": "anything else"}
     }
     ```
   - `.gitignore` with `__pycache__/` and `.DS_Store`.

## 2. Keys

| What | Where | Free limit (check the provider's page, they change) |
|---|---|---|
| `GEMINI_API_KEY` | aistudio.google.com, Get API key | Flash-Lite: about 500 requests a day, resets midnight US Pacific |
| `GROQ_API_KEY` | console.groq.com, API Keys | Whisper: about 8 hours of audio a day |
| `CF_ACCOUNT_ID`, `CF_API_TOKEN` | Cloudflare dashboard. Token template "Workers AI" | 10,000 neurons a day |
| `NOTION_TOKEN` (optional) | notion.so/profile/integrations, new internal integration | Generous |

## 3. The inbox Worker

```bash
cd worker
cp wrangler.example.toml wrangler.toml
npx wrangler login
npx wrangler d1 create share-to-notes          # copy the database_id into wrangler.toml
npx wrangler d1 execute share-to-notes --remote --file=schema.sql
npx wrangler secret put INBOX_TOKEN            # any long random string
npx wrangler secret put GITHUB_TOKEN           # see below
npx wrangler deploy
```

For `GITHUB_TOKEN`, make a **fine-grained** token at github.com/settings/personal-access-tokens:
access to **this engine repo** and **your notes repo**, permission **Contents: Read and write**.
You'll use the same token as `NOTES_REPO_TOKEN` below.

## 4. GitHub secrets on this repo

Settings, then Secrets and variables, then Actions, then **Secrets**.
Use secrets, not variables, for everything. Secrets show as `***` in the log. Variables would show in full,
and on a public repo anyone can read the log.

| Name | Value |
|---|---|
| `NOTES_REPO` | your notes repo, e.g. `yourname/my-notes` |
| `NOTES_REPO_TOKEN` | the fine-grained token from step 3 |
| `INBOX_TOKEN` | the same string you gave the Worker |
| `GEMINI_API_KEY`, `GROQ_API_KEY` | from step 2 |
| `CF_ACCOUNT_ID`, `CF_API_TOKEN` | from step 2 |
| `NOTION_TOKEN` | optional |
| `FETCHER_ENV` | optional. Settings for your plugin, one `KEY=value` per line |

Then go to the Actions tab and enable workflows if GitHub asks.

## 5. settings.env in your notes repo

Settings that aren't secret go in `settings.env` at the top of your **notes** repo, so they stay private:

```
INBOX_URL=https://share-to-notes-inbox.yourname.workers.dev
NOTION_DATA_SOURCE_ID=
NOTION_HUB_ID=
NOTE_LANGUAGE=British English
LOCAL_TZ=Europe/London
VIDEO_LABEL=video
BASE_TAG=saved
VAULT_NAME=My Notes
STRIP_PARAMS=
```

Only `INBOX_URL` is required. `STRIP_PARAMS` lists extra tracking parameters to remove from links.
Use the same list as `STRIP_PARAMS` in the Worker's wrangler.toml, so both sides spot duplicate links. `NOTION_HUB_ID` is a page with a 📟 callout that each run keeps updated.

## 6. Notion (optional)

1. Make a database with these properties: **Name** (title), **Category** (select), **Summary** (text),
   **Tags** (multi-select), **Creator** (text), **Type** (select), **Source** (URL), **Posted** (date),
   **Saved** (date), **Shortcode** (text), **Status** (select: Ready, Failed), **Read** (checkbox).
2. Share the database with your integration (••• menu, Connections).
3. Copy the data source ID into `NOTION_DATA_SOURCE_ID` in settings.env.
4. To show the Allowance card, add an **Embed** block with
   `https://YOUR_INBOX/widget?k=YOUR_WIDGET_KEY`.

## 7. Your phone's share menu

### iPhone

1. In Shortcuts, make a new shortcut called **Save to Notes**. In its details, turn on
   **Show in Share Sheet** and set it to receive **URLs** and **Text**.
2. Add **Get Contents of URL**:
   - URL: `https://YOUR_INBOX/api/add`
   - Method **POST**
   - Header `Authorization` = `Bearer YOUR_INBOX_TOKEN`
   - Request Body **JSON**, field `url` = **Shortcut Input**
3. Add **Get Dictionary Value** for key `message`, then **Show Notification** with it.

Share any link to **Save to Notes**. You'll see "Saved to your inbox".

### Android

Install **HTTP Shortcuts** from the Play Store. Make a new shortcut:

- Method **POST**, URL `https://YOUR_INBOX/api/add`
- Header `Authorization` = `Bearer YOUR_INBOX_TOKEN`
- Body type **JSON**: `{"url": "{shared text}"}`, using the app's variable for shared text
- Turn on **Show in share menu**

Share any link to it. Other apps that can send a web request from the share menu work too.

### Any device

Open your inbox URL in a browser, enter your inbox token once, and paste links into the box.

Commands in this guide work the same on Mac, Windows (PowerShell) and Linux.

## Handy commands

```bash
gh workflow run process.yml                      # process the inbox now
gh workflow run process.yml -f rewrite=all       # rewrite every note from its saved text
gh workflow run process.yml -f rewrite=notion    # resend every note to Notion
gh workflow run process.yml -f rewrite=SHORTCODE # rewrite one note by its ID
```

Rewriting by note ID is better than by link on a public repo. The value isn't printed in the log either way,
but an ID gives nothing away.

## Test locally

```bash
NOTES_DIR=./test-notes CF_ACCOUNT_ID=... CF_API_TOKEN=... GEMINI_API_KEY=... \
  python pipeline/notes_pipeline.py --url "https://example.com/clip.mp4" --print
```
