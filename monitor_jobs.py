import hashlib
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml


CONFIG_PATH = Path("config.yml")
SEEN_PATH = Path("seen_jobs.json")


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen():
    if not SEEN_PATH.exists():
        return set()
    with SEEN_PATH.open("r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(seen):
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def fetch_text(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    return response.text


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def get_section_blocks(markdown, include_sections, exclude_sections):
    """
    Extracts markdown content under headings that match include_sections.
    If include_sections is empty, returns the whole document.
    """
    if not include_sections:
        return markdown

    lines = markdown.splitlines()
    blocks = []
    current_heading = None
    current_lines = []

    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def flush():
        if current_heading and current_lines:
            heading_lower = current_heading.lower()

            included = any(s.lower() in heading_lower for s in include_sections)
            excluded = any(s.lower() in heading_lower for s in exclude_sections)

            if included and not excluded:
                blocks.append("\n".join(current_lines))

    for line in lines:
        match = heading_pattern.match(line)

        if match:
            flush()
            current_heading = match.group(2)
            current_lines = [line]
        else:
            current_lines.append(line)

    flush()
    return "\n\n".join(blocks)


def extract_markdown_table_rows(markdown):
    """
    Pulls likely job rows from markdown tables.
    Most job-list repos use README tables.
    """
    rows = []

    for line in markdown.splitlines():
        line = line.strip()

        if not line.startswith("|"):
            continue

        if "---" in line:
            continue

        cells = [normalize(cell) for cell in line.strip("|").split("|")]

        if len(cells) < 3:
            continue

        row_text = " | ".join(cells)

        if "company" in row_text.lower() and "role" in row_text.lower():
            continue

        rows.append(row_text)

    return rows


def passes_filters(row, filters):
    row_lower = row.lower()

    keywords = filters.get("keywords_any", [])
    locations = filters.get("locations_any", [])
    excludes = filters.get("exclude_keywords", [])

    if keywords:
        if not any(k.lower() in row_lower for k in keywords):
            return False

    if locations:
        if not any(loc.lower() in row_lower for loc in locations):
            return False

    if excludes:
        if any(ex.lower() in row_lower for ex in excludes):
            return False

    return True


def job_id(source_name, row):
    raw = f"{source_name}:{row}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def send_email(to_email, new_jobs):
    smtp_email = os.environ["SMTP_EMAIL"]
    smtp_password = os.environ["SMTP_PASSWORD"]

    subject = f"{len(new_jobs)} new job posting(s) matched your filters"

    body_lines = ["New matching job postings:\n"]

    for job in new_jobs:
        body_lines.append(f"- [{job['source']}] {job['row']}")

    body = "\n".join(body_lines)

    message = MIMEText(body)
    message["Subject"] = subject
    message["From"] = smtp_email
    message["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_email, smtp_password)
        server.send_message(message)


def main():
    config = load_config()
    seen = load_seen()
    new_jobs = []

    filters = config.get("filters", {})

    for source in config["sources"]:
        text = fetch_text(source["url"])

        if source["type"] == "markdown":
            section_config = source.get("sections", {})
            include_sections = section_config.get("include", [])
            exclude_sections = section_config.get("exclude", [])

            relevant_markdown = get_section_blocks(
                text,
                include_sections=include_sections,
                exclude_sections=exclude_sections,
            )

            rows = extract_markdown_table_rows(relevant_markdown)

        else:
            raise ValueError(f"Unsupported source type: {source['type']}")

        for row in rows:
            if not passes_filters(row, filters):
                continue

            unique_id = job_id(source["name"], row)

            if unique_id not in seen:
                seen.add(unique_id)
                new_jobs.append({
                    "source": source["name"],
                    "row": row,
                })

    if new_jobs:
        send_email(config["email"]["to"], new_jobs)

    save_seen(seen)


if __name__ == "__main__":
    main()