# Gmail Attachment Fetcher for Nextcloud

`fetcher.py` connects to a Gmail mailbox over IMAP, downloads eligible attachments from matching messages, and uploads them to a Nextcloud folder using WebDAV.

## How It Works

- By default, it searches the configured mailbox for unread messages (`only_unseen: true`). Set this to `false` to scan all messages each run.
- It saves regular attachments and inline images. If `allowed_extensions` is non-empty, files with other extensions are skipped.
- It avoids overwriting files in the local download directory by adding a numeric suffix when a filename already exists there.
- It creates the configured Nextcloud destination folder if it does not exist, then uploads each downloaded file.
- Successfully uploaded local files are deleted when `delete_local_after_upload` is enabled. Failed uploads remain in the local download directory.
- Messages are marked read during the download step when `mark_as_read` is enabled. This happens before upload, even if an upload later fails or no eligible attachment was found. Keep this in mind when relying on unread messages for retry behavior.

## Requirements

- Python 3
- The Python `requests` package
- Gmail IMAP enabled, Google 2-Step Verification enabled, and a Google App Password
- A Nextcloud account and a Nextcloud app password with access to the destination folder

Install the dependency if needed:

```sh
python3 -m pip install requests
```

## Configure

Copy `config_example.json` to `config.json`, then set the Gmail and Nextcloud credentials and destination:

```sh
cp config_example.json config.json
```

The configuration has three sections:

- `gmail`: account email, Google App Password, IMAP server/port, mailbox, and unread/read behavior.
- `nextcloud`: server `base_url`, username, Nextcloud App Password, and `upload_folder` (relative to the user's Nextcloud files). `verify_ssl` should normally remain `true`; set it to `false` only when you intentionally connect using a certificate that cannot be verified.
- `options`: local download directory, whether to delete local files after successful upload, allowed file extensions, and execution mode. `run_once: true` is appropriate for cron; `run_once: false` keeps the process polling continuously at `poll_interval_seconds` intervals.

`config.json` contains secrets and is ignored by Git. Do not commit it.

## Run Once

Run from the project directory so the default config and relative download directory resolve correctly:

```sh
cd /path/to/mailfetcher
python3 fetcher.py
```

You can also pass a config file explicitly:

```sh
python3 fetcher.py /path/to/config.json
```

The script logs progress and errors to standard output. Set `"run_once": true` to process the mailbox once and exit.

## Schedule With Cron

Use an absolute path and change into the project directory so `config.json` and `downloaded_attachments/` are found. For example, to run every five minutes:

```cron
*/5 * * * * cd /media/containerdata/nextcloud/mailfetcher && /usr/bin/python3 fetcher.py >> /media/containerdata/nextcloud/mailfetcher/fetcher.log 2>&1
```

Edit the crontab for the Linux user that can read `config.json` and write to the project directory:

```sh
crontab -e
```

Choose a schedule that fits your needs, and leave `"run_once": true` in the config when using cron. The `poll_interval_seconds` setting is only used when `run_once` is `false`. Check `fetcher.log` to troubleshoot scheduled runs.