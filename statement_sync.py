"""Copy bank-statement PDFs from Gmail into Google Drive, organised by bank and year."""

import argparse
import base64
import hashlib
import io
import re
import sys
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from email.utils import parseaddr
from functools import cache
from http.client import HTTPException
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from httplib2 import HttpLib2Error
from oauthlib.oauth2 import OAuth2Error

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/drive.file"]
DEFAULT_QUERY = ("has:attachment filename:pdf "
                 "{subject:statement subject:estatement filename:statement filename:estatement}")
DEFAULT_ROOT = "Bank Statements"
REVIEW_FOLDER = "_Needs review"
FOLDER_MIME = "application/vnd.google-apps.folder"
RETRIES = 5
SIGN_IN_TIMEOUT = 600  # Seconds. Stops a scheduled run from waiting forever for a browser.
# Raised once retries run out: API refusals and network failures.
API_ERRORS = (HttpError, TransportError, HttpLib2Error, HTTPException, OSError)

# (Drive folder, sender domains, pattern for sender name / subject / file name).
# A plain name matches any part of the domain. Short or common names are full domains instead,
# so axis.com and registrar.sc.edu aren't mistaken for banks.
# The first match wins, so "SBI Card" comes before "SBI".
BANKS = [
    ("SBI Card", {"sbicard"}, r"\bsbi ?card\b"),
    ("HDFC Bank", {"hdfcbank", "hdfc"}, r"\bhdfc ?bank\b"),
    ("ICICI Bank", {"icicibank", "icici"}, r"\bicici ?bank\b"),
    ("SBI", {"sbi", "onlinesbi"}, r"\bsbi\b|\bstate bank of india\b"),
    ("Axis Bank", {"axisbank", "axis.bank.in"}, r"\baxis ?bank\b"),
    ("Kotak Mahindra Bank", {"kotak", "kotakbank"}, r"\bkotak (mahindra )?bank\b"),
    ("IDFC FIRST Bank", {"idfcfirstbank", "idfcbank", "idfcfirst"}, r"\bidfc ?first\b"),
    ("Yes Bank", {"yesbank", "yes.bank.in"}, r"\byes ?bank\b"),
    ("IndusInd Bank", {"indusind", "indusindbank"}, r"\bindusind\b"),
    ("RBL Bank", {"rblbank", "rbl.bank.in"}, r"\brbl ?bank\b"),
    ("AU Small Finance Bank", {"aubank", "au.bank.in"}, r"\bau (small finance )?bank\b"),
    ("Federal Bank", {"federalbank", "federal.bank.in"}, r"\bfederal ?bank\b"),
    ("Bank of Baroda", {"bankofbaroda", "bobcard"}, r"\bbank of baroda\b"),
    ("Punjab National Bank", {"pnbindia", "pnb.co.in", "pnb.bank.in"}, r"\bpunjab national bank\b"),
    ("Canara Bank", {"canarabank", "canara.bank.in"}, r"\bcanara ?bank\b"),
    ("Union Bank of India", {"unionbankofindia", "unionbank"}, r"\bunion bank of india\b"),
    ("Standard Chartered", {"standardchartered", "sc.com", "sc.bank.in"}, r"\bstandard ?chartered\b"),
    ("HSBC", {"hsbc"}, r"\bhsbc\b"),
    ("Citibank", {"citibank", "citi.com", "citi.bank.in"}, r"\bciti ?bank\b"),
    ("DBS Bank", {"dbs.com", "dbs.com.sg", "dbs.bank.in"}, r"\bdbs ?bank\b"),
    ("American Express", {"americanexpress", "aexp"}, r"\bamerican express\b"),
]

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

MONTH_NAME = (r"(?<![a-z])(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
              r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?![a-z])")
DAY = r"(?P<day>0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?"
SEP = r"[\s\-_/,.']"
YEAR = r"(?P<year>20\d{2})(?!\d)"

# In order: 31-10-2025, 2025-10-31, Oct 2025 / 31-Oct-2025, October 31, 2025, 2025-10,
# 31102025 (or <digits>31102025), 20251031
DATE_PATTERNS = [
    r"(?<!\d)(?P<day>\d{1,2})[-/._](?P<month>\d{1,2})[-/._](?P<year>20\d{2})(?!\d)",
    r"(?<!\d)(?P<year>20\d{2})[-/._](?P<month>\d{1,2})[-/._](?P<day>\d{1,2})(?!\d)",
    rf"(?:(?<!\d){DAY}{SEP}*)?{MONTH_NAME}{SEP}*{YEAR}",
    rf"{MONTH_NAME}{SEP}*{DAY}(?!\d){SEP}+{YEAR}",
    r"(?<!\d)(?P<year>20\d{2})[-_](?P<month>0[1-9]|1[0-2])(?![\d\-/]|[._]\d)",
    # ddmmyyyy, also when glued to the end of an account number (SBI names files <account><ddmmyyyy>.pdf)
    r"(?P<day>0[1-9]|[12]\d|3[01])(?P<month>0[1-9]|1[0-2])(?P<year>20\d{2})(?!\d)",
    r"(?<!\d)(?P<year>20\d{2})(?P<month>0[1-9]|1[0-2])(?P<day>0[1-9]|[12]\d|3[01])(?!\d)",
]


def run(request):
    return request.execute(num_retries=RETRIES)


def domain_matches(domain, names):
    labels = domain.split(".")
    return any(f".{domain}".endswith(f".{n}") if "." in n else n in labels for n in names)


def detect_bank(sender, subject, filename):
    name, address = parseaddr(sender)
    if not address:  # Python rejects an unquoted comma, as in: HDFC Bank, Alerts <alerts@hdfcbank.net>
        name, _, address = sender.rpartition("<")
        address = address.rstrip("> ")
    domain = address.rpartition("@")[2].lower()
    for bank, domains, _ in BANKS:
        if domain_matches(domain, domains):
            return bank

    text = re.sub(r"[_\-.]+", " ", f"{name} {subject} {filename}")
    for bank, _, pattern in BANKS:
        if re.search(pattern, text, re.IGNORECASE):
            return bank
    return None


def parse_dates(text):
    for pattern in DATE_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            month = m["month"]
            month = int(month) if month.isdigit() else MONTHS.index(month[:3].lower()) + 1
            # A date with no day counts as the 1st, so "Nov 2025" still fits an email from 3 Nov.
            yield m.end(), int(m["year"]), month, int(m.groupdict().get("day") or 1)


def statement_month(subject, filename, received):
    # The last date mentioned wins. Dates after the email arrived (like a due date) are ignored.
    latest = (received.year, received.month, received.day)
    for text in (subject, filename):
        dates = [(end, y, m) for end, y, m, d in parse_dates(text)
                 if 1 <= m <= 12 and 1 <= d <= 31 and (y, m, d) <= latest]
        if dates:
            return max(dates)[1:]
    return latest[:2]


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .") or "statement.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def search_messages(messages, query, limit):
    count, page_token = 0, None
    while True:
        page = run(messages.list(userId="me", q=query, maxResults=100, pageToken=page_token))
        for item in page.get("messages", []):
            yield item["id"]
            count += 1
            if limit and count >= limit:
                return
        page_token = page.get("nextPageToken")
        if not page_token:
            return


def pdf_parts(part):
    body = part.get("body") or {}
    is_pdf = ((part.get("filename") or "").lower().endswith(".pdf")
              or (part.get("mimeType") or "").lower() == "application/pdf")
    if is_pdf and (body.get("attachmentId") or body.get("data")):
        yield part
    for child in part.get("parts") or []:
        yield from pdf_parts(child)


def attachment_bytes(messages, message_id, body):
    data = body.get("data")
    if not data:
        data = run(messages.attachments().get(userId="me", messageId=message_id, id=body["attachmentId"]))["data"]
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def find_id(files, query):
    page_token = None
    while True:  # Drive can return an empty page before the end of the results.
        page = run(files.list(q=query, spaces="drive", fields="nextPageToken, files(id)",
                              pageSize=1, pageToken=page_token))
        if page.get("files"):
            return page["files"][0]["id"]
        page_token = page.get("nextPageToken")
        if not page_token:
            return None


def find_folder(files, name, parent):
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    return find_id(files, f"name = '{escaped}' and mimeType = '{FOLDER_MIME}' "
                          f"and '{parent}' in parents and trashed = false")


@cache
def get_folder(files, name, parent):
    body = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}
    return find_folder(files, name, parent) or run(files.create(body=body, fields="id"))["id"]


def already_uploaded(files, sha256):
    return find_id(files, f"appProperties has {{ key='sha256' and value='{sha256}' }} and trashed = false")


def upload(files, parent, name, data, properties, description):
    body = {"name": name, "parents": [parent], "appProperties": properties, "description": description}
    # Resumable: a retry continues the same upload, so it can't create a second copy.
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/pdf", resumable=True)
    run(files.create(body=body, media_body=media, fields="id"))


def sync(gmail, drive, query=DEFAULT_QUERY, root_name=DEFAULT_ROOT, dry_run=False, limit=None):
    messages, files = gmail.users().messages(), drive.files()
    stats, seen, root_id = Counter(), set(), None

    for message_id in search_messages(messages, query, limit):
        stats["emails"] += 1
        try:
            msg = run(messages.get(userId="me", id=message_id, format="full"))
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            sender, subject = headers.get("from", ""), headers.get("subject", "")
            received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc).astimezone()

            parts = list(pdf_parts(msg["payload"]))
        except (*API_ERRORS, KeyError, ValueError) as exc:
            stats["errors"] += 1
            print(f"ERROR     message {message_id}: {exc}")
            continue

        for part in parts:  # A bad attachment must not block the others in the same email.
            stats["pdfs"] += 1
            try:
                data = attachment_bytes(messages, message_id, part["body"])
                filename = safe_name(part.get("filename") or f"{message_id}.pdf")
                # PDF readers accept a header anywhere in the first 1024 bytes.
                if b"%PDF" not in data[:1024]:
                    stats["not_pdf"] += 1
                    print(f"SKIP      {filename} in {subject!r}: not a real PDF")
                    continue

                sha256 = hashlib.sha256(data).hexdigest()
                bank = detect_bank(sender, subject, filename)
                folder = bank or REVIEW_FOLDER
                year, month = statement_month(subject, filename, received)
                name = f"{year}-{month:02d} - {filename}"
                path = f"{root_name}/{folder}/{year}/{name}"

                if sha256 in seen or already_uploaded(files, sha256):
                    stats["duplicates"] += 1
                    print(f"DUPLICATE {path} (skipped)")
                    continue

                if dry_run:
                    print(f"WOULD ADD {path}")
                else:
                    root_id = get_folder(files, root_name, "root")
                    parent = get_folder(files, str(year), get_folder(files, folder, root_id))
                    link = f"https://mail.google.com/mail/u/0/#all/{message_id}"
                    upload(files, parent, name, data,
                           properties={"sha256": sha256, "gmailMessageId": message_id},
                           description=f"From Gmail: {subject} | {sender} | {received:%Y-%m-%d} | {link}")
                    print(f"ADDED     {path}")

                # Only count a PDF once it is really uploaded, so a failed upload can be retried.
                seen.add(sha256)
                stats["uploaded"] += 1
                stats["needs_review"] += not bank
            except (*API_ERRORS, KeyError, ValueError) as exc:
                stats["errors"] += 1
                print(f"ERROR     message {message_id}, {part.get('filename') or 'attachment'}: {exc}")

    verb = "Would add" if dry_run else "Added"
    print(f"\n{stats['emails']} emails, {stats['pdfs']} PDFs. {verb} {stats['uploaded']} "
          f"({stats['needs_review']} in {REVIEW_FOLDER}). Duplicates skipped: {stats['duplicates']}. "
          f"Non-PDFs skipped: {stats['not_pdf']}. Errors: {stats['errors']}.")
    if not dry_run:
        root_id = root_id or find_folder(files, root_name, "root")
    if root_id:
        print(f"Drive folder: https://drive.google.com/drive/folders/{root_id}")
    return stats["errors"]


def get_credentials(client_secrets, token_path, reauth, open_browser):
    creds = None
    if token_path.exists() and not reauth:
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
        except RefreshError:
            print("Saved sign-in has expired or was revoked; signing in again.")
            creds = None
        except ValueError:
            print(f"Saved sign-in in {token_path} can't be read; signing in again.")
            creds = None
        except TransportError as exc:
            sys.exit(f"Could not reach Google to renew your sign-in: {exc}")

    if not creds or not creds.valid:
        if not client_secrets.exists():
            sys.exit(f"Missing {client_secrets}. Create a Desktop OAuth client in Google Cloud, "
                     "download its JSON and save it there (see README.md).")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
            # prompt="consent" makes Google return the refresh token that later runs depend on.
            creds = flow.run_local_server(port=0, open_browser=open_browser, prompt="consent",
                                          timeout_seconds=SIGN_IN_TIMEOUT)
        except AttributeError:  # How the Google library reports that nobody finished signing in.
            sys.exit(f"Sign-in timed out after {SIGN_IN_TIMEOUT // 60} minutes. Run this in a terminal to sign in.")
        # oauthlib raises a bare Warning when a permission box was left unticked.
        except (OAuth2Error, webbrowser.Error, Warning, ValueError, OSError) as exc:
            sys.exit(f"Sign-in did not finish: {exc}\nRun again and tick both permission boxes "
                     "(Gmail and Drive). If no browser opens, add --no-browser.")

    # The token grants access to the whole mailbox, so only this user may read it.
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.touch(mode=0o600, exist_ok=True)
    token_path.chmod(0o600)
    token_path.write_text(creds.to_json())
    return creds


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy bank-statement PDFs from Gmail into an organised Google Drive folder.")
    parser.add_argument("--dry-run", action="store_true", help="show what would be added; change nothing")
    parser.add_argument("--after", metavar="YYYY-MM-DD", help="only emails received on or after this date")
    parser.add_argument("--limit", type=positive_int, help="process at most N emails (handy for a first run)")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query (default: %(default)s)")
    parser.add_argument("--root-folder", default=DEFAULT_ROOT, help="Drive folder name (default: %(default)s)")
    parser.add_argument("--credentials", type=Path, default=Path("credentials.json"),
                        help="OAuth client file from Google Cloud (default: %(default)s)")
    parser.add_argument("--token", type=Path, default=Path("token.json"),
                        help="where the sign-in is saved (default: %(default)s)")
    parser.add_argument("--reauth", action="store_true", help="ignore the saved sign-in and sign in again")
    parser.add_argument("--no-browser", action="store_true", help="print the sign-in link instead of opening it")
    args = parser.parse_args()

    if args.after:
        try:
            after = datetime.strptime(args.after, "%Y-%m-%d").astimezone()
            after_seconds = int(after.timestamp())
        except (ValueError, OverflowError, OSError):
            parser.error("--after must look like 2025-01-31")
        # Gmail reads a plain date as midnight Pacific; epoch seconds keep it at local midnight.
        args.query += f" after:{after_seconds}"
    return args


def main():
    args = parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):  # A character the terminal can't show must not crash the run.
        sys.stdout.reconfigure(errors="backslashreplace")
    creds = get_credentials(args.credentials, args.token, args.reauth, not args.no_browser)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    note = "   (dry run: nothing will be changed)" if args.dry_run else ""
    print(f"Gmail search: {args.query}{note}\n")
    try:
        errors = sync(gmail, drive, args.query, args.root_folder, args.dry_run, args.limit)
    except RefreshError as exc:
        sys.exit(f"Your Google sign-in stopped working: {exc}\nRun again with --reauth; "
                 "files already uploaded will be skipped.")
    except HttpError as exc:
        sys.exit(f"Google refused the request: {exc}\nIf you left a permission unticked when signing in, "
                 "run again with --reauth and tick both boxes.")
    except API_ERRORS as exc:
        sys.exit(f"Lost contact with Google: {exc}\nRun again; files already uploaded will be skipped.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
