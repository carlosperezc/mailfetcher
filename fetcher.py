#!/usr/bin/env python3
"""
gmail_to_nextcloud.py

Connects to a Gmail account via IMAP, scans for new (unseen) emails,
downloads any attachments, and uploads them to a Nextcloud folder
using the Nextcloud WebDAV API.

All credentials and connection settings are read from config.json
(see config.json in this same folder for the expected structure).

Requirements:
    pip install requests

Gmail setup required before running:
    1. Enable 2-Step Verification on the Google account.
    2. Generate an "App Password" (Google Account > Security > App Passwords).
       Use that 16-character app password in config.json, NOT the normal
       account password -- Gmail no longer accepts plain passwords for IMAP.
    3. Make sure IMAP is enabled in Gmail Settings > Forwarding and POP/IMAP.

Nextcloud setup required before running:
    1. Create an "App Password" for the Nextcloud account too
       (Settings > Security > Devices & sessions > Create new app password).
    2. Make sure the target upload_folder exists or let the script create it.
"""

import imaplib
import email
import json
import logging
import mimetypes
import os
import sys
import time
from email.header import decode_header
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("gmail2nextcloud")


def load_config(path="config.json"):
    if not os.path.exists(path):
        log.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def decode_mime_words(value):
    """Decode a MIME-encoded email header (subject, filename, etc.)."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


class NextcloudUploader:
    def __init__(self, nc_config):
        self.base_url = nc_config["base_url"].rstrip("/")
        self.username = nc_config["username"]
        self.password = nc_config["app_password"]
        self.upload_folder = "/" + nc_config["upload_folder"].strip("/")
        self.auth = HTTPBasicAuth(self.username, self.password)
        self.webdav_root = f"{self.base_url}/remote.php/dav/files/{self.username}"
        # When pointing straight at the container (self-signed cert), set
        # "verify_ssl": false in config.json to skip certificate verification.
        self.verify_ssl = nc_config.get("verify_ssl", True)
        if not self.verify_ssl:
            # Silence the noisy InsecureRequestWarning when verification is off.
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )

    def ensure_folder_exists(self):
        """Create the target folder (and parents) on Nextcloud if missing."""
        parts = [p for p in self.upload_folder.split("/") if p]
        current_path = ""
        for part in parts:
            current_path += "/" + part
            url = self.webdav_root + current_path
            resp = requests.request(
                "MKCOL", url, auth=self.auth, timeout=30, verify=self.verify_ssl
            )
            # 201 = created, 405 = already exists -- both are fine
            if resp.status_code not in (201, 405):
                log.warning(
                    "Unexpected status creating folder %s: %s",
                    current_path,
                    resp.status_code,
                )

    def upload_file(self, local_path, remote_filename):
        """Upload a single file to the configured Nextcloud folder."""
        url = f"{self.webdav_root}{self.upload_folder}/{remote_filename}"
        with open(local_path, "rb") as f:
            data = f.read()
        resp = requests.put(
            url, data=data, auth=self.auth, timeout=120, verify=self.verify_ssl
        )
        if resp.status_code in (200, 201, 204):
            log.info("Uploaded '%s' to Nextcloud folder '%s'", remote_filename, self.upload_folder)
            return True
        else:
            log.error(
                "Failed to upload '%s' (status %s): %s",
                remote_filename,
                resp.status_code,
                resp.text[:300],
            )
            return False


class GmailAttachmentFetcher:
    def __init__(self, gmail_config, options):
        self.email_addr = gmail_config["email"]
        self.app_password = gmail_config["app_password"]
        self.imap_server = gmail_config.get("imap_server", "imap.gmail.com")
        self.imap_port = gmail_config.get("imap_port", 993)
        self.mailbox = gmail_config.get("mailbox", "INBOX")
        self.mark_as_read = gmail_config.get("mark_as_read", True)
        self.only_unseen = gmail_config.get("only_unseen", True)

        self.download_dir = Path(options.get("local_download_dir", "./downloaded_attachments"))
        self.delete_local_after_upload = options.get("delete_local_after_upload", True)
        self.allowed_extensions = [
            ext.lower().lstrip(".") for ext in options.get("allowed_extensions", [])
        ]

        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.conn = None

    def connect(self):
        log.info("Connecting to %s:%s ...", self.imap_server, self.imap_port)
        self.conn = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
        self.conn.login(self.email_addr, self.app_password)
        self.conn.select(self.mailbox)
        log.info("Connected and selected mailbox '%s'", self.mailbox)

    def disconnect(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn.logout()

    def _search_criteria(self):
        return "UNSEEN" if self.only_unseen else "ALL"

    def _extension_allowed(self, filename):
        if not self.allowed_extensions:
            return True
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return ext in self.allowed_extensions

    def _resolve_filename(self, part):
        """Return a filename for a downloadable part, or None to skip it.

        Handles three cases:
          * regular attachments (Content-Disposition: attachment),
          * inline images (Content-Disposition: inline, common for images
            embedded in the message body), and
          * images with no disposition at all but a Content-ID / image type.
        For inline images the filename is often missing, so we synthesize one
        from the Content-ID (or a counter) plus an extension guessed from the
        MIME type.
        """
        disposition = (part.get("Content-Disposition") or "").lower()
        maintype = part.get_content_maintype()
        content_id = part.get("Content-ID")

        is_attachment = "attachment" in disposition
        # An inline image, or an image referenced by Content-ID with no
        # disposition header, is a body image we still want to save.
        is_inline_image = maintype == "image" and (
            "inline" in disposition or content_id is not None or not disposition
        )

        if not (is_attachment or is_inline_image):
            return None

        filename = decode_mime_words(part.get_filename())
        if filename:
            return filename

        # No filename provided -- synthesize one (typical for inline images).
        if not is_inline_image:
            return None

        ext = mimetypes.guess_extension(part.get_content_type()) or ".bin"
        if content_id:
            # Content-ID looks like <abc123@mail>; strip the angle brackets.
            stem = content_id.strip().strip("<>").split("@")[0] or "inline_image"
            # Keep it filesystem-safe.
            stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
        else:
            stem = "inline_image"
        return f"{stem}{ext}"

    def fetch_new_emails(self):
        """Returns a list of dicts: {msg_id, subject, saved_files: [local_paths]}"""
        results = []
        status, data = self.conn.search(None, self._search_criteria())
        if status != "OK":
            log.error("IMAP search failed: %s", status)
            return results

        msg_ids = data[0].split()
        log.info("Found %d message(s) matching criteria.", len(msg_ids))

        for msg_id in msg_ids:
            status, msg_data = self.conn.fetch(msg_id, "(RFC822)")
            if status != "OK":
                log.warning("Failed to fetch message %s", msg_id)
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject = decode_mime_words(msg.get("Subject"))

            saved_files = []
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue

                filename = self._resolve_filename(part)
                if not filename:
                    continue
                if not self._extension_allowed(filename):
                    log.info("Skipping '%s' (extension not allowed)", filename)
                    continue

                # Avoid overwriting files with the same name
                safe_name = filename
                local_path = self.download_dir / safe_name
                counter = 1
                while local_path.exists():
                    stem, dot, ext = filename.rpartition(".")
                    safe_name = f"{stem}_{counter}.{ext}" if dot else f"{filename}_{counter}"
                    local_path = self.download_dir / safe_name
                    counter += 1

                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                with open(local_path, "wb") as f:
                    f.write(payload)

                log.info("Downloaded attachment '%s' from email '%s'", safe_name, subject)
                saved_files.append(str(local_path))

            results.append({"msg_id": msg_id, "subject": subject, "saved_files": saved_files})

            if self.mark_as_read:
                self.conn.store(msg_id, "+FLAGS", "\\Seen")

        return results


def run_once(config):
    gmail_cfg = config["gmail"]
    nc_cfg = config["nextcloud"]
    options = config.get("options", {})

    fetcher = GmailAttachmentFetcher(gmail_cfg, options)
    uploader = NextcloudUploader(nc_cfg)

    try:
        fetcher.connect()
    except imaplib.IMAP4.error as e:
        log.error("Gmail login failed: %s", e)
        log.error("Make sure 2-Step Verification + App Password are set up, and IMAP is enabled.")
        return

    uploader.ensure_folder_exists()

    try:
        emails = fetcher.fetch_new_emails()
    finally:
        fetcher.disconnect()

    total_uploaded = 0
    for item in emails:
        for local_path in item["saved_files"]:
            filename = os.path.basename(local_path)
            success = uploader.upload_file(local_path, filename)
            if success:
                total_uploaded += 1
                if options.get("delete_local_after_upload", True):
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass

    log.info("Run complete. %d attachment(s) uploaded to Nextcloud.", total_uploaded)


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    config = load_config(config_path)
    options = config.get("options", {})

    if options.get("run_once", True):
        run_once(config)
    else:
        interval = options.get("poll_interval_seconds", 60)
        log.info("Starting continuous polling every %s seconds. Ctrl+C to stop.", interval)
        while True:
            try:
                run_once(config)
            except Exception as e:
                log.exception("Unexpected error during run: %s", e)
            time.sleep(interval)


if __name__ == "__main__":
    main()