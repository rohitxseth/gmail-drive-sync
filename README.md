# Bank statements: Gmail → Google Drive

A command-line tool that finds bank-statement PDFs in your Gmail and files them in Google Drive:

```
Bank Statements/
├── HDFC Bank/
│   └── 2025/
│       ├── 2025-09 - Acct_Statement_XX4521_30092025.pdf
│       └── 2025-10 - Acct_Statement_XX4521_31102025.pdf
├── ICICI Bank/
│   └── 2025/
│       └── 2025-10 - statement.pdf
└── _Needs review/        statement PDFs from senders it couldn't match to a bank
```

## Quick start (about 10 minutes, once)

1. **Google Cloud project.** In the [Google Cloud console](https://console.cloud.google.com/), create or pick a project. Under *APIs & Services → Library*, enable the **Gmail API** and the **Google Drive API**.
2. **Consent screen.** Go to *Menu → Google Auth platform → Branding*. If it says it isn't configured yet, click *Get started*: give the app a name and your email, and choose **External** as the audience. Then under *Audience → Test users*, add your own Gmail address.
3. **OAuth client.** Go to *Google Auth platform → Clients → Create client*, choose **Desktop app**, and create it. Download the JSON and save it in this folder as `credentials.json`.
4. **Install** (Python 3.10 or newer):
   ```
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
5. **Preview, then run:**
   ```
   python statement_sync.py --dry-run     # the first run opens a browser to sign in
   python statement_sync.py
   ```
   Google shows an "unverified app" warning because the app is in testing mode; it's your own app, so choose *Continue*. **Tick both permission boxes** (Gmail and Drive) on the consent screen. The sign-in is saved to `token.json`, so later runs don't ask again.

Sample output:

```
ADDED     Bank Statements/HDFC Bank/2025/2025-10 - Acct_Statement_XX4521_31102025.pdf
ADDED     Bank Statements/ICICI Bank/2025/2025-09 - statement.pdf
ADDED     Bank Statements/_Needs review/2025/2025-10 - CAS_Oct2025.pdf
DUPLICATE Bank Statements/HDFC Bank/2025/2025-10 - Acct_Statement_XX4521_31102025.pdf (skipped)
SKIP      statement.pdf in 'Axis Bank statement': not a real PDF

5 emails, 5 PDFs. Added 3 (1 in _Needs review). Duplicates skipped: 1. Non-PDFs skipped: 1. Errors: 0.
Drive folder: https://drive.google.com/drive/folders/...
```

## Options

| Flag | What it does |
| --- | --- |
| `--dry-run` | Show what would be added; change nothing |
| `--after 2025-01-01` | Only emails received after this date |
| `--limit 20` | Process at most N emails |
| `--query "..."` | Use your own Gmail search instead of the default |
| `--root-folder "Statements"` | Drive folder name (default `Bank Statements`) |
| `--reauth` | Sign in again |
| `--no-browser` | Print the sign-in link instead of opening a browser |

## How it works

1. **Find.** Gmail search: `has:attachment filename:pdf {subject:statement subject:estatement filename:statement filename:estatement}`.
2. **Extract.** Walks each email's MIME parts (they can nest) and downloads every PDF attachment. Files that aren't real PDFs are skipped.
3. **Classify.** Works out the bank from the sender's domain (`hdfcbank.net`, `alerts.sbi.co.in`, `icici.bank.in`). If that fails, it checks the sender name, subject and file name, which handles forwarded emails. Anything it can't match goes to `_Needs review` instead of being dropped.
4. **Date.** Takes the statement month from the subject, then the file name, then the month the email arrived. Dates after the email arrived, such as a payment due date, are ignored.
5. **File.** Creates `Bank/Year` folders as needed and uploads each file as `YYYY-MM - original name.pdf`. Each file's Drive description links back to its email.
6. **Re-run safely.** Each upload stores the PDF's SHA-256 hash in Drive's private app properties. A PDF that's already in Drive is skipped, so the tool can run on a schedule.

## Design choices

- **Least-privilege access.** `gmail.readonly` can't change or delete mail. `drive.file` can only see the files and folders this tool created, not the rest of your Drive.
- **Never drop silently.** Statement-like PDFs from unknown senders go to `_Needs review`.
- **Idempotent.** De-duplication lives in Drive itself, so it works across machines and survives deleting local files.
- **Resilient.** API calls retry on rate limits and server errors. One bad email is logged and skipped without stopping the run.
- **Banks are data.** To add a bank, add one line to `BANKS` in `statement_sync.py`.

## Limitations

- Password-protected PDFs, which many Indian banks send, are uploaded as they are; the tool doesn't decrypt them.
- Because of `drive.file`, the tool can't see a `Bank Statements` folder you made by hand. It creates and uses its own.
- While the app is in testing mode, Google expires the saved sign-in after 7 days. Run with `--reauth` when that happens.
- Bank detection covers the major Indian banks and a few international ones. Others land in `_Needs review`.

## Taking it to production

- Run it on a schedule (cron or a Cloud Run job), or trigger it on new mail with Gmail push notifications (`users.watch` plus Pub/Sub).
- For a company's Google Workspace, replace the personal sign-in with a service account using domain-wide delegation, and keep secrets in a secret manager.
- Decrypt password-protected statements using per-bank password rules held in a vault, then parse the transactions into a database.
- Add metrics and alerts (emails seen, files added, failures), plus a review queue for `_Needs review`.

## Demo

Screen recording: <add link>

## How this was built

Written with Claude as an AI coding assistant, as the brief suggested.
