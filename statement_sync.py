"""Copy bank-statement PDFs from Gmail into Google Drive, organised by bank and year."""

import argparse
import base64
import hashlib
import io
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from email.utils import parseaddr
from functools import cache
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/drive.file"]
DEFAULT_QUERY = ("has:attachment filename:pdf "
                 "{subject:statement subject:estatement filename:statement filename:estatement}")
DEFAULT_ROOT = "Bank Statements"
REVIEW_FOLDER = "_Needs review"
FOLDER_MIME = "application/vnd.google-apps.folder"
RETRIES = 5

# (Drive folder, sender domain labels, pattern for sender name / subject / file name).
# The first match wins, so "SBI Card" comes before "SBI".
BANKS = [
    ("SBI Card", {"sbicard"}, r"\bsbi ?card\b"),
    ("HDFC Bank", {"hdfcbank", "hdfc"}, r"\bhdfc ?bank\b"),
    ("ICICI Bank", {"icicibank", "icici"}, r"\bicici ?bank\b"),
    ("SBI", {"sbi", "onlinesbi"}, r"\bsbi\b|\bstate bank of india\b"),
    ("Axis Bank", {"axisbank", "axis"}, r"\baxis ?bank\b"),
    ("Kotak Mahindra Bank", {"kotak", "kotakbank"}, r"\bkotak (mahindra )?bank\b"),
    ("IDFC FIRST Bank", {"idfcfirstbank", "idfcbank", "idfcfirst"}, r"\bidfc ?first\b"),
    ("Yes Bank", {"yesbank"}, r"\byes ?bank\b"),
    ("IndusInd Bank", {"indusind", "indusindbank"}, r"\bindusind\b"),
    ("RBL Bank", {"rblbank"}, r"\brbl ?bank\b"),
    ("AU Small Finance Bank", {"aubank"}, r"\bau (small finance )?bank\b"),
    ("Federal Bank", {"federalbank"}, r"\bfederal ?bank\b"),
    ("Bank of Baroda", {"bankofbaroda", "bobcard"}, r"\bbank of baroda\b"),
    ("Punjab National Bank", {"pnb", "pnbindia"}, r"\bpunjab national bank\b"),
    ("Canara Bank", {"canarabank"}, r"\bcanara ?bank\b"),
    ("Union Bank of India", {"unionbankofindia", "unionbank"}, r"\bunion bank of india\b"),
    ("Standard Chartered", {"sc", "standardchartered"}, r"\bstandard ?chartered\b"),
    ("HSBC", {"hsbc"}, r"\bhsbc\b"),
    ("Citibank", {"citi", "citibank"}, r"\bciti ?bank\b"),
    ("DBS Bank", {"dbs"}, r"\bdbs ?bank\b"),
    ("American Express", {"americanexpress", "aexp"}, r"\bamerican express\b"),
]

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

# In order: 31-10-2025, 2025-10-31, Oct 2025 / 31-Oct-2025, 2025-10, 31102025 (or <digits>31102025)
DATE_PATTERNS = [
    r"(?<!\d)\d{1,2}[-/.](?P<month>\d{1,2})[-/.](?P<year>20\d{2})(?!\d)",
    r"(?<!\d)(?P<year>20\d{2})[-/.](?P<month>\d{1,2})[-/.]\d{1,2}(?!\d)",
    r"\b(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-/,.']*(?P<year>20\d{2})(?!\d)",
    r"(?<!\d)(?P<year>20\d{2})[-_](?P<month>0[1-9]|1[0-2])(?![\d\-/.])",
    # ddmmyyyy, also when glued to the end of an account number (SBI names files <account><ddmmyyyy>.pdf)
    r"(?:0[1-9]|[12]\d|3[01])(?P<month>0[1-9]|1[0-2])(?P<year>20\d{2})(?!\d)",
]


def run(request):
    return request.execute(num_retries=RETRIES)


def detect_bank(sender, subject, filename):
    name, address = parseaddr(sender)
    domain_labels = set(address.rpartition("@")[2].lower().split("."))
    for bank, labels, _ in BANKS:
        if domain_labels & labels:
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
            month = int(month) if month.isdigit() else MONTHS.index(month.lower()) + 1
            yield m.end(), int(m["year"]), month


def statement_month(subject, filename, received):
    # The last date mentioned wins. Dates after the email arrived (like a due date) are ignored.
    latest = (received.year, received.month)
    for text in (subject, filename):
        dates = [(end, y, m) for end, y, m in parse_dates(text) if 1 <= m <= 12 and (y, m) <= latest]
        if dates:
            return max(dates)[1:]
    return latest


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
    found = run(files.list(q=query, spaces="drive", fields="files(id)", pageSize=1)).get("files", [])
    return found[0]["id"] if found else None


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
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/pdf")
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

            for part in pdf_parts(msg["payload"]):
                stats["pdfs"] += 1
                data = attachment_bytes(messages, message_id, part["body"])
                filename = safe_name(part.get("filename") or f"{message_id}.pdf")
                if not data.startswith(b"%PDF"):
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
        except (HttpError, KeyError, ValueError) as exc:
            stats["errors"] += 1
            print(f"ERROR     message {message_id}: {exc}")

    if not dry_run:
        root_id = root_id or find_folder(files, root_name, "root")

    verb = "Would add" if dry_run else "Added"
    print(f"\n{stats['emails']} emails, {stats['pdfs']} PDFs. {verb} {stats['uploaded']} "
          f"({stats['needs_review']} in {REVIEW_FOLDER}). Duplicates skipped: {stats['duplicates']}. "
          f"Non-PDFs skipped: {stats['not_pdf']}. Errors: {stats['errors']}.")
    if root_id:
        print(f"Drive folder: https://drive.google.com/drive/folders/{root_id}")
    return stats["errors"]


def get_credentials(client_secrets, token_path, reauth, open_browser):
    creds = None
    if token_path.exists() and not reauth:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                print("Saved sign-in has expired or was revoked; signing in again.")
                creds = None

    if not creds or not creds.valid:
        if not client_secrets.exists():
            sys.exit(f"Missing {client_secrets}. Create a Desktop OAuth client in Google Cloud, "
                     "download its JSON and save it there (see README.md).")
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
        creds = flow.run_local_server(port=0, open_browser=open_browser)

    token_path.write_text(creds.to_json())
    return creds


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy bank-statement PDFs from Gmail into an organised Google Drive folder.")
    parser.add_argument("--dry-run", action="store_true", help="show what would be added; change nothing")
    parser.add_argument("--after", metavar="YYYY-MM-DD", help="only emails received after this date")
    parser.add_argument("--limit", type=int, help="process at most N emails (handy for a first run)")
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
            datetime.strptime(args.after, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            parser.error("--after must look like 2025-01-31")
        args.query += f" after:{args.after.replace('-', '/')}"
    return args


def main():
    args = parse_args()
    creds = get_credentials(args.credentials, args.token, args.reauth, not args.no_browser)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    note = "   (dry run: nothing will be changed)" if args.dry_run else ""
    print(f"Gmail search: {args.query}{note}\n")
    try:
        errors = sync(gmail, drive, args.query, args.root_folder, args.dry_run, args.limit)
    except HttpError as exc:
        sys.exit(f"Google refused the request: {exc}\nIf you left a permission unticked when signing in, "
                 "run again with --reauth and tick both boxes.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
